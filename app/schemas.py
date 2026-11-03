from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_serializer


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("payload", "metadata", "result_json", check_fields=False)
    def hide_undelivered_output_links(self, value):
        from .config import get_settings
        if get_settings().app_environment != "production":
            return value

        def clean(item):
            if isinstance(item, dict):
                return {key: "" if str(key).lower().replace("_", "") in {
                    "artifacturi", "outputuri", "previewuri", "downloaduri", "artifacturl", "outputurl",
                    "previewurl", "downloadurl",
                } else clean(child) for key, child in item.items()}
            if isinstance(item, list):
                return [clean(child) for child in item]
            return item

        return clean(value)


class UsageReconciliationRequest(BaseModel):
    evidence_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=2000)


class JobExecutionOptions(BaseModel):
    """Controls persisted retry budget for every asynchronous workflow."""

    max_attempts: int = Field(default=3, ge=1, le=10)


class TenantCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    settings: dict[str, Any] = Field(default_factory=dict)


class TenantRead(APIModel):
    id: str
    slug: str
    name: str
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TenantBudgetUpsert(BaseModel):
    """Tenant budget in integer micro-units (1 USD = 1,000,000)."""

    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    monthly_limit_micros: int | None = Field(default=None, ge=0)
    task_limit_micros: int | None = Field(default=None, ge=0)
    enforcement: Literal["block"] = "block"
    enabled: bool = True


class TenantBudgetRead(APIModel):
    id: str
    tenant_id: str
    currency: str
    monthly_limit_micros: int | None
    task_limit_micros: int | None
    enforcement: str
    enabled: bool
    period_key: str
    settled_micros: int
    reserved_micros: int
    remaining_micros: int | None
    created_at: datetime
    updated_at: datetime


class UsageLedgerRead(APIModel):
    id: str
    tenant_id: str
    job_id: str
    period_key: str
    status: str
    decision: str
    source: str
    provider: str | None
    model_id: str | None
    currency: str
    amount_micros: int
    usage: dict[str, Any] = Field(validation_alias="usage_json")
    cost: dict[str, Any] = Field(validation_alias="cost_json")
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectRead(APIModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    status: str
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    created_at: datetime
    updated_at: datetime


class AssetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=60)
    uri: str
    project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetAnalyzeCreate(JobExecutionOptions):
    """Options forwarded to the configured media-analysis Provider."""

    shot_analysis: bool = True
    transcribe: bool = False
    run_transcription: bool = False
    whisper_model: str = Field(default="small", min_length=1, max_length=100)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    options: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    quote_id: str | None = Field(default=None, min_length=1, max_length=64)
    force_new: bool = False


class AssetRead(APIModel):
    id: str
    tenant_id: str
    project_id: str | None
    name: str
    kind: str
    uri: str
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    created_at: datetime
    updated_at: datetime


class AssetLabelsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=2000)


class UploadIntentRead(BaseModel):
    asset_id: str
    project_id: str | None
    idempotency_key: str
    specification: dict[str, Any]
    status: str
    last_error_code: str | None
    expires_at: datetime | None
    created_at: datetime


class UploadSessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    project_id: str | None = None


class UploadSessionRead(BaseModel):
    upload_id: str
    asset_id: str
    upload_uri: str
    expires_at: datetime
    status: Literal["created"] = "created"


class RemoteUploadAttestation(BaseModel):
    """Proof issued by the configured external object-storage gateway."""

    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    signature: str = Field(
        min_length=64,
        max_length=71,
        pattern=r"^(?:sha256=)?[0-9a-fA-F]{64}$",
    )


class RemoteAssetRegister(BaseModel):
    """Register an object that is already stored outside the control plane."""

    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=60)
    uri: str = Field(min_length=1, max_length=4096)
    # Supplying the id lets a storage gateway bind a proof before the API
    # creates this record. It is required only when HMAC attestation is on.
    asset_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    project_id: str | None = None
    size_bytes: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    attestation: "RemoteUploadAttestation | None" = None


