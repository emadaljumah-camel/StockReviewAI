import unittest
from pathlib import Path

from pydantic import ValidationError

from lmstudio_client import (
    CompositionPolicyResult,
    apply_composition_policy,
    composition_log_fields,
    finalize_assessment,
    finalize_assessment_result,
)
from models import (
    CompositionDefect,
    CompositionDefectSeverity,
    LocationContext,
    MainSubjectType,
    ModelAssessment,
)
from prompt import SYSTEM_PROMPT


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MAIN = PROJECT_ROOT / "StockReviewAI.lrplugin" / "StockReviewAI.lua"
NORMALIZED_SYSTEM_PROMPT = " ".join(SYSTEM_PROMPT.split())


def assessment_data(**overrides):
    data = {
        "technical_score": 75,
        "composition_score": 68,
        "commercial_value_score": 72,
        "editorial_value_score": 60,
        "main_subject_type": "discrete",
        "main_subject_visibility": "clear",
        "foreground_dominates_frame": False,
        "composition_defect_severity": "none",
        "composition_defects": [],
        "human_activity_readability": "none",
        "editorial_evidence": [],
        "recognizable_people": False,
        "release_sensitive_property": False,
        "prominent_ip_content": False,
        "hard_reject": False,
        "hard_reject_reasons": [],
        "reason": "The model describes the composition as strong.",
        "issues": [],
    }
    data.update(overrides)
    return data


def assessment(**overrides):
    return ModelAssessment.model_validate(assessment_data(**overrides))


class CompositionSchemaTests(unittest.TestCase):
    def test_none_with_empty_defects_validates(self):
        value = assessment()
        self.assertEqual(
            value.composition_defect_severity,
            CompositionDefectSeverity.NONE,
        )
        self.assertEqual(value.composition_defects, [])

    def test_none_with_defects_fails(self):
        with self.assertRaises(ValidationError):
            assessment(
                composition_defects=["dominant_distraction"],
            )

    def test_non_none_with_empty_defects_fails(self):
        for severity in ("minor", "major", "severe"):
            with self.subTest(severity=severity):
                with self.assertRaises(ValidationError):
                    assessment(composition_defect_severity=severity)

    def test_unknown_severity_fails(self):
        with self.assertRaises(ValidationError):
            assessment(composition_defect_severity="catastrophic")

    def test_unknown_defect_fails(self):
        with self.assertRaises(ValidationError):
            assessment(
                composition_defect_severity="major",
                composition_defects=["unknown_defect"],
            )

    def test_duplicate_defects_fail(self):
        with self.assertRaises(ValidationError):
            assessment(
                composition_defect_severity="major",
                composition_defects=[
                    "subject_obscured",
                    "subject_obscured",
                ],
            )

    def test_excluded_defects_fail(self):
        for defect in (
            "unintentional_tilt_or_perspective",
            "weak_timing",
        ):
            with self.subTest(defect=defect):
                with self.assertRaises(ValidationError):
                    assessment(
                        composition_defect_severity="minor",
                        composition_defects=[defect],
                    )

    def test_composition_fields_are_internal_only(self):
        response = finalize_assessment(assessment(), 70).model_dump()
        self.assertEqual(
            set(response), {"verdict", "score", "reason", "issues"}
        )
        self.assertNotIn("composition_defect_severity", response)
        self.assertNotIn("composition_defects", response)


