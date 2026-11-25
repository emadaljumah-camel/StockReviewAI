from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

from models import LocationContext, NonBlankString, ReviewResponse


JobStatus = Literal[
    "created",
    "running",
    "completed",
    "cancelled",
    "failed",
    "interrupted",
]
JobIdentifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]


class JobPhoto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    photo_id: JobIdentifier
    catalog_uuid: NonBlankString | None = None
    filename: NonBlankString
    threshold: StrictInt = Field(ge=0, le=100)
    location_context: LocationContext | None = None


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_job_id: JobIdentifier | None = None
    client: Literal["lightroom"] = "lightroom"
    plugin_version: NonBlankString = "1.0.0"
    photos: list[JobPhoto] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_photo_ids(self) -> "CreateJobRequest":
        photo_ids = [photo.photo_id for photo in self.photos]
        if len(photo_ids) != len(set(photo_ids)):
            raise ValueError("photos must have unique photo_id values")
        return self


class PreviewUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_base64: NonBlankString


class JobProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: NonBlankString = "created"
    processed_photos: StrictInt = Field(default=0, ge=0)
    total_photos: StrictInt = Field(default=0, ge=0)
    current_photo_id: NonBlankString | None = None


class JobPhotoResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    photo_id: JobIdentifier
    verdict: Literal["YES", "NO"]
    score: StrictInt = Field(ge=0, le=100)
    reason: NonBlankString
    issues: list[NonBlankString] = Field(default_factory=list)

    @classmethod
    def from_review(
        cls,
        photo_id: str,
        review: ReviewResponse,
    ) -> "JobPhotoResult":
        return cls(photo_id=photo_id, **review.model_dump())


class JobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: JobIdentifier
    status: Literal["completed"] = "completed"
    results: list[JobPhotoResult]
    errors: list[NonBlankString] = Field(default_factory=list)


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: JobIdentifier
    status: JobStatus
    progress: JobProgress
    errors: list[str] = Field(default_factory=list)
    resumable: bool = True
