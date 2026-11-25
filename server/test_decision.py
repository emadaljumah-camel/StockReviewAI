import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from lmstudio_client import (
    ReviewError,
    _build_payload,
    finalize_assessment,
    release_restriction,
    select_route,
)
from main import app
from models import (
    HardRejectReason,
    LMStudioSettings,
    ModelAssessment,
    ReviewRequest,
    ReviewResponse,
)
from prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT


def assessment_data(**overrides):
    data = {
        "technical_score": 80,
        "composition_score": 70,
        "commercial_value_score": 90,
        "editorial_value_score": 60,
        "main_subject_type": "discrete",
        "main_subject_visibility": "clear",
        "foreground_dominates_frame": False,
        "composition_defect_severity": "none",
        "composition_correctability": "not_normally_correctable",
        "composition_defects": [],
        "human_activity_readability": "clear",
        "editorial_evidence": [],
        "recognizable_people": False,
        "release_sensitive_property": False,
        "prominent_ip_content": False,
        "hard_reject": False,
        "hard_reject_reasons": [],
        "reason": "Usable stock photograph.",
        "issues": [],
    }
    data.update(overrides)
    return data


def assessment(**overrides):
    return ModelAssessment.model_validate(assessment_data(**overrides))


class RouteSelectionTests(unittest.TestCase):
    def test_release_sensitive_content_is_editorial_only(self):
        for field in (
            "recognizable_people",
            "release_sensitive_property",
            "prominent_ip_content",
        ):
            with self.subTest(field=field):
                value = assessment(**{field: True})
                self.assertEqual(
                    release_restriction(value),
                    "editorial_only",
                )
                self.assertEqual(select_route(value)[0], "editorial")

    def test_recognizable_people_force_editorial(self):
        route, value = select_route(
            assessment(
                recognizable_people=True,
                commercial_value_score=100,
                editorial_value_score=40,
            )
        )
        self.assertEqual((route, value), ("editorial", 40))

    def test_release_sensitive_property_forces_editorial(self):
        route, value = select_route(
            assessment(
                release_sensitive_property=True,
                commercial_value_score=100,
                editorial_value_score=45,
            )
        )
        self.assertEqual((route, value), ("editorial", 45))

    def test_prominent_ip_content_forces_editorial(self):
        route, value = select_route(
            assessment(
                prominent_ip_content=True,
                commercial_value_score=100,
                editorial_value_score=50,
            )
        )
        self.assertEqual((route, value), ("editorial", 49))

    def test_unrestricted_assessment_selects_higher_market_score(self):
        commercial_assessment = assessment(
            commercial_value_score=81,
            editorial_value_score=80,
        )
        editorial_assessment = assessment(
            commercial_value_score=80,
            editorial_value_score=81,
            editorial_evidence=["meaningful_human_activity"],
        )
        commercial = select_route(commercial_assessment)
        editorial = select_route(editorial_assessment)

        self.assertEqual(
            release_restriction(commercial_assessment),
            "unrestricted",
        )
        self.assertEqual(
            release_restriction(editorial_assessment),
            "unrestricted",
        )
        self.assertEqual(commercial, ("commercial", 81))
        self.assertEqual(editorial, ("editorial", 81))

    def test_market_score_tie_selects_commercial(self):
        self.assertEqual(
            select_route(
                assessment(
                    commercial_value_score=75,
                    editorial_value_score=75,
                    editorial_evidence=["meaningful_human_activity"],
                )
            ),
            ("commercial", 75),
        )


