from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)


NonBlankString = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]


class HardRejectReason(str, Enum):
    MISSED_FOCUS = "missed_focus"
    DESTRUCTIVE_MOTION_BLUR = "destructive_motion_blur"
    IRRECOVERABLE_CLIPPING = "irrecoverable_clipping"
    SEVERE_ARTIFACTS = "severe_artifacts"
    NO_MEANINGFUL_SUBJECT = "no_meaningful_subject"


class EditorialEvidence(str, Enum):
    MEANINGFUL_HUMAN_ACTIVITY = "meaningful_human_activity"
    PUBLIC_EVENT_OR_CEREMONY = "public_event_or_ceremony"
    DISTINCTIVE_ARCHITECTURE = "distinctive_architecture"
    UNUSUAL_DOCUMENTARY_CONDITION = (
        "unusual_documentary_condition"
    )
    STRONG_VISUAL_SENSE_OF_PLACE = (
        "strong_visual_sense_of_place"
    )


class MainSubjectVisibility(str, Enum):
    CLEAR = "clear"
    PARTIALLY_OBSCURED = "partially_obscured"
    SEVERELY_OBSCURED = "severely_obscured"


class MainSubjectType(str, Enum):
    DISCRETE = "discrete"
    ENVIRONMENTAL_SCENE = "environmental_scene"
    PATTERN_OR_ABSTRACT = "pattern_or_abstract"


class CompositionDefectSeverity(str, Enum):
    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"
    SEVERE = "severe"


class CompositionDefect(str, Enum):
    SUBJECT_OBSCURED = "subject_obscured"
    SUBJECT_TOO_SMALL = "subject_too_small"
    ACCIDENTAL_CROP = "accidental_crop"
    EXCESSIVE_DEAD_SPACE = "excessive_dead_space"
    DOMINANT_DISTRACTION = "dominant_distraction"
    POOR_VISUAL_BALANCE = "poor_visual_balance"
    CLUTTERED_VISUAL_HIERARCHY = "cluttered_visual_hierarchy"
    AWKWARD_OVERLAPS = "awkward_overlaps"


class HumanActivityReadability(str, Enum):
    NONE = "none"
    INCIDENTAL = "incidental"
    CLEAR = "clear"


class LocationContextLevel(str, Enum):
    NONE = "none"
    COUNTRY = "country"
    REGION = "region"
    CITY = "city"
    SUBLOCATION = "sublocation"


class LocationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: NonBlankString | None = None
    state_or_province: NonBlankString | None = None
    city: NonBlankString | None = None
    sublocation: NonBlankString | None = None

    @model_validator(mode="after")
    def require_at_least_one_value(self) -> "LocationContext":
        if not any(
            (
                self.country,
                self.state_or_province,
                self.city,
                self.sublocation,
            )
        ):
            raise ValueError(
                "location_context must contain at least one location field"
            )
        return self


class ReviewRequest(BaseModel):
    filename: str = Field(min_length=1)
    image_base64: str = Field(min_length=1)
    threshold: StrictInt = Field(default=70, ge=0, le=100)
    location_context: LocationContext | None = None


class ModelAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    technical_score: StrictInt = Field(ge=0, le=100)
    composition_score: StrictInt = Field(ge=0, le=100)
    commercial_value_score: StrictInt = Field(ge=0, le=100)
    editorial_value_score: StrictInt = Field(ge=0, le=100)
    main_subject_type: MainSubjectType
    main_subject_visibility: MainSubjectVisibility
    foreground_dominates_frame: StrictBool
    composition_defect_severity: CompositionDefectSeverity
    composition_defects: list[CompositionDefect]
    human_activity_readability: HumanActivityReadability
    editorial_evidence: list[EditorialEvidence]
    recognizable_people: StrictBool
    release_sensitive_property: StrictBool
    prominent_ip_content: StrictBool
    hard_reject: StrictBool
    hard_reject_reasons: list[HardRejectReason]
    reason: NonBlankString
    issues: list[NonBlankString]

    @model_validator(mode="after")
    def validate_consistency(self) -> "ModelAssessment":
        if len(self.composition_defects) != len(
            set(self.composition_defects)
        ):
            raise ValueError(
                "composition_defects must not contain duplicates"
            )
        if (
            self.composition_defect_severity
            == CompositionDefectSeverity.NONE
            and self.composition_defects
        ):
            raise ValueError(
                "composition_defect_severity=none requires "
                "composition_defects to be empty"
            )
        if (
            self.composition_defect_severity
            != CompositionDefectSeverity.NONE
            and not self.composition_defects
        ):
            raise ValueError(
                "non-none composition_defect_severity requires "
                "at least one composition defect"
            )
        if len(self.editorial_evidence) != len(set(self.editorial_evidence)):
            raise ValueError("editorial_evidence must not contain duplicates")
        if self.hard_reject and not self.hard_reject_reasons:
            raise ValueError(
                "hard_reject=true requires at least one hard-rejection reason"
            )
        if not self.hard_reject and self.hard_reject_reasons:
            raise ValueError(
                "hard_reject=false requires hard_reject_reasons to be empty"
            )
        return self


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["YES", "NO"]
    score: StrictInt = Field(ge=0, le=100)
    reason: NonBlankString
    issues: list[NonBlankString] = Field(default_factory=list)


class LMStudioSettings(BaseModel):
    endpoint: str = "http://127.0.0.1:1234/v1/chat/completions"
    model: str = "local-model"
    timeout_seconds: float = Field(default=120, gt=0)
    retries: int = Field(default=2, ge=0)


class ServerSettings(BaseModel):
    log_file: str = "StockReviewAI.server.log"


class GeocodingSettings(BaseModel):
    reverse_url: str = "https://nominatim.openstreetmap.org/reverse"
    user_agent: NonBlankString = "StockReviewAI/1.0"
    timeout_seconds: float = Field(default=8, gt=0)
    minimum_interval_seconds: float = Field(default=1, ge=1)


class AppConfig(BaseModel):
    lmstudio: LMStudioSettings = Field(default_factory=LMStudioSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    geocoding: GeocodingSettings = Field(default_factory=GeocodingSettings)