class CompositionPolicyTests(unittest.TestCase):
    def policy(
        self,
        severity,
        composition=90,
        route=90,
        defects=(),
        subject_type="discrete",
        foreground_dominates=False,
    ):
        return apply_composition_policy(
            raw_composition_score=composition,
            raw_route_value=route,
            severity=CompositionDefectSeverity(severity),
            main_subject_type=MainSubjectType(subject_type),
            foreground_dominates_frame=foreground_dominates,
            composition_defects=[
                CompositionDefect(defect) for defect in defects
            ],
        )

    def test_none_preserves_composition_and_route(self):
        result = self.policy("none")
        self.assertEqual(result.effective_composition_score, 90)
        self.assertEqual(result.effective_route_value, 90)
        self.assertEqual(result.composition_score_deduction, 0)
        self.assertEqual(result.route_value_deduction, 0)
        self.assertEqual(result.composition_policy_applied, "none")

    def test_minor_preserves_realistic_composition_and_route_scores(self):
        result = self.policy("minor", composition=68, route=70)
        self.assertEqual(result.effective_composition_score, 68)
        self.assertEqual(result.effective_route_value, 70)
        self.assertEqual(result.composition_score_deduction, 0)
        self.assertEqual(result.route_value_deduction, 0)
        self.assertEqual(result.composition_policy_applied, "minor")

    def test_minor_only_caps_unrealistically_high_composition(self):
        result = self.policy("minor")
        self.assertEqual(result.effective_composition_score, 69)
        self.assertEqual(result.effective_route_value, 90)
        self.assertEqual(result.composition_score_deduction, 21)
        self.assertEqual(result.route_value_deduction, 0)

    def test_major_caps_composition_and_route(self):
        result = self.policy("major")
        self.assertEqual(result.effective_composition_score, 49)
        self.assertEqual(result.effective_route_value, 64)

    def test_severe_caps_composition_and_route(self):
        result = self.policy("severe")
        self.assertEqual(result.effective_composition_score, 29)
        self.assertEqual(result.effective_route_value, 49)

    def test_major_and_severe_deductions_apply_below_caps(self):
        for severity, composition, route, expected_composition, expected_route in (
            ("minor", 40, 30, 40, 30),
            ("major", 40, 30, 20, 20),
            ("severe", 20, 30, 0, 10),
        ):
            with self.subTest(severity=severity):
                result = self.policy(severity, composition, route)
                self.assertEqual(
                    result.effective_composition_score, expected_composition
                )
                self.assertEqual(
                    result.effective_route_value, expected_route
                )

    def test_deductions_clamp_at_zero(self):
        result = self.policy("severe", composition=5, route=10)
        self.assertEqual(result.effective_composition_score, 0)
        self.assertEqual(result.effective_route_value, 0)
        self.assertEqual(result.composition_score_deduction, 5)
        self.assertEqual(result.route_value_deduction, 10)

    def test_three_distinct_minor_defects_escalate_to_major(self):
        result = self.policy(
            "minor",
            composition=68,
            route=70,
            defects=(
                "subject_obscured",
                "dominant_distraction",
                "poor_visual_balance",
            ),
        )
        self.assertEqual(
            result.raw_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MAJOR,
        )
        self.assertEqual(
            result.severity_escalation_reason,
            "three_or_more_distinct_defects",
        )
        self.assertEqual(result.effective_composition_score, 48)
        self.assertEqual(result.effective_route_value, 60)
        self.assertEqual(result.composition_policy_applied, "major")

    def test_two_distinct_minor_defects_remain_minor(self):
        result = self.policy(
            "minor",
            composition=68,
            route=70,
            defects=("subject_too_small", "poor_visual_balance"),
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )
        self.assertIsNone(result.severity_escalation_reason)

    def test_duplicate_defects_do_not_inflate_policy_count(self):
        result = self.policy(
            "minor",
            defects=(
                "subject_too_small",
                "subject_too_small",
                "poor_visual_balance",
            ),
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )

    def test_discrete_small_subject_with_dead_space_escalates_to_major(self):
        result = self.policy(
            "minor",
            composition=68,
            route=70,
            defects=("subject_too_small", "excessive_dead_space"),
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MAJOR,
        )
        self.assertEqual(
            result.severity_escalation_reason,
            "discrete_subject_too_small_with_excessive_dead_space",
        )
        self.assertEqual(result.effective_composition_score, 48)
        self.assertEqual(result.effective_route_value, 60)

    def test_ignored_only_defect_normalizes_environmental_severity(self):
        for severity in ("minor", "major"):
            with self.subTest(severity=severity):
                result = self.policy(
                    severity,
                    composition=68,
                    route=70,
                    defects=("subject_too_small",),
                    subject_type="environmental_scene",
                )
                self.assertEqual(
                    result.effective_composition_defect_severity,
                    CompositionDefectSeverity.NONE,
                )
                self.assertEqual(
                    result.severity_escalation_reason,
                    "all_composition_defects_ignored_for_subject_type",
                )
                self.assertEqual(result.effective_composition_score, 68)
                self.assertEqual(result.effective_route_value, 70)
                self.assertEqual(result.composition_score_deduction, 0)
                self.assertEqual(result.route_value_deduction, 0)

    def test_abstract_ignored_only_defect_normalizes_to_none(self):
        result = self.policy(
            "minor",
            defects=("subject_too_small",),
            subject_type="pattern_or_abstract",
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.NONE,
        )
        self.assertEqual(result.effective_composition_defects, ())
        self.assertEqual(
            result.ignored_composition_defects,
            (CompositionDefect.SUBJECT_TOO_SMALL,),
        )

    def test_three_defect_rule_counts_only_effective_defects(self):
        result = self.policy(
            "minor",
            defects=(
                "subject_too_small",
                "excessive_dead_space",
                "poor_visual_balance",
            ),
            subject_type="environmental_scene",
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )
        self.assertEqual(
            result.effective_composition_defects,
            (
                CompositionDefect.EXCESSIVE_DEAD_SPACE,
                CompositionDefect.POOR_VISUAL_BALANCE,
            ),
        )
        self.assertIsNone(result.severity_escalation_reason)

    def test_corroborated_foreground_dominance_escalates_all_subject_types(self):
        for subject_type in MainSubjectType:
            with self.subTest(subject_type=subject_type):
                result = self.policy(
                    "minor",
                    composition=68,
                    route=70,
                    defects=(
                        "dominant_distraction",
                        "poor_visual_balance",
                    ),
                    subject_type=subject_type.value,
                    foreground_dominates=True,
                )
                self.assertEqual(
                    result.effective_composition_defect_severity,
                    CompositionDefectSeverity.MAJOR,
                )
                self.assertEqual(
                    result.severity_escalation_reason,
                    "foreground_dominance_with_multiple_effective_defects",
                )
                self.assertEqual(result.effective_composition_score, 48)
                self.assertEqual(result.effective_route_value, 60)

    def test_foreground_rule_requires_dominance(self):
        result = self.policy(
            "minor",
            defects=("dominant_distraction", "poor_visual_balance"),
            foreground_dominates=False,
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )

    def test_foreground_rule_requires_dominant_distraction(self):
        result = self.policy(
            "minor",
            defects=("excessive_dead_space", "poor_visual_balance"),
            foreground_dominates=True,
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )

    def test_foreground_rule_requires_an_additional_effective_defect(self):
        result = self.policy(
            "minor",
            defects=("dominant_distraction",),
            foreground_dominates=True,
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )

    def test_ignored_defect_cannot_corroborate_foreground_dominance(self):
        result = self.policy(
            "minor",
            defects=("dominant_distraction", "subject_too_small"),
            subject_type="environmental_scene",
            foreground_dominates=True,
        )
        self.assertEqual(
            result.effective_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )
        self.assertEqual(
            result.effective_composition_defects,
            (CompositionDefect.DOMINANT_DISTRACTION,),
        )
        self.assertIsNone(result.severity_escalation_reason)

    def test_all_ignored_normalization_reason_is_logged(self):
        value = assessment(
            main_subject_type="environmental_scene",
            composition_defect_severity="major",
            composition_defects=["subject_too_small"],
        )
        policy = apply_composition_policy(
            raw_composition_score=value.composition_score,
            raw_route_value=70,
            severity=value.composition_defect_severity,
            main_subject_type=value.main_subject_type,
            foreground_dominates_frame=(
                value.foreground_dominates_frame
            ),
            composition_defects=value.composition_defects,
        )
        fields = composition_log_fields(value, policy)
        self.assertEqual(
            fields["severity_escalation_reason"],
            "all_composition_defects_ignored_for_subject_type",
        )
        self.assertEqual(
            fields["raw_composition_defects"], "subject_too_small"
        )
        self.assertEqual(fields["effective_composition_defects"], "")
        self.assertEqual(
            fields["ignored_composition_defects"], "subject_too_small"
        )

    def test_existing_major_and_severe_severity_are_preserved(self):
        defects = (
            "subject_too_small",
            "excessive_dead_space",
            "poor_visual_balance",
        )
        for severity in ("major", "severe"):
            with self.subTest(severity=severity):
                result = self.policy(severity, defects=defects)
                self.assertEqual(
                    result.effective_composition_defect_severity,
                    CompositionDefectSeverity(severity),
                )
                self.assertIsNone(result.severity_escalation_reason)

    def test_location_context_cannot_remove_major_caps(self):
        value = assessment(
            composition_defect_severity="major",
            composition_defects=["poor_visual_balance"],
            editorial_value_score=72,
            editorial_evidence=["strong_visual_sense_of_place"],
            prominent_ip_content=True,
        )
        for context in (
            LocationContext(city="Verified City"),
            LocationContext(sublocation="Verified Sublocation"),
        ):
            with self.subTest(context=context):
                finalization = finalize_assessment_result(
                    value, 70, context
                )
                policy = finalization.composition_policy
                self.assertEqual(policy.raw_route_value, 72)
                self.assertEqual(policy.effective_composition_score, 48)
                self.assertEqual(policy.effective_route_value, 62)

    def test_strong_editorial_evidence_cannot_remove_major_caps(self):
        finalization = finalize_assessment_result(
            assessment(
                composition_defect_severity="major",
                composition_defects=["awkward_overlaps"],
                editorial_value_score=90,
                editorial_evidence=["public_event_or_ceremony"],
                prominent_ip_content=True,
            ),
            70,
        )
        self.assertEqual(
            finalization.composition_policy.effective_route_value, 64
        )

    def test_commercial_route_uses_composition_policy(self):
        finalization = finalize_assessment_result(
            assessment(
                commercial_value_score=90,
                composition_defect_severity="major",
                composition_defects=["dominant_distraction"],
            ),
            70,
        )
        self.assertEqual(finalization.selected_market_basis, "commercial")
        self.assertEqual(finalization.composition_policy.raw_route_value, 90)
        self.assertEqual(
            finalization.composition_policy.effective_route_value, 64
        )

    def test_editorial_route_uses_composition_policy(self):
        finalization = finalize_assessment_result(
            assessment(
                editorial_value_score=90,
                editorial_evidence=["unusual_documentary_condition"],
                prominent_ip_content=True,
                composition_defect_severity="severe",
                composition_defects=["subject_obscured"],
            ),
            70,
        )
        self.assertEqual(finalization.selected_market_basis, "editorial")
        self.assertEqual(finalization.composition_policy.raw_route_value, 90)
        self.assertEqual(
            finalization.composition_policy.effective_route_value, 49
        )

    def test_hard_rejection_still_overrides_score(self):
        result = finalize_assessment(
            assessment(
                technical_score=100,
                composition_score=100,
                commercial_value_score=100,
                hard_reject=True,
                hard_reject_reasons=["missed_focus"],
            ),
            0,
        )
        self.assertEqual(result.verdict, "NO")

    def test_release_sensitive_editorial_minimum_remains_enforced(self):
        result = finalize_assessment(
            assessment(
                technical_score=100,
                composition_score=100,
                editorial_value_score=69,
                editorial_evidence=["public_event_or_ceremony"],
                prominent_ip_content=True,
            ),
            0,
        )
        self.assertEqual(result.verdict, "NO")

    def test_log_fields_use_the_supplied_policy_result(self):
        value = assessment(
            composition_defect_severity="major",
            composition_defects=[
                "subject_obscured",
                "dominant_distraction",
            ],
        )
        policy = self.policy(
            "major",
            68,
            72,
            defects=("subject_obscured", "dominant_distraction"),
        )
        fields = composition_log_fields(value, policy)
        self.assertEqual(fields["raw_composition_score"], 68)
        self.assertEqual(fields["effective_composition_score"], 48)
        self.assertEqual(
            fields["raw_composition_defect_severity"], "major"
        )
        self.assertEqual(
            fields["effective_composition_defect_severity"], "major"
        )
        self.assertEqual(fields["severity_escalation_reason"], "")
        self.assertEqual(
            fields["raw_composition_defects"],
            "subject_obscured; dominant_distraction",
        )
        self.assertEqual(
            fields["effective_composition_defects"],
            "subject_obscured; dominant_distraction",
        )
        self.assertEqual(fields["ignored_composition_defects"], "")
        self.assertEqual(fields["main_subject_type"], "discrete")
        self.assertEqual(fields["raw_route_value"], 72)
        self.assertEqual(fields["effective_route_value"], 62)
        self.assertEqual(fields["composition_score_deduction"], 20)
        self.assertEqual(fields["route_value_deduction"], 10)
        self.assertEqual(fields["composition_policy_applied"], "major")


