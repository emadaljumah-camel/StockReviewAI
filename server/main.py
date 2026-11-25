import logging
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException

from config import load_config
from job_models import CreateJobRequest, PreviewUploadRequest
from job_runner import JobRunner
from job_store import JobConflictError, JobStore, PreviewConflictError
from lmstudio_client import ReviewError, review_with_lmstudio
from models import ReviewRequest, ReviewResponse


CONFIG = load_config()

app = FastAPI(
    title="StockReviewAI Server",
    version="1.0.0",
    root_path="/stockreview",
)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("StockReviewAI")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    log_path = Path(__file__).with_name(CONFIG.server.log_file)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s\t%(levelname)s\t%(message)s"))
    logger.addHandler(handler)
    return logger


LOGGER = _build_logger()
JOB_STORE = JobStore(CONFIG)
JOB_RUNNER = JobRunner(JOB_STORE, CONFIG, LOGGER)


@app.on_event("startup")
def recover_interrupted_jobs() -> None:
    for job_id in JOB_STORE.mark_running_jobs_interrupted():
        LOGGER.warning("job_interrupted_after_restart job_id=%s", job_id)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/review", response_model=ReviewResponse)
async def review(request: ReviewRequest) -> ReviewResponse:
    try:
        return await review_with_lmstudio(
            request,
            CONFIG.lmstudio,
            LOGGER,
            CONFIG.geocoding,
        )
    except ReviewError as exc:
        LOGGER.error("review_failed filename=%s error=%s", request.filename, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _require_job(job_id: str) -> None:
    try:
        exists = JOB_STORE.job_dir(job_id).exists()
    except ValueError:
        exists = False
    if not exists:
        raise HTTPException(status_code=404, detail="Unknown job")


@app.post("/jobs")
def create_job(request: CreateJobRequest) -> dict[str, object]:
    try:
        job_id, reused = JOB_STORE.create_job(request)
    except (JobConflictError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    status = JOB_STORE.get_status(job_id)["status"]
    return {"job_id": job_id, "status": status, "reused": reused}


@app.post("/jobs/{job_id}/photos/{photo_id}/preview")
def upload_preview(
    job_id: str,
    photo_id: str,
    request: PreviewUploadRequest,
) -> dict[str, object]:
    _require_job(job_id)
    try:
        digest, reused = JOB_STORE.save_preview(
            job_id,
            photo_id,
            request.image_base64,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown photo_id for this job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PreviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": job_id,
        "photo_id": photo_id,
        "status": "uploaded",
        "sha256": digest,
        "reused": reused,
    }


@app.post("/jobs/{job_id}/photos/{photo_id}/preview/binary")
def upload_binary_preview(
    job_id: str,
    photo_id: str,
    data: bytes = Body(media_type="image/jpeg"),
    content_type: str | None = Header(default=None),
) -> dict[str, object]:
    _require_job(job_id)
    if (content_type or "").split(";", 1)[0].strip().lower() != "image/jpeg":
        raise HTTPException(
            status_code=415, detail="Preview Content-Type must be image/jpeg"
        )
    try:
        digest, reused = JOB_STORE.save_preview_bytes(job_id, photo_id, data)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Unknown photo_id for this job"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PreviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": job_id,
        "photo_id": photo_id,
        "status": "uploaded",
        "sha256": digest,
        "reused": reused,
    }


@app.get("/jobs/{job_id}/uploads")
def uploaded_photos(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    return {
        "job_id": job_id,
        "uploaded_photo_ids": JOB_STORE.uploaded_photo_ids(job_id),
    }


@app.post("/jobs/{job_id}/run")
def run_job(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    try:
        return JOB_RUNNER.queue(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    try:
        return JOB_RUNNER.cancel(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/status")
def job_status(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    return JOB_STORE.get_status(job_id)


@app.get("/jobs/{job_id}/result")
def job_result(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    status = JOB_STORE.get_status(job_id)
    if status.get("status") != "completed":
        return status
    try:
        return JOB_STORE.load_result(job_id).model_dump(mode="json")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Stored result is invalid") from exc
