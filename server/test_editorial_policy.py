import unittest

from pydantic import ValidationError

from lmstudio_client import (
    STRONG_EDITORIAL_EVIDENCE,
    SUPPORTING_EDITORIAL_EVIDENCE,
    EditorialPolicyResult,
    VerifiedLocationState,
    editorial_log_fields,
    effective_editorial_score,
    evaluate_editorial_policy,
    finalize_assessment,
    finalize_assessment_result,
    release_restriction,
    select_route,
)
from models import (
    EditorialEvidence,
    LocationContext,
    LocationContextLevel,
    ModelAssessment,
)
from prompt import SYSTEM_PROMPT


NORMALIZED_SYSTEM_PROMPT = " ".join(SYSTEM_PROMPT.split())


def assessment_data(**overrides):
    data = {
        "technical_score": 80,
        "composition_score": 80,
        "commercial_value_score": 60,
        "editorial_value_score": 80,
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
        "reason": "Visible facts support the assessment.",
        "issues": [],
    }
    data.update(overrides)
    return data


def assessment(**overrides):
    return ModelAssessment.model_validate(assessment_data(**overrides))


class EditorialEvidenceScoreTests(unittest.TestCase):
    def test_empty_evidence_caps_editorial_score_at_49(self):
        self.assertEqual(
            effective_editorial_score(
                assessment(editorial_value_score=90)
            ),
            49,
        )

    def test_empty_evidence_does_not_raise_lower_raw_score(self):
        self.assertEqual(
            effective_editorial_score(
                assessment(editorial_value_score=40)
            ),
            40,
        )

    def test_each_supporting_category_caps_score_at_69(self):
        for evidence in (
            "distinctive_architecture",
            "strong_visual_sense_of_place",
        ):
            with self.subTest(evidence=evidence):
                self.assertEqual(
                    effective_editorial_score(
                        assessment(
                            editorial_value_score=90,
                            editorial_evidence=[evidence],
                        )
                    ),
                    69,
                )

    def test_combined_supporting_evidence_still_caps_at_69(self):
        self.assertEqual(
            effective_editorial_score(
                assessment(
                    editorial_value_score=90,
                    editorial_evidence=[
                        "distinctive_architecture",
                        "strong_visual_sense_of_place",
                    ],
                )
            ),
            69,
        )

    def test_strong_evidence_allows_raw_score(self):
        for evidence in STRONG_EDITORIAL_EVIDENCE:
            with self.subTest(evidence=evidence):
                self.assertEqual(
                    effective_editorial_score(
                        assessment(
                            editorial_value_score=85,
                            editorial_evidence=[evidence],
                        )
                    ),
                    85,
                )

    def test_strong_and_supporting_evidence_allow_raw_score(self):
        self.assertEqual(
            effective_editorial_score(
                assessment(
                    editorial_value_score=88,
                    editorial_evidence=[
                        "meaningful_human_activity",
                        "distinctive_architecture",
                    ],
                )
            ),
            88,
        )

    def test_city_or_sublocation_lifts_supporting_evidence_cap(self):
        for context in (
            LocationContext(city="Lamu"),
            LocationContext(sublocation="Lamu Old Town"),
        ):
            with self.subTest(context=context):
                self.assertEqual(
                    effective_editorial_score(
                        assessment(
                            editorial_value_score=75,
                            editorial_evidence=[
                                "distinctive_architecture",
                                "strong_visual_sense_of_place",
                            ],
                        ),
                        context,
                    ),
                    75,
                )

    def test_country_or_region_does_not_lift_supporting_cap(self):
        for context in (
            LocationContext(country="Kenya"),
            LocationContext(state_or_province="Lamu"),
        ):
            with self.subTest(context=context):
                self.assertEqual(
                    effective_editorial_score(
                        assessment(
                            editorial_value_score=75,
                            editorial_evidence=["distinctive_architecture"],
                        ),
                        context,
                    ),
                    69,
                )

    def test_city_without_evidence_remains_capped_at_49(self):
        self.assertEqual(
            effective_editorial_score(
                assessment(editorial_value_score=75),
                LocationContext(city="Lamu"),
            ),
            49,
        )

    def test_city_removes_cap_but_never_raises_raw_score(self):
        result = evaluate_editorial_policy(
            assessment(
                editorial_value_score=60,
                editorial_evidence=["strong_visual_sense_of_place"],
            ),
            VerifiedLocationState(True, LocationContextLevel.CITY),
        )
        self.assertEqual(
            result,
            EditorialPolicyResult(
                raw_score=60,
                effective_score=60,
                cap_applied=None,
                specific_location_used=True,
            ),
        )

    def test_inconsistent_location_state_does_not_lift_cap(self):
        value = assessment(
            editorial_value_score=75,
            editorial_evidence=["distinctive_architecture"],
        )
        states = (
            VerifiedLocationState(False, LocationContextLevel.CITY),
            VerifiedLocationState(True, LocationContextLevel.NONE),
        )
        for state in states:
            with self.subTest(state=state):
                result = evaluate_editorial_policy(value, state)
                self.assertEqual(result.effective_score, 69)
                self.assertEqual(result.cap_applied, 69)
                self.assertFalse(result.specific_location_used)

    def test_sanitized_meaningful_activity_cannot_use_city_to_avoid_empty_cap(self):
        self.assertEqual(
            effective_editorial_score(
                assessment(
                    editorial_value_score=90,
                    human_activity_readability="incidental",
                    editorial_evidence=["meaningful_human_activity"],
                ),
                LocationContext(city="Lamu"),
            ),
            49,
        )

    def test_evidence_sets_match_policy(self):
        self.assertEqual(
            STRONG_EDITORIAL_EVIDENCE,
            frozenset(
                {
                    EditorialEvidence.MEANINGFUL_HUMAN_ACTIVITY,
                    EditorialEvidence.PUBLIC_EVENT_OR_CEREMONY,
                    EditorialEvidence.UNUSUAL_DOCUMENTARY_CONDITION,
                }
            ),
        )
        self.assertEqual(
            SUPPORTING_EDITORIAL_EVIDENCE,
            frozenset(
                {
                    EditorialEvidence.DISTINCTIVE_ARCHITECTURE,
                    EditorialEvidence.STRONG_VISUAL_SENSE_OF_PLACE,
                }
            ),
        )


