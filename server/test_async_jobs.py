import base64
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from job_models import CreateJobRequest, JobPhoto, JobProgress
from job_runner import JobRunner
from job_store import JobStore, PreviewConflictError
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


class LightroomAsyncContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        plugin = Path(__file__).resolve().parent.parent / "StockReviewAI.lrplugin"
        cls.submit_source = (plugin / "StockReviewAI.lua").read_text(encoding="utf-8")
        cls.check_source = (plugin / "CheckJobStatus.lua").read_text(encoding="utf-8")
        cls.client_source = (plugin / "JobClient.lua").read_text(encoding="utf-8")
        cls.info_source = (plugin / "Info.lua").read_text(encoding="utf-8")

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

    def test_lightroom_post_method_precedes_timeout(self):
        self.assertIn('"POST",\n        settings.timeoutSeconds', self.client_source)


if __name__ == "__main__":
    unittest.main()
