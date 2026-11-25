import unittest

from pydantic import ValidationError

from lmstudio_client import (
    apply_composition_policy,
    composition_log_fields,
    editorial_log_fields,
    effective_editorial_evidence,
    effective_editorial_score,
    finalize_assessment,
    ignored_editorial_evidence,
    release_restriction,
)
from models import (
    CompositionCorrectability,
    EditorialEvidence,
    HumanActivityReadability,
    MainSubjectVisibility,
    ModelAssessment,
)
from prompt import SYSTEM_PROMPT


NORMALIZED_SYSTEM_PROMPT = " ".join(SYSTEM_PROMPT.split())


def assessment_data(**overrides):
    data = {
        "technical_score": 75,
        "composition_score": 68,
        "commercial_value_score": 35,
        "editorial_value_score": 70,
        "main_subject_type": "discrete",
        "main_subject_visibility": "clear",
        "foreground_dominates_frame": False,
        "composition_defect_severity": "none",
        "composition_correctability": "not_normally_correctable",
        "composition_defects": [],
        "human_activity_readability": "clear",
        "editorial_evidence": ["meaningful_human_activity"],
        "recognizable_people": False,
        "release_sensitive_property": False,
        "prominent_ip_content": False,
        "hard_reject": False,
        "hard_reject_reasons": [],
        "reason": "Visible facts support the assessment.",
        "issues": [],
    }
    data.update(overrides)
    return data


def assessment(**overrides):
    return ModelAssessment.model_validate(assessment_data(**overrides))


