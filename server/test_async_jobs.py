import asyncio
import base64
import hashlib
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from job_models import CreateJobRequest, JobPhoto, JobProgress
from job_runner import JobCancelled, JobRunner
from job_store import JobStore, PreviewConflictError
from lmstudio_client import ReviewError
from models import AppConfig, ReviewResponse

JPEG = b"\xff\xd8\xfftest-preview"


def job_request(job_id: str = "client-job-1") -> CreateJobRequest:
    return CreateJobRequest(
        client_job_id=job_id,
        photos=[
            JobPhoto(
                photo_id="photo-1",
                catalog_uuid="catalog-photo-1",
                filename="one.jpg",
                threshold=70,
            )
        ],
    )


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            server={"jobs_dir": self.temporary.name, "max_job_workers": 1}
        )
        self.store = JobStore(self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_and_upload_are_idempotent(self):
        job_id, reused = self.store.create_job(job_request())
        repeated_id, repeated = self.store.create_job(job_request())
        encoded = base64.b64encode(JPEG).decode("ascii")
        digest, upload_reused = self.store.save_preview(job_id, "photo-1", encoded)
        repeated_digest, repeated_upload = self.store.save_preview(
            job_id, "photo-1", encoded
        )

        self.assertFalse(reused)
        self.assertTrue(repeated)
        self.assertEqual(repeated_id, job_id)
        self.assertFalse(upload_reused)
        self.assertTrue(repeated_upload)
        self.assertEqual(repeated_digest, digest)

    def test_changed_preview_is_rejected(self):
        job_id, _ = self.store.create_job(job_request())
        self.store.save_preview(
            job_id, "photo-1", base64.b64encode(JPEG).decode("ascii")
        )
        with self.assertRaises(PreviewConflictError):
            self.store.save_preview(
                job_id,
                "photo-1",
                base64.b64encode(JPEG + b"changed").decode("ascii"),
            )

    def test_binary_preview_preserves_exact_bytes_and_shares_idempotency(self):
        job_id, _ = self.store.create_job(job_request())
        preview = JPEG + b"\x00metadata\xff\xe1Exif"
        digest, reused = self.store.save_preview_bytes(job_id, "photo-1", preview)
        repeated_digest, repeated = self.store.save_preview(
            job_id, "photo-1", base64.b64encode(preview).decode("ascii")
        )

        self.assertEqual(digest, hashlib.sha256(preview).hexdigest())
        self.assertFalse(reused)
        self.assertEqual(repeated_digest, digest)
        self.assertTrue(repeated)
        self.assertEqual(
            self.store.preview_path(job_id, "photo-1").read_bytes(), preview
        )

    def test_running_job_is_marked_interrupted_after_restart(self):
        job_id, _ = self.store.create_job(job_request())
        self.store.set_status(
            job_id,
            "running",
            JobProgress(stage="reviewing", total_photos=1),
        )

        self.assertEqual(self.store.mark_running_jobs_interrupted(), [job_id])
        status = self.store.get_status(job_id)
        self.assertEqual(status["status"], "interrupted")
        self.assertTrue(status["resumable"])


class JobRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            server={"jobs_dir": self.temporary.name, "max_job_workers": 1}
        )
        self.store = JobStore(self.config)
        self.runner = JobRunner(
            self.store,
            self.config,
            logging.getLogger("test-job-runner"),
        )
        self.job_id, _ = self.store.create_job(job_request())
        self.store.save_preview(
            self.job_id,
            "photo-1",
            base64.b64encode(JPEG).decode("ascii"),
        )

    async def asyncTearDown(self) -> None:
        self.runner.executor.shutdown(wait=True)
        self.temporary.cleanup()

    async def test_process_checkpoints_and_completes_job(self):
        response = ReviewResponse(
            verdict="YES", score=81, reason="Strong image.", issues=[]
        )
        with patch("job_runner.review_with_lmstudio", AsyncMock(return_value=response)):
            await self.runner.process(self.job_id)

        result = self.store.load_result(self.job_id)
        self.assertEqual(result.results[0].photo_id, "photo-1")
        self.assertEqual(result.results[0].verdict, "YES")
        self.assertEqual(self.store.get_status(self.job_id)["status"], "completed")

    async def test_failed_review_is_not_checkpointed(self):
        with patch(
            "job_runner.review_with_lmstudio",
            AsyncMock(side_effect=ReviewError("validation failed")),
        ), self.assertRaises(ReviewError):
            await self.runner.process(self.job_id)

        self.assertEqual(self.store.load_checkpoint(self.job_id), {})
        self.assertFalse((self.store.job_dir(self.job_id) / "result.json").exists())

    async def test_cancelled_job_stops_before_checkpointing_next_result(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def review(*_args):
            started.set()
            await release.wait()
            return ReviewResponse(
                verdict="YES", score=81, reason="Cancelled review.", issues=[]
            )

        with patch("job_runner.review_with_lmstudio", side_effect=review):
            processing = asyncio.create_task(self.runner.process(self.job_id))
            await started.wait()
            cancelled = self.runner.cancel(self.job_id)
            release.set()
            with self.assertRaises(JobCancelled):
                await processing

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.store.load_checkpoint(self.job_id), {})
        self.assertEqual(self.store.get_status(self.job_id)["status"], "cancelled")
        self.assertFalse((self.store.job_dir(self.job_id) / "result.json").exists())

    async def test_completed_job_is_left_intact_when_closed(self):
        response = ReviewResponse(
            verdict="YES", score=81, reason="Completed review.", issues=[]
        )
        with patch("job_runner.review_with_lmstudio", AsyncMock(return_value=response)):
            await self.runner.process(self.job_id)

        closed = self.runner.cancel(self.job_id)

        self.assertEqual(closed["status"], "completed")
        self.assertTrue(closed["already_completed"])
        self.assertEqual(self.store.get_status(self.job_id)["status"], "completed")
        self.assertTrue((self.store.job_dir(self.job_id) / "result.json").exists())

    async def test_photo_concurrency_is_bounded_and_results_keep_input_order(self):
        photos = [
            JobPhoto(
                photo_id=f"photo-{index}",
                filename=f"{index}.jpg",
                threshold=70,
            )
            for index in range(3)
        ]
        request = CreateJobRequest(client_job_id="concurrent-job", photos=photos)
        job_id, _ = self.store.create_job(request)
        for photo in photos:
            self.store.save_preview(
                job_id,
                photo.photo_id,
                base64.b64encode(JPEG).decode("ascii"),
            )
        config = AppConfig(
            server={
                "jobs_dir": self.temporary.name,
                "max_job_workers": 1,
                "photo_workers_per_job": 2,
            }
        )
        runner = JobRunner(self.store, config, logging.getLogger("test-concurrency"))
        active = 0
        max_active = 0

        async def review(request, *_args):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ReviewResponse(
                verdict="YES",
                score=int(request.filename.split(".")[0]),
                reason="Concurrent review.",
                issues=[],
            )

        try:
            with patch("job_runner.review_with_lmstudio", side_effect=review):
                await runner.process(job_id)
        finally:
            runner.executor.shutdown(wait=True)

        result = self.store.load_result(job_id)
        self.assertEqual(max_active, 2)
        self.assertEqual(
            [item.photo_id for item in result.results],
            ["photo-0", "photo-1", "photo-2"],
        )
        self.assertEqual(self.store.get_status(job_id)["status"], "completed")


class JobEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        config = AppConfig(server={"jobs_dir": self.temporary.name})
        self.previous_store = main.JOB_STORE
        self.previous_runner = main.JOB_RUNNER
        main.JOB_STORE = JobStore(config)
        main.JOB_RUNNER = unittest.mock.Mock()

    def tearDown(self) -> None:
        main.JOB_STORE = self.previous_store
        main.JOB_RUNNER = self.previous_runner
        self.temporary.cleanup()

    def test_create_upload_and_queue_contract(self):
        main.JOB_RUNNER.queue.return_value = {
            "job_id": "endpoint-job",
            "status": "running",
        }
        with TestClient(main.app) as client:
            created = client.post(
                "/jobs",
                json=job_request("endpoint-job").model_dump(mode="json"),
            )
            uploaded = client.post(
                "/jobs/endpoint-job/photos/photo-1/preview",
                json={"image_base64": base64.b64encode(JPEG).decode("ascii")},
            )
            queued = client.post("/jobs/endpoint-job/run")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(queued.json()["status"], "running")
        main.JOB_RUNNER.queue.assert_called_once_with("endpoint-job")

    def test_unknown_job_returns_not_found(self):
        with TestClient(main.app) as client:
            response = client.get("/jobs/not-present/status")
        self.assertEqual(response.status_code, 404)

    def test_cancel_contract(self):
        main.JOB_RUNNER.cancel.return_value = {
            "job_id": "endpoint-job",
            "status": "cancelled",
            "already_cancelled": False,
            "was_running": True,
        }
        with TestClient(main.app) as client:
            client.post(
                "/jobs", json=job_request("endpoint-job").model_dump(mode="json")
            )
            cancelled = client.post("/jobs/endpoint-job/cancel")

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        main.JOB_RUNNER.cancel.assert_called_once_with("endpoint-job")

    def test_binary_upload_contract_and_legacy_compatibility(self):
        with TestClient(main.app) as client:
            created = client.post(
                "/jobs", json=job_request("binary-endpoint-job").model_dump(mode="json")
            )
            uploaded = client.post(
                "/jobs/binary-endpoint-job/photos/photo-1/preview/binary",
                content=JPEG + b"\x00Exif",
                headers={"Content-Type": "image/jpeg"},
            )
            repeated = client.post(
                "/jobs/binary-endpoint-job/photos/photo-1/preview",
                json={
                    "image_base64": base64.b64encode(JPEG + b"\x00Exif").decode(
                        "ascii"
                    )
                },
            )
            invalid_type = client.post(
                "/jobs/binary-endpoint-job/photos/photo-1/preview/binary",
                content=JPEG,
                headers={"Content-Type": "application/octet-stream"},
            )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(uploaded.status_code, 200)
        self.assertFalse(uploaded.json()["reused"])
        self.assertTrue(repeated.json()["reused"])
        self.assertEqual(invalid_type.status_code, 415)


class LightroomAsyncContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        plugin = Path(__file__).resolve().parent.parent / "StockReviewAI.lrplugin"
        cls.submit_source = (plugin / "StockReviewAI.lua").read_text(encoding="utf-8")
        cls.check_source = (plugin / "CheckJobStatus.lua").read_text(encoding="utf-8")
        cls.client_source = (plugin / "JobClient.lua").read_text(encoding="utf-8")
        cls.info_source = (plugin / "Info.lua").read_text(encoding="utf-8")
        cls.cancel_source = (plugin / "CancelJob.lua").read_text(encoding="utf-8")

    def test_submit_returns_after_server_queue_acknowledgement(self):
        self.assertIn("JobClient.submit", self.submit_source)
        self.assertIn("safe to disconnect the laptop", self.submit_source)
        self.assertNotIn("AiClient.reviewPreview", self.submit_source)

    def test_status_command_supports_resume_and_cached_result(self):
        self.assertIn('active.status == "submitting"', self.check_source)
        self.assertIn("JobClient.run", self.check_source)
        self.assertIn('active.status == "downloaded"', self.check_source)
        self.assertIn("completed_result.json", self.check_source)

    def test_status_menu_is_registered(self):
        self.assertIn("Check Job Status / Apply Results", self.info_source)

    def test_cancel_menu_uses_server_and_closes_local_job(self):
        self.assertIn("Cancel Active Job", self.info_source)
        self.assertIn("JobClient.cancel", self.cancel_source)
        self.assertIn('active.status = "cancelled"', self.cancel_source)
        self.assertIn("active.closed = true", self.cancel_source)

    def test_lightroom_post_method_precedes_timeout(self):
        self.assertIn('"POST",\n        settings.timeoutSeconds', self.client_source)

    def test_lightroom_uses_one_export_session_and_binary_uploads(self):
        self.assertEqual(self.submit_source.count("LrExportSession {"), 1)
        self.assertIn("photosToExport = photos", self.submit_source)
        self.assertIn("LR_initialSequenceNumber = 1", self.submit_source)
        self.assertIn(
            'LR_tokens = "stock-review-ai-{{naming_sequenceNumber_5Digits}}"',
            self.submit_source,
        )
        self.assertIn("preview/binary", self.client_source)
        self.assertNotIn("AiClient", self.client_source)
        self.assertNotIn("base64Encode", self.client_source)


if __name__ == "__main__":
    unittest.main()