class EditorialEvidenceValidationTests(unittest.TestCase):
    def test_unknown_evidence_fails(self):
        with self.assertRaises(ValidationError):
            assessment(editorial_evidence=["guessed_location"])

    def test_removed_social_or_cultural_activity_fails(self):
        with self.assertRaises(ValidationError):
            assessment(
                editorial_evidence=[
                    "visible_social_or_cultural_activity"
                ]
            )

    def test_missing_evidence_fails(self):
        data = assessment_data()
        del data["editorial_evidence"]
        with self.assertRaises(ValidationError):
            ModelAssessment.model_validate(data)

    def test_empty_evidence_is_valid(self):
        self.assertEqual(assessment().editorial_evidence, [])

    def test_duplicate_evidence_fails(self):
        with self.assertRaises(ValidationError):
            assessment(
                editorial_evidence=[
                    "distinctive_architecture",
                    "distinctive_architecture",
                ]
            )


class EditorialDecisionPolicyTests(unittest.TestCase):
    def test_dsc_9251_city_context_is_editorial_pick_at_74(self):
        finalization = finalize_assessment_result(
            assessment(
                technical_score=75,
                composition_score=70,
                commercial_value_score=65,
                editorial_value_score=75,
                editorial_evidence=[
                    "distinctive_architecture",
                    "strong_visual_sense_of_place",
                ],
                prominent_ip_content=True,
            ),
            70,
            LocationContext(country="Kenya", city="Lamu"),
        )
        self.assertEqual(finalization.editorial_policy.raw_score, 75)
        self.assertEqual(finalization.editorial_policy.effective_score, 75)
        self.assertEqual(finalization.response.score, 74)
        self.assertEqual(
            finalization.release_restriction, "editorial_only"
        )
        self.assertEqual(finalization.response.verdict, "YES")

    def test_release_sensitive_effective_69_is_rejected(self):
        value = assessment(
            technical_score=100,
            composition_score=100,
            editorial_value_score=100,
            editorial_evidence=["distinctive_architecture"],
            release_sensitive_property=True,
        )
        result = finalize_assessment(value, 70)
        self.assertGreaterEqual(result.score, 70)
        self.assertEqual(result.verdict, "NO")

    def test_release_sensitive_effective_70_remains_eligible(self):
        value = assessment(
            technical_score=100,
            composition_score=100,
            editorial_value_score=70,
            editorial_evidence=["meaningful_human_activity"],
            recognizable_people=True,
        )
        self.assertEqual(finalize_assessment(value, 70).verdict, "YES")

    def test_editorial_70_does_not_bypass_weighted_threshold(self):
        value = assessment(
            technical_score=0,
            composition_score=0,
            editorial_value_score=70,
            editorial_evidence=["meaningful_human_activity"],
            recognizable_people=True,
        )
        result = finalize_assessment(value, 70)
        self.assertEqual(result.score, 28)
        self.assertEqual(result.verdict, "NO")

    def test_hard_reject_overrides_passing_editorial_score(self):
        value = assessment(
            technical_score=100,
            composition_score=100,
            editorial_value_score=100,
            editorial_evidence=["public_event_or_ceremony"],
            recognizable_people=True,
            hard_reject=True,
            hard_reject_reasons=["missed_focus"],
        )
        self.assertEqual(finalize_assessment(value, 70).verdict, "NO")

    def test_unrestricted_content_can_pass_commercially_without_evidence(self):
        value = assessment(
            technical_score=75,
            composition_score=75,
            commercial_value_score=75,
            editorial_value_score=100,
            editorial_evidence=[],
        )
        self.assertEqual(select_route(value), ("commercial", 75))
        self.assertEqual(finalize_assessment(value, 70).verdict, "YES")

    def test_route_uses_effective_editorial_score(self):
        value = assessment(
            commercial_value_score=60,
            editorial_value_score=95,
            editorial_evidence=[],
        )
        self.assertEqual(effective_editorial_score(value), 49)
        self.assertEqual(select_route(value), ("commercial", 60))