class RemoteUploadSessionCreate(BaseModel):
    """Create a direct-to-object-storage upload intent.

    The platform never signs or proxies bytes. ``upload_url`` is issued by a
    storage gateway or the caller's storage integration and is returned to the
    browser/mobile client unchanged. Set ``register_only`` when the object is
    already present and no upload is required.
    """

    name: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(gt=0)
    object_uri: str = Field(min_length=1, max_length=4096)
    # Existing-object registrations may choose the id before asking the
    # storage gateway to sign an attestation. Pending uploads may omit it.
    asset_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    upload_url: str | None = Field(default=None, max_length=8192)
    upload_method: Literal["PUT", "POST"] = "PUT"
    upload_headers: dict[str, str] = Field(default_factory=dict)
    register_only: bool = False
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Required for register_only sessions when STORAGE_ATTESTATION_SECRET is
    # configured. It is intentionally not used for a pending upload because
    # the completion proof must bind the asset after the bytes are uploaded.
    attestation: "RemoteUploadAttestation | None" = None


class RemoteUploadSessionRead(BaseModel):
    upload_id: str
    asset_id: str
    object_uri: str
    upload_url: str | None = None
    upload_method: Literal["PUT", "POST"] | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    upload_fields: dict[str, str] = Field(default_factory=dict)
    upload_file_field: str = "file"
    complete_uri: str | None = None
    expires_at: datetime
    status: Literal["created", "ready"]


class ArtifactAccessRequest(BaseModel):
    purpose: Literal["preview", "download"] = "preview"


class ArtifactAccessRead(BaseModel):
    artifact_id: str
    url: str
    expires_at: datetime
    purpose: Literal["preview", "download"]


class RemoteUploadComplete(BaseModel):
    """Client/storage-gateway attestation after a direct upload completes."""

    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    etag: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)
    attestation: RemoteUploadAttestation | None = None


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = "1"
    content: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = None


class TemplateRead(APIModel):
    id: str
    tenant_id: str
    project_id: str | None
    name: str
    version: str
    content: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TimelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    project_id: str
    fps: float = Field(default=30.0, gt=0)
    duration_ms: int = Field(default=0, ge=0)
    timeline: dict[str, Any] = Field(default_factory=dict)


class TimelineRead(APIModel):
    id: str
    tenant_id: str
    project_id: str
    name: str
    fps: float
    duration_ms: int
    timeline: dict[str, Any] = Field(validation_alias="timeline_json")
    created_at: datetime
    updated_at: datetime


class RenderRequest(JobExecutionOptions):
    quality: Literal["draft", "standard", "high", "studio"] = "draft"
    output: dict[str, Any] = Field(default_factory=lambda: {"format": "mp4"})
    options: dict[str, Any] = Field(default_factory=dict)
    quote_id: str | None = Field(default=None, min_length=1, max_length=64)


class TimelineValidationRead(BaseModel):
    valid: bool
    errors: int
    warnings: int
    issues: list[dict[str, Any]]


class JobCreate(BaseModel):
    type: str = Field(min_length=1, max_length=80)
    project_id: str | None = None
    status: str = "queued"
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    max_attempts: int = Field(default=3, ge=1, le=10)
    steps: list["JobStepCreate"] = Field(default_factory=list)


class JobStepCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sequence: int = Field(default=0, ge=0)
    status: str = "queued"
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    payload: dict[str, Any] = Field(default_factory=dict)


class JobStepUpdate(BaseModel):
    status: str | None = None
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    payload: dict[str, Any] | None = None
    error: str | None = None


class JobStatusUpdate(BaseModel):
    status: str
    error: str | None = None


class JobStepRead(APIModel):
    id: str
    tenant_id: str
    job_id: str
    name: str
    sequence: int
    status: str
    progress: float
    payload: dict[str, Any]
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobFailureRead(BaseModel):
    """Stable, tenant-safe explanation of the latest execution failure.

    The worker keeps a richer provider audit record internally.  API clients
    receive only this bounded projection so credentials, URLs and raw provider
    responses never become part of the frontend contract.
    """

    schema_version: str
    code: str
    message: str
    retryable: bool
    policy_blocked: bool = False
    provider: str | None = None
    capability: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str | None = None