class ObservableFieldValidationTests(unittest.TestCase):
    def test_observable_fields_are_required(self):
        for field in (
            "main_subject_type",
            "main_subject_visibility",
            "foreground_dominates_frame",
            "composition_defect_severity",
            "composition_defects",
            "human_activity_readability",
        ):
            data = assessment_data()
            del data[field]
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    ModelAssessment.model_validate(data)

    def test_unknown_enum_values_fail(self):
        for field, value in (
            ("main_subject_visibility", "mostly_visible"),
            ("human_activity_readability", "maybe"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    assessment(**{field: value})

    def test_foreground_dominates_frame_is_strict_boolean(self):
        for value in (0, 1, "true"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    assessment(foreground_dominates_frame=value)


class CompositionObservableCompatibilityTests(unittest.TestCase):
    def test_observables_do_not_apply_a_duplicate_cap(self):
        value = assessment(
            composition_score=80,
            main_subject_visibility="severely_obscured",
            foreground_dominates_frame=True,
        )
        policy = apply_composition_policy(
            composition_correctability=CompositionCorrectability.NOT_NORMALLY_CORRECTABLE,
            raw_composition_score=value.composition_score,
            raw_route_value=70,
            severity=value.composition_defect_severity,
            main_subject_type=value.main_subject_type,
            foreground_dominates_frame=(
                value.foreground_dominates_frame
            ),
        )
        self.assertEqual(policy.effective_composition_score, 80)
        self.assertEqual(policy.effective_route_value, 70)

    def test_composition_log_keeps_observables_and_policy_values(self):
        value = assessment(
            main_subject_visibility="partially_obscured",
            foreground_dominates_frame=True,
            human_activity_readability="incidental",
        )
        policy = apply_composition_policy(
            composition_correctability=CompositionCorrectability.NOT_NORMALLY_CORRECTABLE,
            raw_composition_score=value.composition_score,
            raw_route_value=70,
            severity=value.composition_defect_severity,
            main_subject_type=value.main_subject_type,
            foreground_dominates_frame=(
                value.foreground_dominates_frame
            ),
        )
        self.assertEqual(
            composition_log_fields(value, policy),
            {
                "composition_correctability": "not_normally_correctable",
                "raw_composition_score": 68,
                "effective_composition_score": 68,
                "raw_composition_defect_severity": "none",
                "effective_composition_defect_severity": "none",
                "severity_escalation_reason": "",
                "main_subject_type": "discrete",
                "raw_composition_defects": "",
                "effective_composition_defects": "",
                "ignored_composition_defects": "",
                "raw_route_value": 70,
                "effective_route_value": 70,
                "composition_score_deduction": 0,
                "route_value_deduction": 0,
                "composition_policy_applied": "none",
                "main_subject_visibility": "partially_obscured",
                "foreground_dominates_frame": True,
                "human_activity_readability": "incidental",
            },
        )

    def test_main_subject_type_rejects_unknown_value(self):
        with self.assertRaises(ValidationError):
            assessment(main_subject_type="location")


class HumanEvidenceSanitizationTests(unittest.TestCase):
    def test_non_clear_activity_ignores_human_evidence(self):
        human_evidence = {
            EditorialEvidence.MEANINGFUL_HUMAN_ACTIVITY,
        }
        for readability in (
            HumanActivityReadability.NONE,
            HumanActivityReadability.INCIDENTAL,
        ):
            value = assessment(
                human_activity_readability=readability,
                editorial_evidence=["meaningful_human_activity"],
            )
            with self.subTest(readability=readability):
                self.assertEqual(
                    ignored_editorial_evidence(value),
                    human_evidence,
                )
                self.assertEqual(effective_editorial_evidence(value), set())
                self.assertEqual(effective_editorial_score(value), 49)

    def test_supporting_evidence_remains_after_human_evidence_is_ignored(self):
        value = assessment(
            human_activity_readability="incidental",
            editorial_value_score=90,
            editorial_evidence=[
                "meaningful_human_activity",
                "strong_visual_sense_of_place",
            ],
        )
        self.assertEqual(
            effective_editorial_evidence(value),
            {EditorialEvidence.STRONG_VISUAL_SENSE_OF_PLACE},
        )
        self.assertEqual(effective_editorial_score(value), 69)

    def test_clear_activity_preserves_human_evidence(self):
        value = assessment(
            human_activity_readability="clear",
            editorial_value_score=85,
            editorial_evidence=["meaningful_human_activity"],
        )
        self.assertEqual(ignored_editorial_evidence(value), set())
        self.assertEqual(
            effective_editorial_evidence(value),
            {EditorialEvidence.MEANINGFUL_HUMAN_ACTIVITY},
        )
        self.assertEqual(effective_editorial_score(value), 85)

    def test_ignored_evidence_is_logged(self):
        value = assessment(
            human_activity_readability="incidental",
            editorial_value_score=90,
            editorial_evidence=[
                "meaningful_human_activity",
                "distinctive_architecture",
            ],
        )
        fields = editorial_log_fields(value)
        self.assertEqual(
            fields["effective_editorial_evidence"],
            "distinctive_architecture",
        )
        self.assertEqual(
            fields["ignored_editorial_evidence"],
            "meaningful_human_activity",
        )


class ReleasePropertyPromptTests(unittest.TestCase):
    def test_prompt_keeps_unidentified_exteriors_unrestricted(self):
        self.assertIn(
            "An unidentified residential building exterior remains "
            "unrestricted.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Do not classify an ordinary exterior building as "
            "release-sensitive merely because it is prominent or distinctive.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_lists_narrow_property_conditions(self):
        for condition in (
            "a private interior",
            "clearly visible protected artwork or decorative design",
            "property explicitly identified through trusted supplied metadata "
            "as restricted",
            "a clearly identifiable private business whose identity is "
            "central",
        ):
            with self.subTest(condition=condition):
                self.assertIn(condition, NORMALIZED_SYSTEM_PROMPT)

    def test_prompt_prohibits_socioeconomic_inference_from_wear(self):
        self.assertIn(
            "Do not claim or imply poverty, development level, informality, "
            "socioeconomic status, or culture",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Statements that visible wear suggests socioeconomic or cultural "
            "context are prohibited",
            NORMALIZED_SYSTEM_PROMPT,
        )


class ObservableResponseContractTests(unittest.TestCase):
    def test_observable_fields_are_internal_only(self):
        response = finalize_assessment(assessment(), 70).model_dump()
        self.assertEqual(
            set(response),
            {"verdict", "score", "reason", "issues"},
        )
        for field in (
            "main_subject_visibility",
            "foreground_dominates_frame",
            "human_activity_readability",
        ):
            self.assertNotIn(field, response)

    def test_prominent_branding_remains_editorial_only(self):
        value = assessment(prominent_ip_content=True)
        self.assertEqual(release_restriction(value), "editorial_only")


if __name__ == "__main__":
    unittest.main()
