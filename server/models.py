from typing import Literal

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    filename: str = Field(min_length=1)
    image_base64: str = Field(min_length=1)
    threshold: int = Field(default=70, ge=0, le=100)


class ReviewResponse(BaseModel):
    verdict: Literal["YES", "NO"]
    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)


class LMStudioSettings(BaseModel):
    endpoint: str = "http://127.0.0.1:1234/v1/chat/completions"
    model: str = "local-model"
    timeout_seconds: float = Field(default=120, gt=0)
    retries: int = Field(default=2, ge=0)


class ServerSettings(BaseModel):
    log_file: str = "StockReviewAI.server.log"


class AppConfig(BaseModel):
    lmstudio: LMStudioSettings = Field(default_factory=LMStudioSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
