from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ShareCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="视频分享", min_length=1, max_length=120)
    audience: Literal["workspace", "anyone_with_link"] = "workspace"
    allow_download: bool = False
    include_subtitles: bool = False
    expires_in_seconds: int = Field(default=86400, ge=60, le=604800)

    @model_validator(mode="after")
    def scoped_files(self):
        if self.include_subtitles and not self.allow_download:
            raise ValueError("分享字幕文件需要同时允许下载")
        if not self.label.strip():
            raise ValueError("请填写分享名称")
        return self


class SharedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=200, repr=False)


class SharedAccess(SharedRequest):
    purpose: Literal["preview", "download"] = "preview"


class SharedFile(SharedRequest):
    kind: Literal["manifest", "srt", "vtt"] = "manifest"
