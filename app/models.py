from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ProjectBudget(TimestampMixin, Base):
    __tablename__ = "project_budgets"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    limit_micros: Mapped[int | None] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)


class ProductionBatch(TimestampMixin, Base):
    __tablename__ = "production_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    shared_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    submission_quote_id: Mapped[str | None] = mapped_column(String(36))
    aggregate_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_production_batch_create"),)


class BatchItem(TimestampMixin, Base):
    __tablename__ = "batch_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    client_key: Mapped[str] = mapped_column(String(80), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    overrides_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    config_snapshot: Mapped[dict | None] = mapped_column(JSON)
    plan_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    __table_args__ = (UniqueConstraint("batch_id", "client_key", name="uq_batch_item_client_key"),)


class BatchRevision(TimestampMixin, Base):
    __tablename__ = "batch_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), index=True, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (UniqueConstraint("batch_id", "number", name="uq_batch_revision_number"),
                      UniqueConstraint("batch_id", "idempotency_key", name="uq_batch_revision_request"))


class BatchQuote(TimestampMixin, Base):
    __tablename__ = "batch_quotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), index=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    analysis_quote_id: Mapped[str] = mapped_column(ForeignKey("preparation_quotes.id"), nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_key: Mapped[str | None] = mapped_column(String(255))
    result_json: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_batch_quote_request"),
                      UniqueConstraint("tenant_id", "consumed_key", name="uq_batch_confirmation"))


class PreparationQuote(TimestampMixin, Base):
    __tablename__ = "preparation_quotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_key: Mapped[str | None] = mapped_column(String(255))
    result_json: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_preparation_quote_intent"),
                      UniqueConstraint("tenant_id", "consumed_key", name="uq_preparation_confirmation"))


class BatchRenderQuote(TimestampMixin, Base):
    __tablename__ = "batch_render_quotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_key: Mapped[str | None] = mapped_column(String(255))
    result_json: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_batch_render_quote_request"),
                      UniqueConstraint("tenant_id", "consumed_key", name="uq_batch_render_confirmation"))


class BatchRun(TimestampMixin, Base):
    __tablename__ = "batch_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), index=True, nullable=False)
    quote_id: Mapped[str] = mapped_column(ForeignKey("batch_render_quotes.id"), unique=True, nullable=False)
    parent_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    quality: Mapped[str] = mapped_column(String(20), nullable=False)
    aggregate_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchRunItem(TimestampMixin, Base):
    __tablename__ = "batch_run_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("batch_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    batch_item_id: Mapped[str] = mapped_column(ForeignKey("batch_items.id"), index=True, nullable=False)
    revision_id: Mapped[str] = mapped_column(ForeignKey("plan_revisions.id"), nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    __table_args__ = (UniqueConstraint("run_id", "batch_item_id", name="uq_batch_run_item"),)


class BatchTaskAction(TimestampMixin, Base):
    __tablename__ = "batch_task_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), index=True, nullable=False)
    phase_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_batch_task_action_request"),)


class RenderQuote(TimestampMixin, Base):
    __tablename__ = "render_quotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    timeline_id: Mapped[str] = mapped_column(ForeignKey("timelines.id", ondelete="CASCADE"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_key: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_render_quote_intent"),)


class JobAction(TimestampMixin, Base):
    __tablename__ = "job_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("job_id", "idempotency_key", name="uq_job_action_intent"),)


class BatchReviewAction(TimestampMixin, Base):
    __tablename__ = "batch_review_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("batch_runs.id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_batch_review_request"),)


class DeliveryPackageRequest(TimestampMixin, Base):
    __tablename__ = "delivery_package_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("batch_runs.id", ondelete="CASCADE"), nullable=False)
    package_id: Mapped[str | None] = mapped_column(ForeignKey("delivery_packages.id", ondelete="SET NULL"))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_package_creation_receipt"),)


class DeliveryPackage(TimestampMixin, Base):
    __tablename__ = "delivery_packages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("production_batches.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("batch_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    files_json: Mapped[list] = mapped_column(JSON, nullable=False)
    remote_state: Mapped[str | None] = mapped_column(String(40))
    remote_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    object_uri: Mapped[str | None] = mapped_column(String(2048))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_delivery_package_request"),)


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    login_name: Mapped[str | None] = mapped_column(String(40), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    credential_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    personal_tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), unique=True, nullable=False)


class Membership(TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_membership_user_tenant"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="owner", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class LegacyTenantAdoption(Base):
    """An explicit operator binding; the original business Tenant stays intact."""
    __tablename__ = 'legacy_tenant_adoptions'
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey('tenants.id',ondelete='CASCADE'),unique=True,nullable=False)
    user_id: Mapped[str | None] = mapped_column(ForeignKey('users.id',ondelete='SET NULL'))
    database_instance_id: Mapped[str] = mapped_column(String(36),nullable=False)
    intent_hash: Mapped[str] = mapped_column(String(64),nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON,nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)


