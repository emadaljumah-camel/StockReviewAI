import base64
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from location_context import (
    ResolvedLocationContext,
    resolve_location_context,
    strip_jpeg_metadata_base64,
)
from models import (
    CompositionDefect,
    CompositionDefectSeverity,
    EditorialEvidence,
    GeocodingSettings,
    HumanActivityReadability,
    LMStudioSettings,
    LocationContext,
    LocationContextLevel,
    MainSubjectType,
    ModelAssessment,
    ReviewRequest,
    ReviewResponse,
)
from prompt import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    determine_location_context_level,
    location_log_fields,
    user_prompt,
)


class ReviewError(RuntimeError):
    pass


STRONG_EDITORIAL_EVIDENCE = frozenset(
    {
        EditorialEvidence.MEANINGFUL_HUMAN_ACTIVITY,
        EditorialEvidence.PUBLIC_EVENT_OR_CEREMONY,
        EditorialEvidence.UNUSUAL_DOCUMENTARY_CONDITION,
    }
)

SUPPORTING_EDITORIAL_EVIDENCE = frozenset(
    {
        EditorialEvidence.DISTINCTIVE_ARCHITECTURE,
        EditorialEvidence.STRONG_VISUAL_SENSE_OF_PLACE,
    }
)


HUMAN_ACTIVITY_EDITORIAL_EVIDENCE = frozenset(
    {
        EditorialEvidence.MEANINGFUL_HUMAN_ACTIVITY,
    }
)


@dataclass(frozen=True)
class VerifiedLocationState:
    verified_location_context: bool
    location_context_level: LocationContextLevel


@dataclass(frozen=True)
class EditorialPolicyResult:
    raw_score: int
    effective_score: int
    cap_applied: int | None
    specific_location_used: bool


@dataclass(frozen=True)
class CompositionPolicyResult:
    raw_composition_score: int
    effective_composition_score: int
    raw_route_value: int
    effective_route_value: int
    raw_composition_defect_severity: CompositionDefectSeverity
    effective_composition_defect_severity: CompositionDefectSeverity
    severity_escalation_reason: str | None
    raw_composition_defects: tuple[CompositionDefect, ...]
    effective_composition_defects: tuple[CompositionDefect, ...]
    ignored_composition_defects: tuple[CompositionDefect, ...]
    composition_score_deduction: int
    route_value_deduction: int
    composition_policy_applied: str


@dataclass(frozen=True)
class FinalizationResult:
    response: ReviewResponse
    editorial_policy: EditorialPolicyResult
    composition_policy: CompositionPolicyResult
    release_restriction: str
    selected_market_basis: str


def ignored_editorial_evidence(
    assessment: ModelAssessment,
) -> frozenset[EditorialEvidence]:
    if assessment.human_activity_readability == HumanActivityReadability.CLEAR:
        return frozenset()
    return frozenset(assessment.editorial_evidence).intersection(
        HUMAN_ACTIVITY_EDITORIAL_EVIDENCE
    )


def effective_editorial_evidence(
    assessment: ModelAssessment,
) -> frozenset[EditorialEvidence]:
    return frozenset(assessment.editorial_evidence).difference(
        ignored_editorial_evidence(assessment)
    )


def verified_location_state(
    location_context: LocationContext | None,
) -> VerifiedLocationState:
    return VerifiedLocationState(
        verified_location_context=location_context is not None,
        location_context_level=determine_location_context_level(
            location_context
        ),
    )


