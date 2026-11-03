from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ProjectLimitUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_revision: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    limit_micros: int | None = Field(ge=0, le=9_000_000_000_000_000)


class JobActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["retry", "cancel"]
    expected_status: Literal["queued", "running", "failed"]
    expected_attempt: int = Field(ge=0)
    expected_generation: int = Field(ge=0)


class AnalysisPreparationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    asset_ids: list[str] = Field(min_length=1, max_length=100)
    shot_analysis: bool = True
    transcribe: bool = True
    whisper_model: str = Field(default="small", min_length=1, max_length=100)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    force_new: bool = False