class DecisionTests(unittest.TestCase):
    def test_exact_integer_weighting_and_half_up_rounding(self):
        result = finalize_assessment(
            assessment(
                technical_score=80,
                composition_score=70,
                commercial_value_score=90,
            ),
            0,
        )
        self.assertEqual(result.score, 82)  # 81.50 rounds upward.

    def test_values_around_half_boundary(self):
        below = finalize_assessment(
            assessment(
                technical_score=1,
                composition_score=2,
                commercial_value_score=4,
                editorial_value_score=0,
            ),
            0,
        )
        exact = finalize_assessment(
            assessment(
                technical_score=0,
                composition_score=2,
                commercial_value_score=0,
                editorial_value_score=0,
            ),
            0,
        )
        above = finalize_assessment(
            assessment(
                technical_score=1,
                composition_score=4,
                commercial_value_score=3,
                editorial_value_score=0,
            ),
            0,
        )
        self.assertEqual(below.score, 2)  # 2.45
        self.assertEqual(exact.score, 1)  # 0.50
        self.assertEqual(above.score, 3)  # 2.55

    def test_score_bounds(self):
        low = finalize_assessment(
            assessment(
                technical_score=0,
                composition_score=0,
                commercial_value_score=0,
                editorial_value_score=0,
            ),
            0,
        )
        high = finalize_assessment(
            assessment(
                technical_score=100,
                composition_score=100,
                commercial_value_score=100,
                editorial_value_score=100,
            ),
            100,
        )
        self.assertEqual((low.score, high.score), (0, 100))

    def test_threshold_behavior(self):
        passing = assessment(
            technical_score=70,
            composition_score=70,
            commercial_value_score=70,
            editorial_value_score=70,
        )
        self.assertEqual(finalize_assessment(passing, 70).verdict, "YES")
        self.assertEqual(finalize_assessment(passing, 71).verdict, "NO")

    def test_hard_reject_overrides_passing_score(self):
        result = finalize_assessment(
            assessment(
                technical_score=100,
                composition_score=100,
                commercial_value_score=100,
                editorial_value_score=100,
                hard_reject=True,
                hard_reject_reasons=["missed_focus"],
            ),
            70,
        )
        self.assertEqual((result.verdict, result.score), ("NO", 100))

    def test_threshold_must_be_strict_integer_in_range(self):
        valid = assessment()
        for invalid in (-1, 101, "70", 70.0, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ReviewError):
                    finalize_assessment(valid, invalid)

    def test_public_response_has_exact_fields(self):
        result = finalize_assessment(assessment(), 70)
        response = result.model_dump()
        self.assertEqual(
            set(response),
            {"verdict", "score", "reason", "issues"},
        )
        self.assertNotIn("release_restriction", response)
        self.assertNotIn("selected_market_basis", response)
        self.assertNotIn("editorial_evidence", response)


class PromptCalibrationContractTests(unittest.TestCase):
    def test_score_bands_and_high_score_standard_are_present(self):
        for instruction in (
            "0-29: Unusable or essentially no market value.",
            "30-49: Weak.",
            "50-59: Ordinary or limited.",
            "60-69: Decent but below the Pick standard.",
            "70-79: Strong stock candidate.",
            "80-89: Very strong.",
            "90-100: Exceptional and uncommon.",
            "A score of 70 is not average",
            "Scores from 80 to 100 must be used sparingly.",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, SYSTEM_PROMPT)

    def test_descriptive_language_is_calibrated(self):
        self.assertIn(
            "Moderate editorial value must receive an "
            "editorial_value_score below 70.",
            SYSTEM_PROMPT,
        )
        self.assertIn(
            "Limited commercial appeal must receive a "
            "commercial_value_score below 70.",
            SYSTEM_PROMPT,
        )

    def test_generic_alley_and_missing_context_rules_are_present(self):
        self.assertIn(
            "An unidentified generic alley without people or supplied "
            "context is not automatically strong editorial content.",
            SYSTEM_PROMPT,
        )
        self.assertIn(
            "Do not invent historical, cultural, geographic, newsworthy, "
            "or social significance.",
            SYSTEM_PROMPT,
        )

    def test_preview_uncertainty_rules_are_present(self):
        self.assertNotIn("1280-pixel", SYSTEM_PROMPT)
        self.assertIn(
            "configured size and quality settings",
            SYSTEM_PROMPT,
        )
        self.assertIn(
            "do not assume that RAW shadow or highlight recovery will succeed.",
            SYSTEM_PROMPT,
        )
        self.assertIn(
            "Use hard rejection only for defects that are unmistakably fatal "
            "in the supplied preview.",
            SYSTEM_PROMPT,
        )