def evaluate_editorial_policy(
    assessment: ModelAssessment,
    location_state: VerifiedLocationState,
) -> EditorialPolicyResult:
    evidence = effective_editorial_evidence(assessment)
    raw_score = assessment.editorial_value_score
    has_strong_evidence = bool(
        evidence.intersection(STRONG_EDITORIAL_EVIDENCE)
    )
    has_supporting_evidence = bool(
        evidence.intersection(SUPPORTING_EDITORIAL_EVIDENCE)
    )
    has_specific_verified_location = (
        location_state.verified_location_context
        and location_state.location_context_level
        in {
            LocationContextLevel.CITY,
            LocationContextLevel.SUBLOCATION,
        }
    )

    if not evidence:
        return EditorialPolicyResult(
            raw_score=raw_score,
            effective_score=min(raw_score, 49),
            cap_applied=49,
            specific_location_used=False,
        )
    if has_strong_evidence:
        return EditorialPolicyResult(
            raw_score=raw_score,
            effective_score=raw_score,
            cap_applied=None,
            specific_location_used=False,
        )
    if has_supporting_evidence and has_specific_verified_location:
        return EditorialPolicyResult(
            raw_score=raw_score,
            effective_score=raw_score,
            cap_applied=None,
            specific_location_used=True,
        )
    return EditorialPolicyResult(
        raw_score=raw_score,
        effective_score=min(raw_score, 69),
        cap_applied=69,
        specific_location_used=False,
    )


def effective_editorial_score(
    assessment: ModelAssessment,
    location_context: LocationContext | None = None,
) -> int:
    return evaluate_editorial_policy(
        assessment,
        verified_location_state(location_context),
    ).effective_score


def editorial_log_fields(
    assessment: ModelAssessment,
    editorial_policy: EditorialPolicyResult | None = None,
) -> dict[str, int | str | bool]:
    ignored = ignored_editorial_evidence(assessment)
    effective = effective_editorial_evidence(assessment)
    policy = editorial_policy or evaluate_editorial_policy(
        assessment,
        VerifiedLocationState(False, LocationContextLevel.NONE),
    )
    return {
        "raw_editorial_score": policy.raw_score,
        "effective_editorial_score": policy.effective_score,
        "editorial_cap_applied": (
            policy.cap_applied if policy.cap_applied is not None else "none"
        ),
        "specific_verified_location_used": (
            policy.specific_location_used
        ),
        "editorial_evidence": "; ".join(
            evidence.value for evidence in assessment.editorial_evidence
        ),
        "effective_editorial_evidence": "; ".join(
            evidence.value
            for evidence in assessment.editorial_evidence
            if evidence in effective
        ),
        "ignored_editorial_evidence": "; ".join(
            evidence.value
            for evidence in assessment.editorial_evidence
            if evidence in ignored
        ),
    }