class CompositionExplanationTests(unittest.TestCase):
    def test_major_penalty_appends_readable_authoritative_reason(self):
        response = finalize_assessment(
            assessment(
                composition_defect_severity="major",
                composition_defects=[
                    "subject_obscured",
                    "dominant_distraction",
                    "cluttered_visual_hierarchy",
                ],
            ),
            70,
        )
        self.assertIn("Major composition penalty applied", response.reason)
        self.assertIn("an obscured subject", response.reason)
        self.assertIn("a dominant distraction", response.reason)
        self.assertNotIn("subject_obscured", response.reason)

    def test_severe_penalty_appends_readable_authoritative_reason(self):
        response = finalize_assessment(
            assessment(
                composition_defect_severity="severe",
                composition_defects=["subject_too_small"],
            ),
            70,
        )
        self.assertIn("Severe composition penalty applied", response.reason)
        self.assertIn("a subject that is too small", response.reason)
        self.assertNotIn("subject_too_small", response.reason)

    def test_escalated_minor_uses_authoritative_major_explanation(self):
        response = finalize_assessment(
            assessment(
                composition_defect_severity="minor",
                composition_defects=[
                    "subject_too_small",
                    "excessive_dead_space",
                    "poor_visual_balance",
                ],
            ),
            70,
        )
        self.assertIn("Major composition penalty applied", response.reason)

    def test_ignored_defect_is_absent_from_authoritative_explanation(self):
        response = finalize_assessment(
            assessment(
                main_subject_type="environmental_scene",
                composition_defect_severity="major",
                composition_defects=[
                    "subject_too_small",
                    "subject_obscured",
                ],
            ),
            70,
        )
        self.assertIn("an obscured subject", response.reason)
        self.assertNotIn("a subject that is too small", response.reason)

    def test_public_response_still_contains_exactly_four_fields(self):
        response = finalize_assessment(
            assessment(
                composition_defect_severity="major",
                composition_defects=["accidental_crop"],
            ),
            70,
        ).model_dump()
        self.assertEqual(
            set(response), {"verdict", "score", "reason", "issues"}
        )


