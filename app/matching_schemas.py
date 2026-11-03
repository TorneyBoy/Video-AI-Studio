"""Human semantic evidence, shared requirements and immutable sourcing intents."""
from typing import Literal

from pydantic import Field, model_validator

from .storyboard_schemas import StoryModel, VisualIntent


class SemanticSpan(StoryModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, gt=0, strict=True)
    visual: VisualIntent = Field(default_factory=VisualIntent)
    tags: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def bounded(self):
        if (self.start_ms is None) != (self.end_ms is None) or self.start_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("标注区间须完整且终点晚于起点；留空表示整条素材")
        if any(not row.strip() or len(row) > 64 for row in self.tags):
            raise ValueError("标签须为1至64字")
        return self


class SemanticSave(StoryModel):
    base_revision_id: str | None = Field(default=None, max_length=36)
    spans: list[SemanticSpan] = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=2000)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def unique(self):
        if len({row.id for row in self.spans}) != len(self.spans):
            raise ValueError("标注标识不能重复")
        return self


class RequirementOverride(StoryModel):
    media_types: list[Literal["video", "image"]] = Field(default_factory=lambda: ["video", "image"], min_length=1, max_length=2)
    orientation: Literal["any", "portrait", "landscape", "square"] = "any"
    required_tags: list[str] = Field(default_factory=list, max_length=30)
    excluded_tags: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def bounded(self):
        if any(not row.strip() or len(row) > 64 for row in self.required_tags + self.excluded_tags):
            raise ValueError("需求标签须为1至64字")
        if set(row.casefold().strip() for row in self.required_tags) & set(row.casefold().strip() for row in self.excluded_tags):
            raise ValueError("相同标签不能同时要求与排除")
        return self


class MatchCreate(StoryModel):
    story_revision_id: str = Field(min_length=1, max_length=36)
    asset_ids: list[str] = Field(min_length=1, max_length=100)
    overrides: dict[str, RequirementOverride] = Field(default_factory=dict, max_length=500)
    candidates_per_shot: int = Field(default=3, ge=1, le=5, strict=True)

    @model_validator(mode="after")
    def unique(self):
        if any(not row or len(row) > 36 for row in self.asset_ids) or len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("素材标识无效或重复")
        return self


class SourceChoice(StoryModel):
    shot_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    mode: Literal["candidate", "keep", "reshoot", "unassigned"]
    candidate_id: str | None = Field(default=None, max_length=64)
    locked: bool = False
    reviewed: bool = False
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def candidate_required(self):
        if (self.mode == "candidate") != (self.candidate_id is not None):
            raise ValueError("只有采用候选时须填写候选标识")
        if self.mode in {"unassigned", "reshoot"} and self.locked:
            raise ValueError("只有已采用的素材可以锁定")
        return self


class SourceSave(StoryModel):
    report_id: str = Field(min_length=1, max_length=36)
    base_revision_id: str | None = Field(default=None, max_length=36)
    choices: list[SourceChoice] = Field(min_length=1, max_length=500)
    unlock_shot_ids: list[str] = Field(default_factory=list, max_length=500)
    allow_reuse: bool = False

    @model_validator(mode="after")
    def unique(self):
        if len({row.shot_id for row in self.choices}) != len(self.choices):
            raise ValueError("每个镜头只能有一个素材决定")
        return self