def apply_composition_policy(
    *,
    raw_composition_score: int,
    raw_route_value: int,
    severity: CompositionDefectSeverity,
    main_subject_type: MainSubjectType,
    foreground_dominates_frame: bool,
    composition_defects: Sequence[CompositionDefect] = (),
) -> CompositionPolicyResult:
    raw_defects = tuple(composition_defects)
    ignored_defects = tuple(
        defect
        for defect in raw_defects
        if (
            main_subject_type != MainSubjectType.DISCRETE
            and defect == CompositionDefect.SUBJECT_TOO_SMALL
        )
    )
    effective_defects = tuple(
        defect for defect in raw_defects if defect not in ignored_defects
    )

    effective_severity = severity
    severity_escalation_reason = None
    discrete_subject_badly_presented = (
        main_subject_type == MainSubjectType.DISCRETE
        and CompositionDefect.SUBJECT_TOO_SMALL in effective_defects
        and CompositionDefect.EXCESSIVE_DEAD_SPACE in effective_defects
    )
    foreground_dominance_is_material = (
        foreground_dominates_frame
        and CompositionDefect.DOMINANT_DISTRACTION in effective_defects
        and len(set(effective_defects)) >= 2
    )
    if raw_defects and not effective_defects:
        effective_severity = CompositionDefectSeverity.NONE
        severity_escalation_reason = (
            "all_composition_defects_ignored_for_subject_type"
        )
    elif (
        discrete_subject_badly_presented
        and severity
        in {
            CompositionDefectSeverity.NONE,
            CompositionDefectSeverity.MINOR,
        }
    ):
        effective_severity = CompositionDefectSeverity.MAJOR
        severity_escalation_reason = (
            "discrete_subject_too_small_with_excessive_dead_space"
        )
    elif (
        foreground_dominance_is_material
        and severity
        in {
            CompositionDefectSeverity.NONE,
            CompositionDefectSeverity.MINOR,
        }
    ):
        effective_severity = CompositionDefectSeverity.MAJOR
        severity_escalation_reason = (
            "foreground_dominance_with_multiple_effective_defects"
        )
    elif (
        len(set(effective_defects)) >= 3
        and severity
        in {
            CompositionDefectSeverity.NONE,
            CompositionDefectSeverity.MINOR,
        }
    ):
        effective_severity = CompositionDefectSeverity.MAJOR
        severity_escalation_reason = "three_or_more_distinct_defects"

    composition_deduction = 0
    route_deduction = 0
    composition_cap = None
    route_cap = None

    if effective_severity == CompositionDefectSeverity.MINOR:
        composition_cap = 69
    elif effective_severity == CompositionDefectSeverity.MAJOR:
        composition_deduction = 20
        route_deduction = 10
        composition_cap = 49
        route_cap = 64
    elif effective_severity == CompositionDefectSeverity.SEVERE:
        composition_deduction = 40
        route_deduction = 20
        composition_cap = 29
        route_cap = 49

    effective_composition_score = max(
        0, raw_composition_score - composition_deduction
    )
    effective_route_value = max(0, raw_route_value - route_deduction)

    if composition_cap is not None:
        effective_composition_score = min(
            effective_composition_score, composition_cap
        )
    if route_cap is not None:
        effective_route_value = min(effective_route_value, route_cap)

    return CompositionPolicyResult(
        raw_composition_score=raw_composition_score,
        effective_composition_score=effective_composition_score,
        raw_route_value=raw_route_value,
        effective_route_value=effective_route_value,
        raw_composition_defect_severity=severity,
        effective_composition_defect_severity=effective_severity,
        severity_escalation_reason=severity_escalation_reason,
        raw_composition_defects=raw_defects,
        effective_composition_defects=effective_defects,
        ignored_composition_defects=ignored_defects,
        composition_score_deduction=(
            raw_composition_score - effective_composition_score
        ),
        route_value_deduction=raw_route_value - effective_route_value,
        composition_policy_applied=effective_severity.value,
    )


COMPOSITION_DEFECT_LABELS = {
    CompositionDefect.SUBJECT_OBSCURED: "an obscured subject",
    CompositionDefect.SUBJECT_TOO_SMALL: "a subject that is too small",
    CompositionDefect.ACCIDENTAL_CROP: "an accidental subject crop",
    CompositionDefect.EXCESSIVE_DEAD_SPACE: "excessive dead space",
    CompositionDefect.DOMINANT_DISTRACTION: "a dominant distraction",
    CompositionDefect.POOR_VISUAL_BALANCE: "poor visual balance",
    CompositionDefect.CLUTTERED_VISUAL_HIERARCHY: (
        "cluttered visual hierarchy"
    ),
    CompositionDefect.AWKWARD_OVERLAPS: "awkward overlaps",
}