class ExternalIdentity(TimestampMixin, Base):
    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("provider", "app_id", "subject_id", name="uq_external_identity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    app_id: Mapped[str] = mapped_column(String(100), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Provider identities are linked explicitly. Union IDs, names or phone
    # numbers never silently merge two existing personal spaces.


class UserSession(TimestampMixin, Base):
    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    credential_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    client_type: Mapped[str] = mapped_column(String(12), nullable=False)
    login_method: Mapped[str] = mapped_column(String(40), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("user_sessions.id"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthThrottle(Base):
    __tablename__ = "auth_throttles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)


class AuthEvent(Base):
    __tablename__ = "auth_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("user_sessions.id"))
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Never record passwords, tokens, WeChat codes, AppSecret or session_key.
    details_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class TenantBudget(TimestampMixin, Base):
    """Tenant-level spending policy for hosted provider executions.

    Monetary values are stored as integer micro-units (one USD is
    ``1_000_000``) so provider float payloads never become the source of
    truth for enforcement or reconciliation.
    """

    __tablename__ = "tenant_budgets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    monthly_limit_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    task_limit_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enforcement: Mapped[str] = mapped_column(String(20), default="block", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class UsageLedger(TimestampMixin, Base):
    """Immutable provider charge envelope, updated only for one Job's final charge.

    A single row per Job makes webhook retries and polling retries idempotent;
    the raw provider ``usage``/``cost`` payloads remain available for audit.
    """

    __tablename__ = "usage_ledger"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    period_key: Mapped[str] = mapped_column(String(7), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="reserved", nullable=False)
    decision: Mapped[str] = mapped_column(String(40), default="unconfigured", nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(120))
    model_id: Mapped[str | None] = mapped_column(String(160))
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    amount_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    cost_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="active", nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Asset(TimestampMixin, Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AssetLabel(Base):
    __tablename__ = "asset_labels"
    __table_args__ = (UniqueConstraint("asset_id", "tag_key", name="uq_asset_label_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True, nullable=False)
    tag_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)


class ReferenceUsage(TimestampMixin, Base):
    __tablename__ = "reference_usage"
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    allow_output: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ReferenceAnalysis(TimestampMixin, Base):
    __tablename__ = "reference_analyses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    request_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (UniqueConstraint("project_id", "request_key", name="uq_reference_request"),)


class StoryboardDocument(TimestampMixin, Base):
    __tablename__ = "storyboard_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    reference_id: Mapped[str | None] = mapped_column(ForeignKey("reference_analyses.id", ondelete="RESTRICT"))
    request_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    head_revision_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("project_id", "request_key", name="uq_storyboard_create_request"),)


class StoryboardRevision(TimestampMixin, Base):
    __tablename__ = "storyboard_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("storyboard_documents.id", ondelete="CASCADE"), index=True, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (UniqueConstraint("document_id", "request_key", name="uq_storyboard_revision_request"),
                      UniqueConstraint("document_id", "number", name="uq_storyboard_revision_number"))


class AssetSemanticRevision(TimestampMixin, Base):
    __tablename__ = "asset_semantic_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (UniqueConstraint("asset_id", "number", name="uq_asset_semantic_number"),
                      UniqueConstraint("asset_id", "request_key", name="uq_asset_semantic_request"))


class ShotMatchReport(TimestampMixin, Base):
    __tablename__ = "shot_match_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("storyboard_documents.id", ondelete="CASCADE"), index=True, nullable=False)
    story_revision_id: Mapped[str] = mapped_column(ForeignKey("storyboard_revisions.id", ondelete="CASCADE"), nullable=False)
    request_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    inputs_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (UniqueConstraint("document_id", "request_key", name="uq_shot_match_request"),)


class StoryboardSourceRevision(TimestampMixin, Base):
    __tablename__ = "storyboard_source_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("storyboard_documents.id", ondelete="CASCADE"), index=True, nullable=False)
    story_revision_id: Mapped[str] = mapped_column(ForeignKey("storyboard_revisions.id", ondelete="CASCADE"), nullable=False)
    report_id: Mapped[str] = mapped_column(ForeignKey("shot_match_reports.id", ondelete="CASCADE"), nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (UniqueConstraint("document_id", "number", name="uq_storyboard_source_number"),
                      UniqueConstraint("document_id", "request_key", name="uq_storyboard_source_request"))


class StoryboardSourceHead(Base):
    __tablename__ = "storyboard_source_heads"
    document_id: Mapped[str] = mapped_column(ForeignKey("storyboard_documents.id", ondelete="CASCADE"), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("storyboard_source_revisions.id", ondelete="CASCADE"), nullable=False)


class StorageUploadIntent(TimestampMixin, Base):
    """Stable identity for a direct upload; never stores presigned credentials."""

    __tablename__ = "storage_upload_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="requesting", nullable=False)
    object_uri: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))

    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_storage_intents_idempotency"),)


class Template(TimestampMixin, Base):
    __tablename__ = "templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(40), default="1", nullable=False)
    content: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Timeline(TimestampMixin, Base):
    __tablename__ = "timelines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    fps: Mapped[float] = mapped_column(Float, default=30.0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    timeline_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class EditDocument(TimestampMixin, Base):
    __tablename__ = "edit_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    origin_job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class PlanRevision(Base):
    """Append-only authoring input, selected shots and operation receipt."""
    __tablename__ = "plan_revisions"
    __table_args__ = (UniqueConstraint("document_id", "number", name="uq_plan_revision_number"),
                      UniqueConstraint("document_id", "idempotency_key", name="uq_plan_revision_request"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True, nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("edit_documents.id"), index=True, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(ForeignKey("plan_revisions.id"))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    sources_json: Mapped[list] = mapped_column(JSON, nullable=False)
    plan_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    operation_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TimelineRevision(Base):
    __tablename__ = "timeline_revisions"
    id: Mapped[str] = mapped_column(ForeignKey("plan_revisions.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True, nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("edit_documents.id"), index=True, nullable=False)
    timeline_id: Mapped[str] = mapped_column(ForeignKey("timelines.id"), unique=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    timeline_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class EditHead(Base):
    __tablename__ = "edit_heads"
    document_id: Mapped[str] = mapped_column(ForeignKey("edit_documents.id"), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("plan_revisions.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False)
    # Idempotency keys are scoped to a tenant so two tenants can safely reuse
    # the same client generated key.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    # Execution controls are persisted so a worker restart cannot reset the
    # retry budget.  ``attempt_count`` is incremented atomically by claim_job.
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Fences writes by a worker whose claim was superseded by recovery/retry.
    execution_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(120))
    policy_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_jobs_tenant_idempotency_key"),)

    @property
    def failure(self) -> dict | None:
        # Keep this as a lazy import: models are imported while the service
        # modules are being initialized, so importing diagnostics at module
        # load time would create a circular dependency.
        from .services.failure_diagnostics import job_failure

        return job_failure(self)

    @property
    def public_error(self) -> str | None:
        from .services.failure_diagnostics import public_error

        return public_error(self)

    @property
    def public_payload(self) -> dict:
        from .services.failure_diagnostics import public_payload

        return public_payload(self)


class JobStep(TimestampMixin, Base):
    """Durable progress records for a job's individual units of work."""

    __tablename__ = "job_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("job_id", "name", name="uq_job_steps_job_name"),)


class WorkflowRun(TimestampMixin, Base):
    """Durable execution lease for a long-running Job.

    ``Job`` remains the public control-plane contract.  A WorkflowRun adds the
    runtime state needed by a polling worker or Temporal worker to safely
    resume work after a process crash without putting lease details in the
    user-facing job payload.
    """

    __tablename__ = "workflow_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(120), nullable=False)
    runtime: Mapped[str] = mapped_column(String(40), default="local", nullable=False)
    runtime_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(255), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("job_id", name="uq_workflow_runs_job"),)

    @property
    def failure(self) -> dict | None:
        from .services.failure_diagnostics import workflow_failure

        return workflow_failure(self)

    @property
    def public_error(self) -> str | None:
        from .services.failure_diagnostics import public_error

        return public_error(self)


class WorkflowStep(TimestampMixin, Base):
    """Durable activity-level state associated with a WorkflowRun."""

    __tablename__ = "workflow_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True, nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(255), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("workflow_run_id", "name", name="uq_workflow_steps_run_name"),)


class WorkflowEvent(Base):
    """Append-only event/audit stream for workflow recovery and debugging."""

    __tablename__ = "workflow_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("workflow_run_id", "sequence", name="uq_workflow_events_run_sequence"),)


class Artifact(TimestampMixin, Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_role: Mapped[str] = mapped_column(String(80), default="primary", server_default="primary", nullable=False)
    output_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(40), default="pending", server_default="pending", nullable=False)
    legacy_duplicate_of: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (UniqueConstraint("job_id", "output_role", "output_index", name="uq_artifacts_job_output"),)


class ArtifactReview(Base):
    """Append-only human decision about exact rendered bytes and inputs."""
    __tablename__ = "artifact_reviews"
    __table_args__ = (UniqueConstraint("artifact_id", "number", name="uq_artifact_review_number"),
                     UniqueConstraint("artifact_id", "idempotency_key", name="uq_artifact_review_request"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True, nullable=False)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"), index=True, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_review_id: Mapped[str | None] = mapped_column(ForeignKey("artifact_reviews.id"))
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    issues_json: Mapped[list] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ArtifactShare(Base):
    """Revocable capability for an exact approved output; never stores a token."""
    __tablename__ = "artifact_shares"
    __table_args__ = (UniqueConstraint("artifact_id", "idempotency_key", name="uq_artifact_share_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True, nullable=False)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"), index=True, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    review_id: Mapped[str] = mapped_column(ForeignKey("artifact_reviews.id"), nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    audience: Mapped[str] = mapped_column(String(30), nullable=False)
    allow_download: Mapped[bool] = mapped_column(Boolean, nullable=False)
    include_subtitles: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ArtifactStorage(TimestampMixin, Base):
    __tablename__ = "artifact_storage"
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    etag: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)


class ArtifactArchiveTask(TimestampMixin, Base):
    __tablename__ = "artifact_archive_tasks"
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(120))


class DispatchClock(Base):
    """Admission-only mutex; never held across execution or provider IO."""
    __tablename__ = "dispatch_clock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class DispatchTurn(Base):
    __tablename__ = "dispatch_turns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_key: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_key: Mapped[str] = mapped_column(String(36), nullable=False)
    family: Mapped[str] = mapped_column(String(20), nullable=False)
    mode: Mapped[str] = mapped_column(String(12), nullable=False)
    last_turn: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "project_key", "batch_key", "family", "mode", name="uq_dispatch_lane"),)


class JobDispatchStamp(Base):
    __tablename__ = "job_dispatch_stamps"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    last_turn: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ProviderDispatchGate(Base):
    """Provider/capability mutex; counts the existing operation identities."""
    __tablename__ = "provider_dispatch_gates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(160), nullable=False)
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    __table_args__ = (UniqueConstraint("provider", "capability", name="uq_provider_dispatch_gate"),)


class JobTrace(Base):
    __tablename__ = 'job_traces'
    job_id: Mapped[str] = mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RuntimeCounter(Base):
    __tablename__ = 'runtime_counters'
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class WorkerPulse(Base):
    __tablename__ = 'worker_pulses'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    worker_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatabaseRecoveryState(TimestampMixin, Base):
    __tablename__ = 'database_recovery_state'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, autoincrement=False)
    instance_id: Mapped[str] = mapped_column(String(36), default=new_id, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default='active', nullable=False)
    backup_id: Mapped[str | None] = mapped_column(String(36))


class OperationalAlert(Base):
    __tablename__ = 'operational_alerts'
    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(12), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ProviderOperation(TimestampMixin, Base):
    """One billable logical operation; polling claims keep the same identity."""

    __tablename__ = "provider_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False)
    request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_key: Mapped[str | None] = mapped_column(String(160))
    capability: Mapped[str | None] = mapped_column(String(80))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    external_job_id: Mapped[str | None] = mapped_column(String(255))
    execution_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(40), default="created", nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class ProviderEvent(TimestampMixin, Base):
    """Provider event IDs retain their meaning across every delivery."""

    __tablename__ = "provider_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False)
    provider_key: Mapped[str] = mapped_column(String(160), nullable=False)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    disposition: Mapped[str] = mapped_column(String(40), default="received", nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (UniqueConstraint("provider_key", "event_id", name="uq_provider_events_identity"),)


class UsageEvidence(TimestampMixin, Base):
    """Append-only observed charges, including late and conflicting results.

    Corrections await reconciliation rather than replacing a settled ledger.
    Unknown amounts remain null, never a fabricated zero-cost settlement.
    """

    __tablename__ = "usage_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_micros: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    cost_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (UniqueConstraint("job_id", "evidence_hash", name="uq_usage_evidence_identity"),)


class UsageAdjustment(TimestampMixin, Base):
    """An approved delta; never an overwrite of the original charge."""

    __tablename__ = "usage_adjustments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("usage_evidence.id", ondelete="RESTRICT"), unique=True, nullable=False)
    period_key: Mapped[str] = mapped_column(String(7), index=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    delta_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class OutboxEvent(TimestampMixin, Base):
    """Transactionally recorded notifications; consumers deduplicate by ID."""

    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("job_id", "execution_generation", "event_type", name="uq_outbox_job_event"),)


class Consent(TimestampMixin, Base):
    __tablename__ = "consents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    subject_id: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(100), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_uri: Mapped[str | None] = mapped_column(Text)


class VoiceProfile(TimestampMixin, Base):
    __tablename__ = "voice_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    consent_id: Mapped[str] = mapped_column(ForeignKey("consents.id", ondelete="RESTRICT"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), default="pending", nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(100))
    sample_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="consent_pending", nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AvatarProfile(TimestampMixin, Base):
    __tablename__ = "avatar_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    consent_id: Mapped[str] = mapped_column(ForeignKey("consents.id", ondelete="RESTRICT"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), default="pending", nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(100))
    reference_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="consent_pending", nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
