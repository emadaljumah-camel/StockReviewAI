"""Sequential paired benchmark; reads job inputs, never changes job state."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from config import load_config
from job_models import CreateJobRequest
from lmstudio_client import ReviewError, review_with_lmstudio
from location_context import resolve_location_context, strip_jpeg_metadata_base64
from models import ReviewRequest
from prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT


def select_photos(photos, count):
    """Evenly sample the job order, including both endpoints; no score cherry-picking."""
    if not 1 <= count <= len(photos):
        raise ValueError("count must be between 1 and the number of uploaded photos")
    if count == 1:
        return photos[:1]
    return [photos[i * (len(photos) - 1) // (count - 1)] for i in range(count)]


def arm_order(index):
    return ("medium", "low") if index % 2 == 0 else ("low", "medium")


def active_jobs(jobs_dir):
    active = []
    for path in sorted(jobs_dir.glob("*/status.json")):
        status = json.loads(path.read_text(encoding="utf-8"))
        if status.get("status") == "running":
            active.append(path.parent.name)
    return active


async def wait_for_jobs(jobs_dir):
    while jobs := active_jobs(jobs_dir):
        print(f"Waiting for active jobs: {', '.join(jobs)}", flush=True)
        await asyncio.sleep(30)


def compare_pairs(rows):
    by_photo = {}
    for row in rows:
        by_photo.setdefault(row["photo_id"], {})[row["arm"]] = row
    pairs = []
    for photo_id, arms in by_photo.items():
        if not all(arm in arms and "result" in arms[arm] for arm in ("medium", "low")):
            continue
        baseline, low = arms["medium"], arms["low"]
        a, b = (
            baseline["diagnostics"]["assessment"],
            low["diagnostics"]["assessment"],
        )
        pairs.append(
            {
                "photo_id": photo_id,
                "filename": baseline["filename"],
                "threshold": baseline["threshold"],
                "medium_score": baseline["result"]["score"],
                "low_score": low["result"]["score"],
                "score_delta": low["result"]["score"] - baseline["result"]["score"],
                "medium_verdict": baseline["result"]["verdict"],
                "low_verdict": low["result"]["verdict"],
                "verdict_changed": baseline["result"]["verdict"]
                != low["result"]["verdict"],
                "changed_fields": [key for key in a if a[key] != b[key]],
                "effective_severity_changed": (
                    baseline["diagnostics"]["composition_policy"][
                        "effective_composition_defect_severity"
                    ]
                    != low["diagnostics"]["composition_policy"][
                        "effective_composition_defect_severity"
                    ]
                ),
                "release_restriction_changed": (
                    baseline["diagnostics"]["release_restriction"]
                    != low["diagnostics"]["release_restriction"]
                ),
            }
        )
    return pairs


def original_timings(log_path, job_id, photo_ids):
    if not log_path.exists():
        return {}
    pattern = re.compile(
        rf"photo_review_timing job_id={re.escape(job_id)} "
        r"photo_id=(\S+) duration_seconds=([\d.]+)"
    )
    return {
        match.group(1): float(match.group(2))
        for match in pattern.finditer(log_path.read_text(encoding="utf-8"))
        if match.group(1) in photo_ids
    }


def summarize(rows):
    summary = {}
    for arm in ("medium", "low"):
        selected = [r for r in rows if r["arm"] == arm]
        attempts = [a for r in selected for a in r["diagnostics"].get("attempts", [])]
        reasoning = [
            a.get("usage", {})
            .get("completion_tokens_details", {})
            .get("reasoning_tokens")
            for a in attempts
        ]
        known = [n for n in reasoning if isinstance(n, int)]
        summary[arm] = {
            "reviews": len(selected),
            "picks": sum(r.get("result", {}).get("verdict") == "YES" for r in selected),
            "rejects": sum(
                r.get("result", {}).get("verdict") == "NO" for r in selected
            ),
            "failures": sum("error" in r for r in selected),
            "retries": sum(
                max(0, len(r["diagnostics"].get("attempts", [])) - 1) for r in selected
            ),
            "mean_seconds": statistics.mean(r["duration_seconds"] for r in selected)
            if selected
            else None,
            "median_seconds": statistics.median(r["duration_seconds"] for r in selected)
            if selected
            else None,
            "reasoning_tokens_total": sum(known) if known else None,
            "attempts_with_reasoning_usage": len(known),
            "attempts": len(attempts),
        }
    return summary


def write_outputs(output, document):
    document["summary"] = summarize(document["reviews"])
    document["comparisons"] = compare_pairs(document["reviews"])
    output.mkdir(parents=True, exist_ok=True)
    # Only these two benchmark artifacts are written. Persist after each review.
    (output / "results.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# Reasoning benchmark",
        "",
        f"Status: {document['status']}",
        "",
        (
            "Same corrected prompt/schema, preview bytes, location context, threshold, model, and temperature; "
            "medium versus low reasoning. Sequential requests, alternating arm order. "
            "Location resolution is outside timed inference. Existing job verdicts are not the baseline."
        ),
        "",
        (
            "One run per image per arm; no human labels. Differences are not accuracy measurements. "
            "Cache effects and normal inference variability remain. Requested reasoning levels must be "
            "verified against the runtime's rendered model input before attributing differences to reasoning."
        ),
        "",
        "| Arm | Reviews | Mean seconds | Median seconds | Picks | Rejects | Failures | Retries | Reasoning tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, stats in document["summary"].items():
        mean = (
            f"{stats['mean_seconds']:.2f}"
            if stats["mean_seconds"] is not None
            else "N/A"
        )
        median = (
            f"{stats['median_seconds']:.2f}"
            if stats["median_seconds"] is not None
            else "N/A"
        )
        lines.append(
            f"| {arm} | {stats['reviews']} | {mean} | {median} | {stats['picks']} | {stats['rejects']} | {stats['failures']} | {stats['retries']} | {stats['reasoning_tokens_total']} |"
        )
    pairs = document["comparisons"]
    lines += [
        "",
        (
            f"Complete pairs: {len(pairs)}. Verdict changes: {sum(p['verdict_changed'] for p in pairs)}. "
            f"Effective severity changes: {sum(p['effective_severity_changed'] for p in pairs)}. "
            f"Release-route changes: {sum(p['release_restriction_changed'] for p in pairs)}."
        ),
        "",
        "| Photo | Threshold | Medium score/verdict | Low score/verdict | Changed assessment fields |",
        "|---|---:|---|---|---|",
    ]
    for pair in pairs:
        lines.append(
            f"| {pair['filename']} | {pair['threshold']} | {pair['medium_score']} / {pair['medium_verdict']} | {pair['low_score']} / {pair['low_verdict']} | {', '.join(pair['changed_fields'])} |"
        )
    originals = {r["photo_id"]: r for r in document.get("original_job_results", [])}
    if originals:
        original_times = document.get("original_job_timing_seconds", {})
        if original_times:
            lines += [
                "",
                (
                    f"Historical same-photo review timing: {len(original_times)} photos, "
                    f"mean {statistics.mean(original_times.values()):.2f}s, "
                    f"median {statistics.median(original_times.values()):.2f}s. "
                    "Historical timings include location resolution and use the older prompt/policy."
                ),
            ]
        for arm in ("medium", "low"):
            compared = [
                r
                for r in document["reviews"]
                if r["arm"] == arm and "result" in r and r["photo_id"] in originals
            ]
            changed = sum(
                r["result"]["verdict"] != originals[r["photo_id"]]["verdict"]
                for r in compared
            )
            lines += [
                "",
                f"Original job versus new {arm}: {changed}/{len(compared)} verdicts changed.",
            ]
        lines += [
            "",
            "## Comparison with the original job",
            "",
            document["original_job_note"],
            "",
            "| Photo | Original score/verdict | New medium score/verdict | New low score/verdict |",
            "|---|---|---|---|",
        ]
        for pair in pairs:
            original = originals.get(pair["photo_id"])
            if original:
                lines.append(
                    f"| {pair['filename']} | {original['score']} / {original['verdict']} "
                    f"| {pair['medium_score']} / {pair['medium_verdict']} "
                    f"| {pair['low_score']} / {pair['low_verdict']} |"
                )
    if document.get("runtime_verification"):
        lines += [
            "",
            "## Runtime verification",
            "",
            document["runtime_verification"]["evidence"],
        ]
    if document.get("error"):
        lines += ["", f"Stopped: {document['error']}"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args):
    config = load_config()
    job_dir = args.job_dir.resolve()
    output = args.output.resolve()
    jobs_dir = (Path(__file__).parent / config.server.jobs_dir).resolve()
    if output == jobs_dir or jobs_dir in output.parents:
        raise ValueError("Benchmark output must be outside job storage")
    if (output / "results.json").exists():
        previous = json.loads((output / "results.json").read_text(encoding="utf-8"))
        if previous.get("reviews") or previous.get("job_id") != job_dir.name:
            raise ValueError(
                "Output contains results or another job; choose a new output directory"
            )
    elif (output / "report.md").exists():
        raise ValueError("Output already exists; choose a new output directory")
    payload = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    request = CreateJobRequest.model_validate(
        {key: value for key, value in payload.items() if not key.startswith("_")}
    )
    photos = select_photos(request.photos, args.count)
    saved_path = job_dir / "result.json"
    original_results = (
        json.loads(saved_path.read_text(encoding="utf-8")).get("results", [])
        if saved_path.exists()
        else []
    )
    document = {
        "status": "waiting",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_dir.name,
        "model": config.lmstudio.model,
        "endpoint": config.lmstudio.endpoint,
        "photo_ids": [p.photo_id for p in photos],
        "selection": "evenly spaced in original job order",
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(
            json.dumps(RESPONSE_SCHEMA, sort_keys=True).encode()
        ).hexdigest(),
        "reasoning_parameter": "reasoning_effort",
        "arms": ["medium", "low"],
        "original_job_results": [
            r for r in original_results if r["photo_id"] in {p.photo_id for p in photos}
        ],
        "original_job_note": "Historical results use the older composition policy; differences from them are not attributable to reasoning alone.",
        "reviews": [],
    }
    logger = logging.getLogger("reasoning-benchmark")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    write_outputs(output, document)
    try:
        await wait_for_jobs(jobs_dir)
        document["status"] = "running"
        # Reload after waiting, since a running source job may only now have results.
        if saved_path.exists():
            document["original_job_results"] = [
                r
                for r in json.loads(saved_path.read_text(encoding="utf-8"))["results"]
                if r["photo_id"] in document["photo_ids"]
            ]
        document["original_job_timing_seconds"] = original_timings(
            Path(__file__).parent / config.server.log_file,
            job_dir.name,
            set(document["photo_ids"]),
        )
        for index, photo in enumerate(photos):
            preview = (job_dir / "previews" / f"{photo.photo_id}.jpg").read_bytes()
            encoded = base64.b64encode(preview).decode("ascii")
            location = await resolve_location_context(
                encoded, photo.location_context, config.geocoding
            )
            # Resolve once for identical context and remove GPS to avoid timed network lookups.
            review_request = ReviewRequest(
                filename=photo.filename,
                image_base64=strip_jpeg_metadata_base64(encoded),
                threshold=photo.threshold,
                location_context=location.context,
            )
            for arm in arm_order(index):
                await wait_for_jobs(jobs_dir)
                settings = config.lmstudio.model_copy(update={"reasoning_effort": arm})
                diagnostics = {}
                row = {
                    "photo_id": photo.photo_id,
                    "filename": photo.filename,
                    "threshold": photo.threshold,
                    "arm": arm,
                    "preview_sha256": hashlib.sha256(preview).hexdigest(),
                    "location_context": location.context.model_dump()
                    if location.context
                    else None,
                    "diagnostics": diagnostics,
                }
                started = time.perf_counter()
                try:
                    result = await review_with_lmstudio(
                        review_request,
                        settings,
                        logger,
                        config.geocoding,
                        diagnostics=diagnostics,
                    )
                    row["result"] = result.model_dump()
                except ReviewError as exc:
                    row["error"] = str(exc)
                row["duration_seconds"] = time.perf_counter() - started
                row["concurrent_job_detected"] = bool(active_jobs(jobs_dir))
                document["reviews"].append(row)
                write_outputs(output, document)
                print(
                    f"Photo {index + 1}/{len(photos)} {arm}: {row['duration_seconds']:.2f}s, "
                    f"{row.get('result', {}).get('verdict', 'FAILED')}",
                    flush=True,
                )
                if row["concurrent_job_detected"]:
                    raise RuntimeError(
                        "A production job started during inference; timing is contaminated"
                    )
        document["status"] = "completed"
    except BaseException as exc:
        document["status"] = "interrupted"
        document["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        write_outputs(output, document)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "output/reasoning-benchmark",
    )
    asyncio.run(run(parser.parse_args()))