JobCreate.model_rebuild()


class AutoEditCreate(JobExecutionOptions):
    project_id: str
    template_id: str
    asset_ids: list[str] = Field(default_factory=list)
    quality: Literal["draft", "standard", "high", "studio"] = "draft"
    target_duration: float | None = Field(default=None, ge=0.1, le=600, allow_inf_nan=False)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    quote_id: str | None = Field(default=None, min_length=1, max_length=64)


class JobRead(APIModel):
    id: str
    tenant_id: str
    project_id: str | None
    type: str
    status: str
    idempotency_key: str | None
    payload: dict[str, Any] = Field(validation_alias="public_payload")
    error: str | None = Field(validation_alias="public_error")
    attempt_count: int
    max_attempts: int
    next_retry_at: datetime | None
    retryable: bool
    failure_code: str | None
    policy_blocked: bool
    failure: JobFailureRead | None = None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobStatsRead(BaseModel):
    """Tenant-scoped aggregate counters for dashboards and operations."""

    total: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)
    by_failure_code: dict[str, int] = Field(default_factory=dict)
    retryable: int = Field(ge=0)
    policy_blocked: int = Field(ge=0)
    queued_retry: int = Field(ge=0)
    generated_at: datetime


class WorkflowStepRead(APIModel):
    id: str
    tenant_id: str
    workflow_run_id: str
    name: str
    sequence: int
    status: str
    progress: float
    attempt_count: int
    max_attempts: int
    worker_id: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    input_json: dict[str, Any]
    result_json: dict[str, Any]
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkflowRunRead(APIModel):
    id: str
    tenant_id: str
    job_id: str
    workflow_type: str
    runtime: str
    runtime_id: str | None
    status: str
    attempt_count: int
    max_attempts: int
    worker_id: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    next_retry_at: datetime | None
    input_json: dict[str, Any]
    result_json: dict[str, Any]
    error: str | None = Field(validation_alias="public_error")
    failure_code: str | None
    failure: JobFailureRead | None = None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkflowEventRead(APIModel):
    id: str
    tenant_id: str
    workflow_run_id: str
    sequence: int
    event_type: str
    worker_id: str | None
    payload: dict[str, Any]
    created_at: datetime


class WorkflowCreate(BaseModel):
    workflow_type: str | None = Field(default=None, min_length=1, max_length=120)
    runtime: str = Field(default="local", min_length=1, max_length=40)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    steps: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowLeaseUpdate(BaseModel):
    worker_id: str = Field(min_length=1, max_length=255)
    lease_seconds: int = Field(default=60, ge=1, le=3600)


class WorkflowRetryRequest(BaseModel):
    worker_id: str | None = Field(default=None, min_length=1, max_length=255)
    error: str = Field(default="operator requested retry", max_length=2000)
    failure_code: str | None = Field(default="operator_retry", max_length=120)
    delay_seconds: float = Field(default=0.0, ge=0, le=86400)


class JobRetryRequest(BaseModel):
    """Explicit retry request for a terminal provider/worker failure."""

    delay_seconds: float = Field(default=0.0, ge=0, le=86400)
    force: bool = False


class ArtifactCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=80)
    uri: str
    project_id: str | None = None
    job_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactRead(APIModel):
    id: str
    tenant_id: str
    project_id: str | None
    job_id: str | None
    job_status: str | None = None
    job_policy_blocked: bool = False
    edit_revision_number: int | None = None
    kind: str
    uri: str
    output_role: str = "primary"
    output_index: int = 0
    delivery_status: str = "pending"
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    created_at: datetime
    updated_at: datetime

    @field_serializer("uri")
    def access_only_through_delivery(self, value: str) -> str:
        from .config import get_settings
        return "" if get_settings().app_environment == "production" else value