class AssessmentValidationTests(unittest.TestCase):
    def assert_invalid(self, **overrides):
        with self.assertRaises(ValidationError):
            ModelAssessment.model_validate(assessment_data(**overrides))

    def test_score_fields_are_strict_integers(self):
        score_fields = (
            "technical_score",
            "composition_score",
            "commercial_value_score",
            "editorial_value_score",
        )
        for field in score_fields:
            for invalid in ("82", 82.0, True):
                with self.subTest(field=field, invalid=invalid):
                    self.assert_invalid(**{field: invalid})

    def test_boolean_fields_are_strict(self):
        boolean_fields = (
            "recognizable_people",
            "release_sensitive_property",
            "prominent_ip_content",
            "hard_reject",
        )
        for field in boolean_fields:
            for invalid in (0, 1, "false"):
                with self.subTest(field=field, invalid=invalid):
                    self.assert_invalid(**{field: invalid})

    def test_reason_must_have_non_whitespace_text(self):
        for invalid in ("", "   "):
            with self.subTest(invalid=invalid):
                self.assert_invalid(reason=invalid)

    def test_issues_must_have_non_whitespace_text(self):
        for invalid in ("", "   "):
            with self.subTest(invalid=invalid):
                self.assert_invalid(issues=[invalid])

    def test_unknown_hard_reject_reason_fails(self):
        self.assert_invalid(
            hard_reject=True,
            hard_reject_reasons=["low_market_value"],
        )

    def test_hard_reject_requires_reasons(self):
        self.assert_invalid(hard_reject=True, hard_reject_reasons=[])

    def test_non_hard_reject_forbids_reasons(self):
        self.assert_invalid(
            hard_reject=False,
            hard_reject_reasons=["missed_focus"],
        )

    def test_server_owned_fields_are_forbidden(self):
        for field, value in (
            ("verdict", "YES"),
            ("score", 80),
            ("final_score", 80),
            ("route", "commercial"),
        ):
            with self.subTest(field=field):
                self.assert_invalid(**{field: value})


class SchemaContractTests(unittest.TestCase):
    def test_generated_schema_matches_model_contract(self):
        self.assertEqual(
            set(RESPONSE_SCHEMA["properties"]),
            set(ModelAssessment.model_fields),
        )
        self.assertEqual(
            set(RESPONSE_SCHEMA["required"]),
            set(ModelAssessment.model_fields),
        )
        self.assertIs(RESPONSE_SCHEMA["additionalProperties"], False)

        reason_ref = RESPONSE_SCHEMA["properties"]["hard_reject_reasons"][
            "items"
        ]["$ref"]
        definition_name = reason_ref.rsplit("/", 1)[-1]
        self.assertEqual(
            RESPONSE_SCHEMA["$defs"][definition_name]["enum"],
            [reason.value for reason in HardRejectReason],
        )

    def test_lmstudio_payload_uses_generated_schema(self):
        request = ReviewRequest(
            filename="photo.jpg",
            image_base64="/9j/",
            threshold=70,
        )
        payload = _build_payload(request, LMStudioSettings())
        actual_schema = payload["response_format"]["json_schema"]["schema"]
        self.assertEqual(actual_schema, RESPONSE_SCHEMA)


class EndpointCompatibilityTests(unittest.TestCase):
    def test_lightroom_json_number_threshold_is_accepted(self):
        response_result = ReviewResponse(
            verdict="YES",
            score=80,
            reason="Suitable photograph.",
            issues=[],
        )
        mocked_review = AsyncMock(return_value=response_result)

        with (
            patch("main.review_with_lmstudio", mocked_review),
            patch("main.JOB_STORE.mark_running_jobs_interrupted", return_value=[]),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/review",
                    json={
                        "filename": "photo.jpg",
                        "image_base64": "/9j/",
                        "threshold": 70,
                    },
                )

        self.assertEqual(response.status_code, 200)
        request = mocked_review.await_args.args[0]
        self.assertIs(type(request.threshold), int)
        self.assertEqual(request.threshold, 70)
        self.assertEqual(
            set(response.json()),
            {"verdict", "score", "reason", "issues"},
        )

    def test_string_threshold_is_rejected_at_endpoint(self):
        mocked_review = AsyncMock()

        with (
            patch("main.review_with_lmstudio", mocked_review),
            patch("main.JOB_STORE.mark_running_jobs_interrupted", return_value=[]),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/review",
                    json={
                        "filename": "photo.jpg",
                        "image_base64": "/9j/",
                        "threshold": "70",
                    },
                )

        self.assertEqual(response.status_code, 422)
        mocked_review.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