def _readable_list(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def composition_penalty_explanation(
    assessment: ModelAssessment,
    policy: CompositionPolicyResult,
) -> str:
    severity = policy.effective_composition_defect_severity
    if severity not in {
        CompositionDefectSeverity.MAJOR,
        CompositionDefectSeverity.SEVERE,
    }:
        return ""

    penalty_changed_value = (
        policy.effective_composition_score
        < policy.raw_composition_score
        or policy.effective_route_value < policy.raw_route_value
    )
    if not penalty_changed_value:
        return ""

    defects = _readable_list(
        [
            COMPOSITION_DEFECT_LABELS[defect]
            for defect in policy.effective_composition_defects
        ]
    )
    if severity == CompositionDefectSeverity.SEVERE:
        return (
            "Severe composition penalty applied because the framing "
            "makes the photograph unsuitable for stock through "
            f"{defects}."
        )
    return (
        "Major composition penalty applied because the framing "
        "substantially weakens subject clarity, visual hierarchy, or "
        f"stock usefulness through {defects}."
    )


def composition_log_fields(
    assessment: ModelAssessment,
    policy: CompositionPolicyResult,
) -> dict[str, int | str | bool]:
    return {
        "raw_composition_score": policy.raw_composition_score,
        "effective_composition_score": policy.effective_composition_score,
        "raw_composition_defect_severity": (
            policy.raw_composition_defect_severity.value
        ),
        "effective_composition_defect_severity": (
            policy.effective_composition_defect_severity.value
        ),
        "severity_escalation_reason": (
            policy.severity_escalation_reason or ""
        ),
        "main_subject_type": assessment.main_subject_type.value,
        "raw_composition_defects": "; ".join(
            defect.value for defect in policy.raw_composition_defects
        ),
        "effective_composition_defects": "; ".join(
            defect.value for defect in policy.effective_composition_defects
        ),
        "ignored_composition_defects": "; ".join(
            defect.value for defect in policy.ignored_composition_defects
        ),
        "raw_route_value": policy.raw_route_value,
        "effective_route_value": policy.effective_route_value,
        "composition_score_deduction": policy.composition_score_deduction,
        "route_value_deduction": policy.route_value_deduction,
        "composition_policy_applied": policy.composition_policy_applied,
        "main_subject_visibility": assessment.main_subject_visibility.value,
        "foreground_dominates_frame": (
            assessment.foreground_dominates_frame
        ),
        "human_activity_readability": (
            assessment.human_activity_readability.value
        ),
    }


def release_restriction(assessment: ModelAssessment) -> str:
    if (
        assessment.recognizable_people
        or assessment.release_sensitive_property
        or assessment.prominent_ip_content
    ):
        return "editorial_only"
    return "unrestricted"


def select_route(
    assessment: ModelAssessment,
    location_context: LocationContext | None = None,
    editorial_policy: EditorialPolicyResult | None = None,
) -> tuple[str, int]:
    policy = editorial_policy or evaluate_editorial_policy(
        assessment,
        verified_location_state(location_context),
    )
    editorial_score = policy.effective_score
    if release_restriction(assessment) == "editorial_only":
        return "editorial", editorial_score
    if assessment.commercial_value_score >= editorial_score:
        return "commercial", assessment.commercial_value_score
    return "editorial", editorial_score


def finalize_assessment_result(
    assessment: ModelAssessment,
    threshold: int,
    location_context: LocationContext | None = None,
) -> FinalizationResult:
    if type(threshold) is not int or threshold < 0 or threshold > 100:
        raise ReviewError("Review threshold must be an integer from 0 to 100")

    editorial_policy = evaluate_editorial_policy(
        assessment,
        verified_location_state(location_context),
    )
    restriction = release_restriction(assessment)
    market_basis, raw_route_value = select_route(
        assessment,
        editorial_policy=editorial_policy,
    )
    editorial_score = editorial_policy.effective_score
    composition_policy = apply_composition_policy(
        raw_composition_score=assessment.composition_score,
        raw_route_value=raw_route_value,
        severity=assessment.composition_defect_severity,
        main_subject_type=assessment.main_subject_type,
        foreground_dominates_frame=assessment.foreground_dominates_frame,
        composition_defects=assessment.composition_defects,
    )
    release_sensitive_editorial_failure = (
        restriction == "editorial_only"
        and editorial_score < 70
    )
    weighted_total = (
        assessment.technical_score * 35
        + composition_policy.effective_composition_score * 25
        + composition_policy.effective_route_value * 40
    )
    final_score = (weighted_total + 50) // 100

    if assessment.hard_reject:
        verdict = "NO"
    elif release_sensitive_editorial_failure:
        verdict = "NO"
    elif final_score < threshold:
        verdict = "NO"
    else:
        verdict = "YES"
    authoritative_composition_reason = composition_penalty_explanation(
        assessment,
        composition_policy,
    )
    response_reason = assessment.reason
    if authoritative_composition_reason:
        response_reason = (
            f"{response_reason.rstrip()} "
            f"{authoritative_composition_reason}"
        )

    return FinalizationResult(
        response=ReviewResponse(
            verdict=verdict,
            score=final_score,
            reason=response_reason,
            issues=assessment.issues,
        ),
        editorial_policy=editorial_policy,
        composition_policy=composition_policy,
        release_restriction=restriction,
        selected_market_basis=market_basis,
    )


def finalize_assessment(
    assessment: ModelAssessment,
    threshold: int,
    location_context: LocationContext | None = None,
) -> ReviewResponse:
    return finalize_assessment_result(
        assessment,
        threshold,
        location_context,
    ).response


def _validated_jpeg_base64(value: str) -> str:
    raw_value = value.strip()
    if raw_value.startswith("data:image/"):
        _, _, raw_value = raw_value.partition(",")

    try:
        image_bytes = base64.b64decode(raw_value, validate=True)
    except Exception as exc:
        raise ReviewError("Image payload was not valid base64") from exc

    if not image_bytes.startswith(b"\xff\xd8\xff"):
        raise ReviewError("Image payload was not a valid JPEG")

    return raw_value


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```json") and value.endswith("```"):
        return value[7:-3].strip()
    if value.startswith("```") and value.endswith("```"):
        return value[3:-3].strip()
    return value


def _choice_content(response: dict[str, Any]) -> str:
    try:
        choice = response["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or choice.get("text")
    except (KeyError, IndexError, TypeError) as exc:
        raise ReviewError("LM Studio response did not include choices[0].message.content") from exc

    if not isinstance(content, str) or not content.strip():
        raise ReviewError("LM Studio response content was empty")
    return content


def _build_payload(
    request: ReviewRequest,
    settings: LMStudioSettings,
    *,
    location_context: LocationContext | None = None,
    location_context_source: str = "none",
) -> dict[str, Any]:
    image_base64 = _validated_jpeg_base64(request.image_base64)
    image_base64 = strip_jpeg_metadata_base64(image_base64)
    effective_location_context = location_context or request.location_context
    if location_context_source == "none" and request.location_context:
        location_context_source = "lightroom_catalog"
    return {
        "model": settings.model,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "stock_review_result",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            },
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_prompt(
                            request.filename,
                            effective_location_context,
                            location_context_source,
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                        },
                    },
                ],
            },
        ],
    }


