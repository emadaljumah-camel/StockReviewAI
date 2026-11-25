import json
import logging
import unittest
from typing import ClassVar
from unittest.mock import patch

from pydantic import ValidationError

from lmstudio_client import ReviewError, _build_payload, review_with_lmstudio
from models import LMStudioSettings, ModelAssessment, ReviewRequest
from prompt import SYSTEM_PROMPT


def valid_assessment(**overrides):
    value = {
        "technical_score": 80,
        "composition_score": 70,
        "commercial_value_score": 60,
        "editorial_value_score": 50,
        "main_subject_type": "discrete",
        "main_subject_visibility": "clear",
        "foreground_dominates_frame": False,
        "composition_defect_severity": "none",
        "composition_correctability": "not_normally_correctable",
        "composition_defects": [],
        "human_activity_readability": "none",
        "editorial_evidence": [],
        "recognizable_people": False,
        "release_sensitive_property": False,
        "prominent_ip_content": False,
        "hard_reject": False,
        "hard_reject_reasons": [],
        "reason": "A usable stock photograph.",
        "issues": [],
    }
    value.update(overrides)
    return value


def lm_response(content, model="qwen/qwen3.8-27b"):
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
    }


class FakeResponse:
    status_code = 200
    reason_phrase = "OK"
    text = ""

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeAsyncClient:
    responses: ClassVar[list] = []
    calls: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, endpoint, json):
        self.__class__.calls.append((endpoint, json))
        return FakeResponse(self.__class__.responses.pop(0))


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class LMStudioClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAsyncClient.responses = []
        FakeAsyncClient.calls = []
        self.logger = logging.getLogger("test-lmstudio-client")
        self.logger.setLevel(logging.INFO)
        self.handler = LogCapture()
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)

    def test_prompt_states_bidirectional_invariants(self):
        self.assertIn(
            'an empty\ncomposition_defects list requires composition_defect_severity to be exactly\n"none"',
            SYSTEM_PROMPT,
        )
        self.assertIn(
            'Every non-"none" composition_defect_severity requires at least one\nspecific composition defect',
            SYSTEM_PROMPT,
        )
        self.assertIn(
            "when\nhard_reject is true, hard_reject_reasons must contain at least one specific\nvalid reason",
            SYSTEM_PROMPT,
        )
        self.assertIn(
            "When hard_reject is false, hard_reject_reasons must be an empty\nlist",
            SYSTEM_PROMPT,
        )

    async def review(self, responses, retries=2):
        FakeAsyncClient.responses = responses
        request = ReviewRequest(
            filename="DSC_8595.NEF",
            image_base64="/9j/",
            threshold=70,
        )
        settings = LMStudioSettings(
            model="qwen/qwen3.8-27b",
            retries=retries,
        )
        with patch("lmstudio_client.httpx.AsyncClient", FakeAsyncClient):
            return await review_with_lmstudio(request, settings, self.logger)

    def test_reasoning_setting_is_optional_and_validated(self):
        request = ReviewRequest(filename="a.jpg", image_base64="/9j/")
        self.assertNotIn("reasoning_effort", _build_payload(request, LMStudioSettings()))
        for effort in ("xhigh", "medium", "low"):
            payload = _build_payload(request, LMStudioSettings(reasoning_effort=effort))
            self.assertEqual(payload["reasoning_effort"], effort)
        with self.assertRaises(ValidationError):
            LMStudioSettings(reasoning_effort="unsupported")

    async def test_diagnostics_capture_usage_retry_and_final_assessment(self):
        invalid = valid_assessment(composition_defect_severity="minor", composition_defects=[])
        response = lm_response(json.dumps(valid_assessment()))
        response["usage"] = {"completion_tokens": 1000, "completion_tokens_details": {"reasoning_tokens": 700}}
        FakeAsyncClient.responses = [lm_response(json.dumps(invalid)), response]
        diagnostics = {}
        with patch("lmstudio_client.httpx.AsyncClient", FakeAsyncClient):
            await review_with_lmstudio(
                ReviewRequest(filename="a.jpg", image_base64="/9j/"),
                LMStudioSettings(reasoning_effort="medium"), self.logger,
                diagnostics=diagnostics,
            )
        self.assertEqual(len(diagnostics["attempts"]), 2)
        self.assertFalse(diagnostics["attempts"][0]["success"])
        self.assertTrue(diagnostics["attempts"][1]["success"])
        self.assertEqual(diagnostics["attempts"][1]["usage"]["completion_tokens"], 1000)
        self.assertEqual(diagnostics["assessment"]["composition_correctability"], "not_normally_correctable")
        self.assertTrue(all(call[1]["reasoning_effort"] == "medium" for call in FakeAsyncClient.calls))

    async def test_valid_response_is_unchanged(self):
        result = await self.review(
            [lm_response(json.dumps(valid_assessment()))],
        )

        self.assertEqual(result.verdict, "YES")
        self.assertEqual(len(FakeAsyncClient.calls), 1)
        self.assertIn("attempt_type=ordinary", next(r.getMessage() for r in self.handler.records if "lmstudio_attempt" in r.getMessage()))

    async def test_invalid_response_is_corrected_and_succeeds(self):
        invalid = valid_assessment(
            composition_defect_severity="minor",
            composition_defects=[],
        )
        invalid_json = json.dumps(invalid)
        valid_json = json.dumps(valid_assessment())

        await self.review(
            [lm_response(invalid_json), lm_response(valid_json)],
        )

        self.assertEqual(len(FakeAsyncClient.calls), 2)
        first_payload = FakeAsyncClient.calls[0][1]
        second_payload = FakeAsyncClient.calls[1][1]
        self.assertEqual(
            second_payload["messages"][:2],
            first_payload["messages"],
        )
        self.assertEqual(
            second_payload["messages"][2],
            {"role": "assistant", "content": invalid_json},
        )
        correction = second_payload["messages"][3]["content"]
        with self.assertRaises(ValidationError) as caught:
            ModelAssessment.model_validate(invalid)
        self.assertIn(str(caught.exception), correction)
        self.assertTrue(
            any(
                "attempt_type=corrective_validation" in record.getMessage()
                for record in self.handler.records
            )
        )

    async def test_repeated_invalid_corrections_are_not_accepted(self):
        invalid_json = json.dumps(
            valid_assessment(
                hard_reject=True,
                hard_reject_reasons=[],
            )
        )

        with self.assertRaises(ReviewError):
            await self.review(
                [
                    lm_response(invalid_json),
                    lm_response(invalid_json),
                    lm_response(invalid_json),
                ]
            )

        self.assertEqual(len(FakeAsyncClient.calls), 3)
        messages = [call[1]["messages"] for call in FakeAsyncClient.calls]
        self.assertEqual(len(messages[0]), 2)
        self.assertEqual(len(messages[1]), 4)
        self.assertEqual(len(messages[2]), 4)
        self.assertEqual(
            messages[2][2]["content"],
            invalid_json,
        )
        self.assertTrue(
            all(
                "response_sha256=" in record.getMessage()
                for record in self.handler.records
                if record.levelno >= logging.WARNING
            )
        )

    async def test_attempt_logging_includes_model_and_generation_settings(self):
        await self.review(
            [lm_response(json.dumps(valid_assessment()), model="actual-model")],
        )

        message = next(r.getMessage() for r in self.handler.records if "lmstudio_attempt" in r.getMessage())
        self.assertIn("configured_model=qwen/qwen3.8-27b", message)
        self.assertIn("response_model=actual-model", message)
        self.assertIn("attempt_type=ordinary", message)
        self.assertIn("attempt=1", message)
        self.assertIn("generation=temperature:0", message)


if __name__ == "__main__":
    unittest.main()
