from __future__ import annotations

import asyncio
import base64
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from job_models import JobPhoto, JobPhotoResult, JobProgress, JobResult
from job_store import JobStore
from lmstudio_client import review_with_lmstudio
from models import AppConfig, ReviewRequest


class JobCancelled(Exception):
    """Raised internally when a user cancels an unfinished job."""


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
        self._cancel_requested: set[str] = set()
        self._lock = Lock()

    def queue(self, job_id: str) -> dict[str, object]:
        self.store.require_complete_upload(job_id)
        status = self.store.get_status(job_id)
        if status.get("status") == "completed":
            return {"job_id": job_id, "status": "completed", "already_running": False}
        if status.get("status") == "cancelled":
            raise ValueError("Cancelled jobs cannot be restarted")

        with self._lock:
            if self.store.get_status(job_id).get("status") == "cancelled":
                raise ValueError("Cancelled jobs cannot be restarted")
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

    def cancel(self, job_id: str) -> dict[str, object]:
        status = self.store.get_status(job_id)
        current_status = status.get("status")
        if current_status == "completed":
            return {
                "job_id": job_id,
                "status": "completed",
                "already_completed": True,
                "was_running": False,
            }

        progress = JobProgress.model_validate(status.get("progress", {}))
        with self._lock:
            already_cancelled = current_status == "cancelled"
            self._cancel_requested.add(job_id)
            active = job_id in self._active
            self.store.set_status(
                job_id,
                "cancelled",
                progress,
                errors=["Cancelled by user"],
                resumable=False,
            )
        return {
            "job_id": job_id,
            "status": "cancelled",
            "already_cancelled": already_cancelled,
            "was_running": active,
        }

    def _raise_if_cancel_requested(self, job_id: str) -> None:
        with self._lock:
            cancelled = job_id in self._cancel_requested
        if cancelled or self.store.get_status(job_id).get("status") == "cancelled":
            raise JobCancelled()

    def _run(self, job_id: str) -> None:
        try:
            asyncio.run(self.process(job_id))
        except JobCancelled:
            self.logger.info("job_cancelled job_id=%s", job_id)
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
        concurrency = self.config.server.photo_workers_per_job
        pending = [photo for photo in request.photos if photo.photo_id not in completed]

        for batch_start in range(0, len(pending), concurrency):
            self._raise_if_cancel_requested(job_id)
            batch = pending[batch_start : batch_start + concurrency]
            self.store.set_status(
                job_id,
                "running",
                JobProgress(
                    stage="reviewing",
                    processed_photos=len(completed),
                    total_photos=total,
                    current_photo_id=batch[0].photo_id,
                ),
                errors=[],
            )

            tasks = [
                asyncio.create_task(self._review_photo(job_id, photo))
                for photo in batch
            ]
            first_error: Exception | None = None
            try:
                for task in asyncio.as_completed(tasks):
                    try:
                        photo, result = await task
                    except Exception as exc:  # noqa: BLE001 - preserve partial checkpoints
                        first_error = first_error or exc
                        continue

                    self._raise_if_cancel_requested(job_id)
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
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if any(not task.done() for task in tasks):
                    await asyncio.gather(*tasks, return_exceptions=True)

            if first_error is not None:
                raise first_error

        self._raise_if_cancel_requested(job_id)
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

    async def _review_photo(
        self, job_id: str, photo: JobPhoto
    ) -> tuple[JobPhoto, JobPhotoResult]:
        started = time.perf_counter()
        try:
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
            return photo, JobPhotoResult.from_review(photo.photo_id, review)
        finally:
            self.logger.info(
                "photo_review_timing job_id=%s photo_id=%s duration_seconds=%.3f",
                job_id,
                photo.photo_id,
                time.perf_counter() - started,
            )
