from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, model_validator


class GarmentCategory(StrEnum):
    OUTERWEAR = "outerwear"
    FULL_BODY = "full_body"
    UPPER_BODY = "upper_body"
    LOWER_BODY = "lower_body"
    SHOES = "shoes"
    AUTO = "auto"


class ProviderState(StrEnum):
    QUEUED = "queued"
    SUBMITTING = "submitting"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    PROVIDER_UNKNOWN = "provider_unknown"


class CampaignAnalyzeRequest(BaseModel):
    campaign_id: UUID
    video_url: HttpUrl
    video_checksum: str = Field(min_length=32, max_length=128)
    campaign_input_version: int = Field(ge=1)
    products: list[dict[str, Any]]
    brand_direction: str = Field(min_length=1, max_length=5000)
    timing_candidates: list[float] = Field(default_factory=list)
    force_reanalysis: bool = False


class YouCamTaskRequest(BaseModel):
    source_url: HttpUrl
    reference_url: HttpUrl
    garment_category: GarmentCategory

    @model_validator(mode="after")
    def explicit_category_preferred(self) -> "YouCamTaskRequest":
        return self


class MirrorSessionRequest(BaseModel):
    manifest_id: UUID
    shopper_photo_id: UUID
    initial_look_id: UUID


class CampaignDirectionRequest(BaseModel):
    brand_direction: str = Field(min_length=1, max_length=5000)
    force_reanalysis: bool = False


class SegmentInput(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "SegmentInput":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Segment end must be after its start")
        return self


class RemixOptionInput(BaseModel):
    reference_asset_id: UUID
    label: str = Field(min_length=1, max_length=100)
    garment_category: GarmentCategory
    allowed_tags: list[str] = Field(default_factory=list, max_length=12)


class LookReviewRequest(BaseModel):
    product_id: UUID
    reference_asset_id: UUID
    garment_category: GarmentCategory
    is_hero: bool
    remix_allowed: bool
    segments: list[SegmentInput] = Field(min_length=1)
    remix_options: list[RemixOptionInput] = Field(default_factory=list, max_length=12)


class SaveMirrorRequest(BaseModel):
    saved: bool


class PriorityRequest(BaseModel):
    look_id: UUID


class RemixRequest(BaseModel):
    look_id: UUID
    preset_id: UUID | None = None
    text_constraint: str | None = Field(default=None, max_length=240)


class JobRecord(BaseModel):
    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    available_at: datetime
