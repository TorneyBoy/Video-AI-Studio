"""Drafts may be incomplete; quotation resolves and validates every item."""
from typing import Literal
from .review_schemas import ReviewSubmit
from pydantic import BaseModel,ConfigDict,Field,model_validator


class BatchSettings(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    template_id: str | None=Field(default=None,min_length=1,max_length=64)
    target_duration: float | None=Field(default=None,ge=.1,le=600,allow_inf_nan=False)
    aspect_ratio: Literal['9:16','16:9','1:1'] | None='9:16'
    asset_ids: list[str]=Field(default_factory=list,max_length=100)


class BatchOverrides(BatchSettings):
    pass


class BatchItemInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    key: str=Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9_-]+$')
    name: str=Field(default='',max_length=200)
    overrides: BatchOverrides=Field(default_factory=BatchOverrides)


class BatchDraft(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name: str=Field(min_length=1,max_length=200)
    shared: BatchSettings=Field(default_factory=BatchSettings)
    items: list[BatchItemInput]=Field(default_factory=list,max_length=100)

    @model_validator(mode='after')
    def unique_keys(self):
        if len({row.key for row in self.items})!=len(self.items):raise ValueError('每项须使用不同的标识')
        if not self.name.strip():raise ValueError('请输入批次名称')
        return self


class BatchCreate(BatchDraft):
    project_id: str=Field(min_length=1,max_length=64)


class BatchSave(BatchDraft):
    base_revision: int=Field(ge=1)


class BatchQuoteRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    base_revision: int=Field(ge=1)


class BatchConfirmRequest(BatchQuoteRequest):
    quote_id: str=Field(min_length=1,max_length=64)


class BatchRenderSelection(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    key: str=Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9_-]+$')
    revision_id: str=Field(min_length=1,max_length=64)


class BatchRenderCreate(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    items: list[BatchRenderSelection]=Field(min_length=1,max_length=100)
    quality: Literal['draft','standard','high','studio']='high'
    force_new: bool=False

    @model_validator(mode='after')
    def unique_items(self):
        if len({item.key for item in self.items})!=len(self.items):raise ValueError('每个子项只能选择一次')
        return self


class BatchRenderConfirm(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    quote_id: str=Field(min_length=1,max_length=64)


class BatchTaskSelection(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    key: str=Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9_-]+$')
    job_id: str=Field(min_length=1,max_length=64)
    expected_status: Literal['queued','running','failed','succeeded','cancelled']
    expected_attempt: int=Field(ge=0)
    expected_generation: int=Field(ge=0)


class BatchTaskRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    action: Literal['retry','cancel']
    items: list[BatchTaskSelection]=Field(min_length=1,max_length=100)

    @model_validator(mode='after')
    def unique_items(self):
        if len({item.key for item in self.items})!=len(self.items) or len({item.job_id for item in self.items})!=len(self.items):raise ValueError('每个原任务只能选择一次')
        return self


class BatchReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    artifact_id: str = Field(min_length=1,max_length=64)
    review: ReviewSubmit


class BatchReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BatchReviewItem] = Field(min_length=1,max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len({item.key for item in self.items})!=len(self.items) or len({item.artifact_id for item in self.items})!=len(self.items):
            raise ValueError("请为每份成片只选择一项审核")
        return self


class BatchDeliverySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[str] = Field(min_length=1,max_length=100)
    include_subtitles: bool = False

    @model_validator(mode="after")
    def unique(self):
        import re
        if len(set(self.keys))!=len(self.keys) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',key) for key in self.keys):
            raise ValueError("批次子项选择无效或重复")
        return self


class BatchPackageCreate(BatchDeliverySelection):
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
