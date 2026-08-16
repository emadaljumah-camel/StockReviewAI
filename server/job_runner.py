from __future__ import annotations

import asyncio
import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from job_models import JobPhotoResult, JobProgress, JobResult
from job_store import JobStore
from lmstudio_client import review_with_lmstudio
from models import AppConfig, ReviewRequest


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        config: AppConfig,
        logger: logging.Logger,
    ) -> None:
        self.store = store
        self.config = config
        self.logger = logger
        self.executor = ThreadPoolExecutor(
            max_workers=config.server.max_job_workers,
            thread_name_prefix="stock-review-job",
        )
        self._active: set[str] = set()
        self._lock = Lock()

    def queue(self, job_id: str) -> dict[str, object]:
        self.store.require_complete_upload(job_id)
        status = self.store.get_status(job_id)
        if status.get("status") == "completed":
            return {"job_id": job_id, "status": "completed", "already_running": False}

        with self._lock:
            if job_id in self._active:
                return {"job_id": job_id, "status": "running", "already_running": True}
            self._active.add(job_id)

        request = self.store.get_request(job_id)
        checkpoint = self.store.load_checkpoint(job_id)
        progress = JobProgress(
            stage="queued",
            processed_photos=len(checkpoint),
            total_photos=len(request.photos),
        )
        try:
            self.store.set_status(job_id, "running", progress, errors=[])
            self.executor.submit(self._run, job_id)
        except Exception as exc:
            with self._lock:
                self._active.discard(job_id)
            self.store.set_status(
                job_id,
                "failed",
                progress,
                errors=[str(exc)],
                resumable=True,
            )
            raise
        return {
            "job_id": job_id,
            "status": "running",
            "already_running": False,
            "resumed": status.get("status") in {"failed", "interrupted"},
        }

    def _run(self, job_id: str) -> None:
        try:
            asyncio.run(self.process(job_id))
        except Exception as exc:
            self.logger.exception("job_failed job_id=%s", job_id)
            try:
                status = self.store.get_status(job_id)
                progress = JobProgress.model_validate(status.get("progress", {}))
                self.store.set_status(
                    job_id,
                    "failed",
                    progress,
                    errors=[str(exc)],
                    resumable=True,
                )
            except Exception:
                self.logger.exception("job_failure_status_write_failed job_id=%s", job_id)
        finally:
            with self._lock:
                self._active.discard(job_id)

    async def process(self, job_id: str) -> None:
        request = self.store.get_request(job_id)
        completed = self.store.load_checkpoint(job_id)
        total = len(request.photos)

        for photo in request.photos:
            if photo.photo_id in completed:
                continue
            self.store.set_status(
                job_id,
                "running",
                JobProgress(
                    stage="reviewing",
                    processed_photos=len(completed),
                    total_photos=total,
                    current_photo_id=photo.photo_id,
                ),
                errors=[],
            )
            preview = self.store.preview_path(job_id, photo.photo_id).read_bytes()
            review = await review_with_lmstudio(
                ReviewRequest(
                    filename=photo.filename,
                    image_base64=base64.b64encode(preview).decode("ascii"),
                    threshold=photo.threshold,
                    location_context=photo.location_context,
                ),
                self.config.lmstudio,
                self.logger,
                self.config.geocoding,
            )
            result = JobPhotoResult.from_review(photo.photo_id, review)
            self.store.save_checkpoint(job_id, result)
            completed[photo.photo_id] = result
            self.store.set_status(
                job_id,
                "running",
                JobProgress(
                    stage="reviewing",
                    processed_photos=len(completed),
                    total_photos=total,
                ),
                errors=[],
            )

        ordered = [completed[photo.photo_id] for photo in request.photos]
        self.store.save_result(
            JobResult(job_id=job_id, status="completed", results=ordered)
        )
        self.store.set_status(
            job_id,
            "completed",
            JobProgress(
                stage="completed",
                processed_photos=total,
                total_photos=total,
            ),
            errors=[],
            resumable=False,
        )
        self.logger.info("job_completed job_id=%s photos=%s", job_id, total)
