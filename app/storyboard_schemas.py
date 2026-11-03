"""Bounded reference evidence and authoring data; never executable instructions."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ReferenceCreate(StoryModel):
    asset_id: str = Field(min_length=1, max_length=36)
    start_ms: int = Field(ge=0, le=7_200_000, strict=True)
    end_ms: int = Field(gt=0, le=7_200_000, strict=True)
    source_note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def range_valid(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("参考终点必须晚于起点")
        return self


class ReferenceUsageUpdate(StoryModel):
    allow_output: bool


class ReferenceSpan(StoryModel):
    id: str
    start_ms: int
    end_ms: int
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: Literal["hosted_cut", "manual_range"]
    transcript: str = Field(default="", max_length=30_000)


class VisualIntent(StoryModel):
    subject: str = Field(default="", max_length=1000)
    action: str = Field(default="", max_length=1000)
    scene: str = Field(default="", max_length=1000)
    composition: str = Field(default="", max_length=1000)
    camera: str = Field(default="", max_length=1000)
    lighting: str = Field(default="", max_length=1000)
    style: str = Field(default="", max_length=1000)


class StoryFact(StoryModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    text: str = Field(min_length=1, max_length=2000)
    confirmed: bool = False
    locked: bool = False


class StoryShot(StoryModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    duration_ms: int = Field(ge=100, le=120_000, strict=True)
    intent: str = Field(default="", max_length=2000)
    visual: VisualIntent = Field(default_factory=VisualIntent)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    voiceover: str = Field(default="", max_length=4000)
    screen_text: str = Field(default="", max_length=2000)
    sound_notes: str = Field(default="", max_length=2000)
    reference_span_id: str | None = Field(default=None, max_length=64)
    locked: bool = False

    @model_validator(mode="after")
    def constraints_bounded(self):
        if any(not text.strip() or len(text) > 1000 for text in self.constraints):
            raise ValueError("约束不能为空且每条最多1000字")
        return self


class StoryContent(StoryModel):
    schema_version: Literal["storyboard/1"] = "storyboard/1"
    title: str = Field(min_length=1, max_length=200)
    target_duration_ms: int = Field(ge=100, le=7_200_000, strict=True)
    facts: list[StoryFact] = Field(default_factory=list, max_length=100)
    shots: list[StoryShot] = Field(min_length=1, max_length=500)
    status: Literal["draft", "confirmed"] = "draft"

    @model_validator(mode="after")
    def valid_content(self):
        if len({row.id for row in self.shots}) != len(self.shots) or len({row.id for row in self.facts}) != len(self.facts):
            raise ValueError("镜头和事实标识不能重复")
        if sum(row.duration_ms for row in self.shots) != self.target_duration_ms:
            raise ValueError("镜头时长之和须与目标时长一致，不会自动裁切或拉长")
        if self.status == "confirmed" and (any(not row.intent.strip() or not row.visual.subject.strip() for row in self.shots)
                or any(not row.confirmed for row in self.facts)):
            raise ValueError("确认前请填写各镜头意图和主体，并确认资料事实")
        return self


class StoryCreate(StoryModel):
    reference_id: str | None = Field(default=None, max_length=36)
    content: StoryContent


class StorySave(StoryModel):
    base_revision_id: str = Field(min_length=1, max_length=36)
    content: StoryContent
    unlock_shot_ids: list[str] = Field(default_factory=list, max_length=500)
    unlock_fact_ids: list[str] = Field(default_factory=list, max_length=100)