class EditorialLoggingAndResponseTests(unittest.TestCase):
    def test_log_fields_include_raw_effective_and_evidence(self):
        value = assessment(
            editorial_value_score=90,
            editorial_evidence=[
                "distinctive_architecture",
                "strong_visual_sense_of_place",
            ],
        )
        self.assertEqual(
            editorial_log_fields(value),
            {
                "raw_editorial_score": 90,
                "effective_editorial_score": 69,
                "editorial_cap_applied": 69,
                "specific_verified_location_used": False,
                "editorial_evidence": (
                    "distinctive_architecture; "
                    "strong_visual_sense_of_place"
                ),
                "effective_editorial_evidence": (
                    "distinctive_architecture; "
                    "strong_visual_sense_of_place"
                ),
                "ignored_editorial_evidence": "",
            },
        )

    def test_editorial_evidence_is_internal_only(self):
        value = assessment(
            editorial_evidence=["meaningful_human_activity"]
        )
        response = finalize_assessment(value, 70).model_dump()
        self.assertEqual(
            set(response),
            {"verdict", "score", "reason", "issues"},
        )
        self.assertNotIn("editorial_evidence", response)

    def test_release_restriction_remains_independent(self):
        unrestricted = assessment(
            editorial_value_score=80,
            editorial_evidence=["meaningful_human_activity"],
        )
        restricted = assessment(
            editorial_value_score=80,
            editorial_evidence=["meaningful_human_activity"],
            recognizable_people=True,
        )
        self.assertEqual(
            release_restriction(unrestricted),
            "unrestricted",
        )
        self.assertEqual(
            release_restriction(restricted),
            "editorial_only",
        )


