"""Personal AI production inputs; vendor credentials and prices are server-owned."""
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StoryBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)
    shot_id: str = Field(min_length=1, max_length=100)


class ProductionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    operation: Literal["video_generation", "face_replace", "person_replace"]
    prompt: str = Field(default="", max_length=20000)
    source_asset_id: str | None = None
    avatar_profile_id: str | None = None
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=4)
    duration_seconds: float = Field(default=5.0, ge=2, le=30)
    options: dict = Field(default_factory=dict)
    provider: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    scenario_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    scenario_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    storyboard: StoryBinding | None = None

    @model_validator(mode="after")
    def business_inputs(self):
        self.prompt = self.prompt.strip()
        if bool(self.scenario_id) != bool(self.scenario_version):
            raise ValueError("场景与版本必须一起选择")
        if self.operation == "video_generation":
            if not self.prompt:
                raise ValueError("补镜头需要画面描述")
            if self.source_asset_id or self.avatar_profile_id:
                raise ValueError("补镜头请使用参考素材列表")
        elif not self.source_asset_id or not self.avatar_profile_id:
            raise ValueError("换脸/换人需要原视频和已授权人物档案")
        elif self.reference_asset_ids or self.storyboard:
            raise ValueError("换脸/换人不接受补镜头参考列表或分镜绑定")
        if self.operation == "person_replace" and self.prompt:
            raise ValueError("人物替换使用原视频动作与时长，不接受文字生成描述")
        if len(set(self.reference_asset_ids)) != len(self.reference_asset_ids):
            raise ValueError("参考素材不能重复")
        if len(self.options) > 16:
            raise ValueError("生成参数过多")
        for name, value in self.options.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or type(value) not in {str, int, float, bool}:
                raise ValueError("生成参数需为受模型约束的简单值")
            if type(value) in {int, float} and not math.isfinite(value) or isinstance(value, str) and len(value) > 20000:
                raise ValueError("生成参数超出范围")
        seed = self.options.get("seed")
        if seed is not None and type(seed) is not int:
            raise ValueError("seed需要是模型支持范围内的整数")
        negative = self.options.get("negative_prompt", "")
        if not isinstance(negative, str) or len(negative) > 10000:
            raise ValueError("排除描述无效")
        return self


class ProductionConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(min_length=1, max_length=64)


class ProductionAdopt(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdentityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    reference_asset_id: str = Field(min_length=1, max_length=64)
    subject_id: str = Field(min_length=1, max_length=200)
    granted: Literal[True]
