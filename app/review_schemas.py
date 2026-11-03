"""Client-authored review text; identity, quality and media hashes stay server-owned."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    category: Literal["picture", "audio", "subtitle", "content", "other"] = "other"
    severity: Literal["error", "warning", "info"] = "error"
    message: str = Field(min_length=1, max_length=2000)
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if not self.message.strip():
            raise ValueError("请填写问题说明")
        if self.end is not None and (self.start is None or self.end < self.start):
            raise ValueError("问题结束时间不能早于开始时间")
        return self


class ReviewSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_review_id: str | None = Field(default=None, min_length=1, max_length=64)
    decision: Literal["approved", "changes_requested"]
    note: str = Field(default="", max_length=4000)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def decision_consistent(self):
        if self.decision == "changes_requested" and not self.note.strip() and not self.issues:
            raise ValueError("退回时请填写意见或添加问题")
        if self.decision == "approved" and any(issue.severity == "error" for issue in self.issues):
            raise ValueError("仍有错误级问题，不能批准本成片")
        return self