class CompositionPromptContractTests(unittest.TestCase):
    def test_prompt_contains_every_severity_and_defect(self):
        for subject_type in MainSubjectType:
            with self.subTest(subject_type=subject_type):
                self.assertIn(subject_type.value, SYSTEM_PROMPT)
        for severity in CompositionDefectSeverity:
            with self.subTest(severity=severity):
                self.assertIn(severity.value, SYSTEM_PROMPT)
        for defect in CompositionDefect:
            with self.subTest(defect=defect):
                self.assertIn(defect.value, SYSTEM_PROMPT)

    def test_prompt_protects_normally_fixable_issues(self):
        self.assertIn(
            "Do not assign a major or severe composition defect for a "
            "problem that can be corrected through a reasonable crop, "
            "straightening, perspective correction, or another normal "
            "Lightroom adjustment",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Do not assume an extreme crop is harmless.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_preserves_intentional_and_documentary_composition(self):
        self.assertIn(
            "Unconventional composition is not automatically defective.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_distinguishes_environmental_and_discrete_subjects(self):
        self.assertIn(
            "Do not classify an image as discrete merely because it "
            "contains an object, building, sign, or person.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Use subject_too_small only for a discrete main subject.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "When a discrete subject is too small and surrounded by "
            "excessive noncontributing space, composition_defect_severity "
            "must be at least major.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Environmental areas are not dead space when their textures, "
            "lines, scale, light, or spatial relationships establish "
            "atmosphere, depth, or sense of place.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "A street, alley, wall, or ground surface may be part of the "
            "subject rather than empty space.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Judge whether the element contributes value, not merely how "
            "much frame space it occupies.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_requires_consistent_severity_and_scores(self):
        self.assertIn(
            "The composition score, defect severity, defect list, reason, "
            "and issues must agree.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_classifies_materially_obstructive_areas_as_major(self):
        self.assertIn(
            "When multiple large irrelevant areas substantially reduce "
            "access to the subject or weaken the photograph's visual "
            "hierarchy, classify the defect as major even if the subject "
            "remains technically visible.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_requires_corroborated_foreground_dominance(self):
        self.assertIn(
            "Foreground dominance is not automatically a defect.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "when foreground_dominates_frame is true, "
            "dominant_distraction is present, and at least one additional "
            "distinct composition defect applies",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "An ignored subject_too_small classification must not trigger "
            "this rule.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Classify framing as major only when large elements provide "
            "little visual or documentary value and substantially bury, "
            "obstruct, or compete with the intended subject.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Minor weaknesses must not be used for framing that remains "
            "visually effective overall.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_requires_three_defects_to_be_at_least_major(self):
        self.assertIn(
            "When three or more distinct composition defects apply, "
            "composition defect severity must be at least major.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Minor is reserved for weaknesses that do not materially "
            "reduce stock usefulness.",
            NORMALIZED_SYSTEM_PROMPT,
        )


class CompositionRegressionTests(unittest.TestCase):
    def test_general_major_framing_regression(self):
        finalization = finalize_assessment_result(
            assessment(
                technical_score=75,
                composition_score=68,
                composition_defect_severity="major",
                composition_defects=[
                    "subject_obscured",
                    "dominant_distraction",
                    "cluttered_visual_hierarchy",
                ],
                commercial_value_score=72,
                editorial_value_score=60,
                editorial_evidence=[],
            ),
            70,
        )
        self.assertEqual(
            finalization.composition_policy,
            CompositionPolicyResult(
                raw_composition_score=68,
                effective_composition_score=48,
                raw_route_value=72,
                effective_route_value=62,
                raw_composition_defect_severity=(
                    CompositionDefectSeverity.MAJOR
                ),
                effective_composition_defect_severity=(
                    CompositionDefectSeverity.MAJOR
                ),
                severity_escalation_reason=None,
                raw_composition_defects=(
                    CompositionDefect.SUBJECT_OBSCURED,
                    CompositionDefect.DOMINANT_DISTRACTION,
                    CompositionDefect.CLUTTERED_VISUAL_HIERARCHY,
                ),
                effective_composition_defects=(
                    CompositionDefect.SUBJECT_OBSCURED,
                    CompositionDefect.DOMINANT_DISTRACTION,
                    CompositionDefect.CLUTTERED_VISUAL_HIERARCHY,
                ),
                ignored_composition_defects=(),
                composition_score_deduction=20,
                route_value_deduction=10,
                composition_policy_applied="major",
            ),
        )
        self.assertEqual(finalization.response.score, 63)
        self.assertEqual(finalization.response.verdict, "NO")

    def test_discrete_small_subject_regression_escalates_to_major(self):
        finalization = finalize_assessment_result(
            assessment(
                technical_score=75,
                composition_score=68,
                composition_defect_severity="minor",
                composition_defects=[
                    "subject_too_small",
                    "excessive_dead_space",
                ],
                commercial_value_score=70,
                editorial_value_score=60,
                editorial_evidence=[],
            ),
            70,
        )
        policy = finalization.composition_policy
        self.assertEqual(policy.effective_composition_score, 48)
        self.assertEqual(policy.effective_route_value, 60)
        self.assertEqual(
            policy.raw_composition_defect_severity,
            CompositionDefectSeverity.MINOR,
        )
        self.assertEqual(
            policy.effective_composition_defect_severity,
            CompositionDefectSeverity.MAJOR,
        )
        self.assertEqual(finalization.response.score, 62)
        self.assertEqual(finalization.response.verdict, "NO")

    def test_minor_dsc_9251_style_regression_remains_a_pick(self):
        finalization = finalize_assessment_result(
            assessment(
                technical_score=75,
                composition_score=68,
                main_subject_type="environmental_scene",
                composition_defect_severity="minor",
                composition_defects=[
                    "subject_too_small",
                    "excessive_dead_space",
                ],
                commercial_value_score=70,
                editorial_value_score=60,
                editorial_evidence=[],
            ),
            70,
        )
        policy = finalization.composition_policy
        self.assertEqual(policy.effective_composition_score, 68)
        self.assertEqual(policy.effective_route_value, 70)
        self.assertEqual(
            policy.effective_composition_defects,
            (CompositionDefect.EXCESSIVE_DEAD_SPACE,),
        )
        self.assertEqual(
            policy.ignored_composition_defects,
            (CompositionDefect.SUBJECT_TOO_SMALL,),
        )
        self.assertEqual(policy.composition_score_deduction, 0)
        self.assertEqual(policy.route_value_deduction, 0)
        self.assertEqual(finalization.response.score, 71)
        self.assertEqual(finalization.response.verdict, "YES")

    def test_dsc_9342_style_foreground_regression_is_stable_reject(self):
        value = assessment(
            technical_score=75,
            composition_score=68,
            main_subject_type="environmental_scene",
            foreground_dominates_frame=True,
            composition_defect_severity="minor",
            composition_defects=[
                "dominant_distraction",
                "poor_visual_balance",
            ],
            commercial_value_score=70,
            editorial_value_score=60,
            editorial_evidence=[],
        )
        finalization = finalize_assessment_result(value, 72)
        policy = finalization.composition_policy
        fields = composition_log_fields(value, policy)
        self.assertEqual(policy.effective_composition_score, 48)
        self.assertEqual(policy.effective_route_value, 60)
        self.assertEqual(
            policy.effective_composition_defect_severity,
            CompositionDefectSeverity.MAJOR,
        )
        self.assertEqual(
            fields["severity_escalation_reason"],
            "foreground_dominance_with_multiple_effective_defects",
        )
        self.assertEqual(finalization.response.score, 62)
        self.assertEqual(finalization.response.verdict, "NO")

    def test_operational_failure_path_precedes_lightroom_flag_write(self):
        source = PLUGIN_MAIN.read_text(encoding="utf-8")
        review_call = source.index("AiClient.reviewPreview")
        failed_result = source.index("if not result then", review_call)
        apply_flag = source.index("applyFlag", failed_result)
        self.assertLess(review_call, failed_result)
        self.assertLess(failed_result, apply_flag)


if __name__ == "__main__":
    unittest.main()