class EditorialPromptContractTests(unittest.TestCase):
    def test_prompt_judges_the_exact_photograph(self):
        self.assertIn(
            "Evaluate the marketability of this exact photograph, not the "
            "potential marketability of the general subject.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Editorial evidence establishes that documentary content exists. "
            "It does not establish that the photograph is well executed or "
            "marketable.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_meaningful_human_activity_requires_visible_action(self):
        self.assertIn(
            "their action can be understood from the preview",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "A small, dark, obscured, or stationary person must be classified "
            "as incidental and does not qualify.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_does_not_offer_removed_cultural_activity_evidence(self):
        self.assertNotIn(
            "visible_social_or_cultural_activity",
            SYSTEM_PROMPT,
        )
        self.assertNotIn(
            "visible social or cultural activity",
            SYSTEM_PROMPT.lower(),
        )

    def test_general_composition_severity_limits_are_present(self):
        self.assertIn(
            "treat them as obstruction rather than framing.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "major: composition_score should normally not exceed 49.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "severe: composition_score should normally not exceed 29.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_technical_quality_includes_subject_visibility(self):
        self.assertIn(
            "A technically focused image is not necessarily technically "
            "strong.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "hidden in deep shadow, technical_score should normally remain "
            "below 70 even if focus is acceptable.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_storefront_editorial_value_is_constrained(self):
        self.assertIn(
            "Visible branding or business signage can provide editorial "
            "context, but it does not automatically create strong editorial "
            "value.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "If a storefront is poorly lit, obscured, visually buried, or "
            "weakly composed, editorial_value_score should normally remain "
            "below 60.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_speculative_market_justification_is_prohibited(self):
        for phrase in (
            "could illustrate",
            "may appeal to",
            "potentially useful",
            "could represent",
            "suitable for niche buyers",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, NORMALIZED_SYSTEM_PROMPT)
        self.assertIn(
            "Possible uses are not evidence of strong market value.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_weak_execution_is_not_a_hard_rejection(self):
        self.assertIn(
            "Do not use hard rejection when the photograph remains "
            "technically usable.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_contains_every_evidence_value(self):
        for evidence in EditorialEvidence:
            with self.subTest(evidence=evidence):
                self.assertIn(evidence.value, SYSTEM_PROMPT)

    def test_prompt_forbids_guessed_locations(self):
        self.assertIn(
            "Do not identify or name a location or landmark from visual "
            "appearance alone.",
            SYSTEM_PROMPT,
        )
        self.assertIn(
            "Do not treat a guessed location as evidence.",
            SYSTEM_PROMPT,
        )

    def test_prompt_forbids_unsupported_historic_designations(self):
        self.assertIn(
            "does not establish that an area, building, or scene is historic, "
            "historical, heritage-listed, ancient, traditional, an old town, "
            "a UNESCO site, or culturally significant.",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            'It does not support "the city\'s historic urban environment."',
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_forbids_unsupported_socioeconomic_inference(self):
        self.assertIn(
            "Do not claim or imply poverty, development level, informality, "
            "socioeconomic status, or culture from construction style, "
            "litter, aging surfaces, visual wear, wiring, clothing, or "
            "neighborhood appearance.",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_prompt_uses_policy_specific_release_language(self):
        self.assertIn(
            "treated as release-sensitive under the configured no-release "
            "policy",
            NORMALIZED_SYSTEM_PROMPT,
        )
        self.assertIn(
            "No visibly release-sensitive content was detected.",
            NORMALIZED_SYSTEM_PROMPT,
        )


if __name__ == "__main__":
    unittest.main()