async def review_with_lmstudio(
    request: ReviewRequest,
    settings: LMStudioSettings,
    logger: logging.Logger,
    geocoding_settings: GeocodingSettings | None = None,
) -> ReviewResponse:
    try:
        resolved_location = await resolve_location_context(
            request.image_base64,
            request.location_context,
            geocoding_settings or GeocodingSettings(),
        )
    except Exception as exc:
        logger.warning(
            "location_context_resolution_failed filename=%s error=%s",
            request.filename,
            type(exc).__name__,
        )
        resolved_location = ResolvedLocationContext(
            request.location_context,
            "lightroom_catalog" if request.location_context else "none",
            False,
            "error",
            type(exc).__name__,
        )

    if resolved_location.reverse_geocoder_error:
        logger.warning(
            "reverse_geocoder_failed filename=%s error=%s",
            request.filename,
            resolved_location.reverse_geocoder_error,
        )

    payload = _build_payload(
        request,
        settings,
        location_context=resolved_location.context,
        location_context_source=resolved_location.source,
    )
    last_error: Exception | None = None

    for attempt in range(settings.retries + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
                response = await client.post(settings.endpoint, json=payload)
                if response.status_code >= 400:
                    error_body = response.text.strip()[:1000]
                    raise ReviewError(
                        f"LM Studio HTTP {response.status_code}: {error_body or response.reason_phrase}"
                    )

            raw = response.json()
            content = _choice_content(raw)
            parsed = json.loads(_strip_json_fence(content))
            assessment = ModelAssessment.model_validate(parsed)
            finalization = finalize_assessment_result(
                assessment,
                request.threshold,
                resolved_location.context,
            )
            result = finalization.response
            restriction = finalization.release_restriction
            market_basis = finalization.selected_market_basis
            editorial_log = editorial_log_fields(
                assessment,
                finalization.editorial_policy,
            )
            composition_log = composition_log_fields(
                assessment,
                finalization.composition_policy,
            )
            location_log = location_log_fields(
                resolved_location.context,
                source=resolved_location.source,
                gps_metadata_present=(
                    resolved_location.gps_metadata_present
                ),
                reverse_geocoder_status=(
                    resolved_location.reverse_geocoder_status
                ),
            )

            logger.info(
                "review filename=%s verified_location_context=%s "
                "location_context_source=%s gps_metadata_present=%s "
                "reverse_geocoder_status=%s location_context_level=%s "
                "location_country=%s "
                "location_state_or_province=%s location_city=%s "
                "location_sublocation=%s release_restriction=%s "
                "selected_market_basis=%s technical_score=%s "
                "raw_composition_score=%s effective_composition_score=%s "
                "raw_composition_defect_severity=%s "
                "effective_composition_defect_severity=%s "
                "severity_escalation_reason=%s main_subject_type=%s "
                "raw_composition_defects=%s "
                "effective_composition_defects=%s "
                "ignored_composition_defects=%s "
                "raw_route_value=%s effective_route_value=%s "
                "composition_score_deduction=%s route_value_deduction=%s "
                "composition_policy_applied=%s "
                "main_subject_visibility=%s foreground_dominates_frame=%s "
                "human_activity_readability=%s commercial_value_score=%s "
                "raw_editorial_score=%s effective_editorial_score=%s "
                "editorial_cap_applied=%s "
                "specific_verified_location_used=%s "
                "editorial_evidence=%s effective_editorial_evidence=%s "
                "ignored_editorial_evidence=%s hard_reject=%s "
                "hard_reject_reasons=%s verdict=%s score=%s reason=%s issues=%s",
                request.filename,
                location_log["verified_location_context"],
                location_log["location_context_source"],
                location_log["gps_metadata_present"],
                location_log["reverse_geocoder_status"],
                location_log["location_context_level"],
                location_log["location_country"],
                location_log["location_state_or_province"],
                location_log["location_city"],
                location_log["location_sublocation"],
                restriction,
                market_basis,
                assessment.technical_score,
                composition_log["raw_composition_score"],
                composition_log["effective_composition_score"],
                composition_log["raw_composition_defect_severity"],
                composition_log["effective_composition_defect_severity"],
                composition_log["severity_escalation_reason"],
                composition_log["main_subject_type"],
                composition_log["raw_composition_defects"],
                composition_log["effective_composition_defects"],
                composition_log["ignored_composition_defects"],
                composition_log["raw_route_value"],
                composition_log["effective_route_value"],
                composition_log["composition_score_deduction"],
                composition_log["route_value_deduction"],
                composition_log["composition_policy_applied"],
                composition_log["main_subject_visibility"],
                composition_log["foreground_dominates_frame"],
                composition_log["human_activity_readability"],
                assessment.commercial_value_score,
                editorial_log["raw_editorial_score"],
                editorial_log["effective_editorial_score"],
                editorial_log["editorial_cap_applied"],
                editorial_log["specific_verified_location_used"],
                editorial_log["editorial_evidence"],
                editorial_log["effective_editorial_evidence"],
                editorial_log["ignored_editorial_evidence"],
                assessment.hard_reject,
                "; ".join(reason.value for reason in assessment.hard_reject_reasons),
                result.verdict,
                result.score,
                result.reason,
                "; ".join(result.issues),
            )
            return result
        except (httpx.HTTPError, json.JSONDecodeError, ValidationError, ReviewError) as exc:
            last_error = exc
            logger.warning(
                "review_attempt_failed filename=%s attempt=%s error=%s",
                request.filename,
                attempt + 1,
                str(exc),
            )

    raise ReviewError(f"LM Studio review failed: {last_error}")
