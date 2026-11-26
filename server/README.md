# StockReviewAI server

See [root setup](../README.md#setup) for installation and the first dry-run.
Run server commands from this directory because imports use local modules.

## Configuration

[config.py](config.py) reads `config.yaml` beside itself. If absent, it returns
built-in [AppConfig](models.py) defaults; the model ID then defaults to
`local-model`, which may not exist in your runtime.

Copy [config.example.yaml](config.example.yaml) only on first setup.
`config.yaml` is ignored by Git. There is no general environment override,
external-config environment selector, automatic `.env` loader, or config
hot-reload in this implementation. Restart the process after changes.

| Section | Meaning |
|---|---|
| `lmstudio.endpoint` | Full chat-completions URL; default loopback port 1234 |
| `lmstudio.model` | Exact image-capable model ID exposed by the runtime |
| `lmstudio.timeout_seconds` | Model HTTP timeout, default 120 |
| `lmstudio.retries` | Additional attempts, default 2 |
| `lmstudio.reasoning_effort` | null leaves defaults; supported schema choices are xhigh, medium, low; runtime support must be checked |
| `server.jobs_dir` | Absolute path or path relative to this server directory |
| `server.log_file` | Server diagnostic log filename beside the application |
| `server.max_job_workers` | Concurrent background jobs, default 1 |
| `server.photo_workers_per_job` | Concurrent photos within each job, default 1 |
| `geocoding` | Reverse URL, User-Agent, timeout, and minimum interval for GPS lookup |

Changing concurrency can increase memory pressure and model-server contention.
It is not a guaranteed speed improvement. Cancellation stops saving/applying
new outcomes but is not a promise to abort a request already executing in the
model runtime.

## Launch and URLs

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
```

Unlike the direct command, `run_server.bat` creates a missing environment and
runs `pip install -r requirements.txt` on every launch. It can change installed
dependency versions. Documentation preparation does not run that launcher.

The app declares `root_path="/stockreview"` for proxy deployment metadata.
Direct clients use `http://127.0.0.1:8001/review`. For the plug-in default
`http://localhost:8080/stockreview/review`, a separately configured proxy must
strip its base-path prefix before forwarding requests. No such proxy is
started by this repository.

## API

Routes declared in [main.py](main.py):

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Process availability: `{"status":"ok"}` |
| POST | `/review` | Synchronous Base64 image assessment |
| POST | `/jobs` | Create or reuse durable manifest |
| POST | `/jobs/{job_id}/photos/{photo_id}/preview` | JSON Base64 preview |
| POST | `/jobs/{job_id}/photos/{photo_id}/preview/binary` | JPEG bytes with image/jpeg content type |
| GET | `/jobs/{job_id}/uploads` | Acknowledged uploads |
| POST | `/jobs/{job_id}/run` | Queue or resume complete uploads |
| POST | `/jobs/{job_id}/cancel` | Mark cancellation or report an already-completed job |
| GET | `/jobs/{job_id}/status` | Job state and progress |
| GET | `/jobs/{job_id}/result` | Completed result, otherwise current status |

There is no job-deletion API. Cancellation retains stored artifacts.
[models.py](models.py) defines ReviewRequest, strict ModelAssessment, and
ReviewResponse. The synchronous response contains verdict, score, reason, and
issues; policy diagnostics live in logs, not an expanded public response.

A ReviewRequest contains filename, Base64 image, an integer threshold from
0 to 100 (default 70), and optional location context. Job request/result
contracts are in [job_models.py](job_models.py).

## Review and scoring pipeline

1. Resolve catalog location context; if incomplete and image GPS exists,
   attempt reverse geocoding. Missing GPS does not itself reject the review.
2. Strip supported JPEG metadata segments from the model-bound image.
3. Send prompt/schema and image to the configured LM Studio-compatible endpoint.
4. Parse and validate the strict assessment. A bounded corrective retry can
   ask the model to fix reported inconsistencies.
5. Apply editorial/location, release, observable-evidence, and composition
   policies. Correctability can cap effective composition severity.
6. Calculate rounded score with weights 35% technical, 25% effective
   composition, and 40% effective commercial/editorial route value.
7. Hard reject or a release-sensitive editorial failure yields NO; otherwise
   apply the submitted score threshold.
8. Persist per-photo result/checkpoint and ordered final job result.

Source: [lmstudio_client.py](lmstudio_client.py), [prompt.py](prompt.py), and
[location_context.py](location_context.py). See
[architecture and safeguards](../docs/ARCHITECTURE.md).

## Jobs and cancellation

[JobStore](job_store.py) persists manifests, preview hashes/JPEGs, status,
checkpoints, and final results using atomic replacement of individual files.
[JobRunner](job_runner.py) uses a thread pool and process-local active/cancel
sets. Resuming skips checkpointed photos. Startup marks running jobs
interrupted; clients request explicit resume.

Cancellation marks the stored job cancelled and prevents its later status
from being overwritten by normal status updates. In-flight work can take time
to stop; checkpoints/results completed beforehand remain. Completed jobs are
reported as already completed. The Lightroom cancellation command can close a
completed active job without applying it.

Use one Uvicorn process with shared job storage. This is not a distributed
queue or a catalog transaction. Do not erase state to force a duplicate job.

## Benchmark and diagnostics

[benchmark_reasoning.py](benchmark_reasoning.py) runs sequential paired
medium/low reasoning comparisons against saved job photos. It invokes real
inference and writes its own output artifacts; it is not part of normal
Lightroom review or a required test.

Use a separate new output directory outside job storage. Consult
`.\.venv\Scripts\python.exe benchmark_reasoning.py --help` before running.
The script reads existing jobs, waits for jobs marked running, samples photos,
and records timing and verdict differences. A comparison without human labels
does not establish accuracy. Runtime support and concurrent workload affect
interpretation. Output can contain filenames, context, and diagnostics.

Model-call timing, usage, response-model identifiers, validation-error
summaries, and bounded content excerpts/hashes can be logged. Treat diagnostic
files as potentially sensitive, not automatically safe for public upload.

## Tests

From this directory:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py" -q
.\.venv\Scripts\python.exe -m pip check
```

The documentation review ran 204 tests in the existing Windows Python 3.14.4
environment. They use mocks/synthetic data; actual Lightroom, network services,
model support, and visual review quality require separate manual checks.