class ProviderCallbackPayload(BaseModel):
    """Signed result event sent by a remote AI provider.

    The callback carries a URL only. The control plane never accepts binary
    media here and never performs inference in response to this request.
    """

    event_id: str = Field(min_length=1, max_length=255)
    # The gateway echoes the platform Job ID as its stable request ID.  It is
    # optional for backwards-compatible providers, but when present the API
    # binds it to the `{job_id}` path parameter before applying the event.
    request_id: str | None = Field(default=None, max_length=255)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    provider: str | None = Field(default=None, max_length=120)
    external_job_id: str | None = Field(default=None, max_length=255)
    artifact_uri: str | None = None
    artifact_type: str | None = Field(default=None, max_length=80)
    output_uri: str | None = None
    error: str | None = Field(default=None, max_length=2000)
    failure_code: str | None = Field(default=None, max_length=120)
    retryable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)


class ConsentCreate(BaseModel):
    subject_id: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=100)
    granted: bool = False
    project_id: str | None = None
    granted_at: datetime | None = None
    expires_at: datetime | None = None
    evidence_uri: str | None = None


class ConsentRead(APIModel):
    id: str
    tenant_id: str
    project_id: str | None
    subject_id: str
    scope: str
    granted: bool
    granted_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    evidence_uri: str | None
    created_at: datetime
    updated_at: datetime


class VoiceProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    consent_id: str
    project_id: str | None = None
    sample_asset_id: str | None = None
    provider: str = "pending"
    model_version: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["tts"])
    expires_at: datetime | None = None


class VoiceProfileRead(APIModel):
    id: str
    tenant_id: str
    project_id: str | None
    consent_id: str
    name: str
    provider: str
    model_version: str | None
    sample_asset_id: str | None
    status: str
    scopes: list[str]
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AvatarProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    consent_id: str
    project_id: str | None = None
    reference_asset_id: str | None = None
    provider: str = "pending"
    model_version: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["avatar", "lip_sync"])
    expires_at: datetime | None = None


class AvatarProfileRead(APIModel):
    id: str
    tenant_id: str
    project_id: str | None
    consent_id: str
    name: str
    provider: str
    model_version: str | None
    reference_asset_id: str | None
    status: str
    scopes: list[str]
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class VoiceJobCreate(JobExecutionOptions):
    project_id: str
    voice_profile_id: str
    text: str = Field(min_length=1, max_length=200_000)
    language: str = Field(default="zh-CN", min_length=2, max_length=20)
    provider: str | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=160)
    model_version: str | None = Field(default=None, min_length=1, max_length=120)
    preserve_performance: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class AvatarJobCreate(JobExecutionOptions):
    project_id: str
    avatar_profile_id: str
    source_asset_id: str
    mode: Literal["lip_sync", "motion_transfer", "face_replace"]
    audio_asset_id: str | None = None
    provider: str | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=160)
    model_version: str | None = Field(default=None, min_length=1, max_length=120)
    options: dict[str, Any] = Field(default_factory=dict)


class VideoGenerationJobCreate(JobExecutionOptions):
    project_id: str
    prompt: str = Field(min_length=1, max_length=20_000)
    reference_asset_ids: list[str] = Field(default_factory=list)
    provider: str | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=160)
    model_version: str | None = Field(default=None, min_length=1, max_length=120)
    quality: Literal["draft", "standard", "high", "studio"] = "draft"
    options: dict[str, Any] = Field(default_factory=dict)


class EnhancementJobCreate(JobExecutionOptions):
    project_id: str
    asset_id: str
    operations: list[Literal["denoise", "upscale", "stabilize", "interpolate", "deinterlace", "hdr", "authorized_object_cleanup"]] = Field(default_factory=list)
    rights_confirmed: bool = False
    model_id: str | None = Field(default=None, min_length=1, max_length=160)
    model_version: str | None = Field(default=None, min_length=1, max_length=120)
    options: dict[str, Any] = Field(default_factory=dict)
