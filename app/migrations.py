"""Small, dependency-free schema migration runner.

The control plane deliberately keeps migrations inside the application package
instead of requiring Alembic for the first production cut.  ``create_all`` is
only used to bootstrap an empty or legacy database; every subsequent schema
change must be represented by a numbered migration in ``MIGRATIONS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine


MigrationApply = Callable[[Connection], None]


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    apply: MigrationApply


MIGRATION_TABLE = "schema_migrations"


def _ensure_migration_table(connection: Connection) -> None:
    # The SQL is intentionally accepted by both PostgreSQL and SQLite.  The
    # application uses timezone-aware values on PostgreSQL and stores the same
    # ISO-compatible value on SQLite's dynamically typed timestamp column.
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(64) PRIMARY KEY,
                description VARCHAR(255) NOT NULL,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL
            )
            """
        )
    )


def _applied_versions(connection: Connection) -> set[str]:
    rows = connection.execute(text("SELECT version FROM schema_migrations")).scalars()
    return set(rows)


def _record_migration(connection: Connection, migration: Migration | tuple[str, str]) -> None:
    version, description = (
        (migration.version, migration.description)
        if isinstance(migration, Migration)
        else migration
    )
    # ON CONFLICT makes the baseline marker safe when two local processes
    # start against the same empty database.  The documented production path
    # still runs this command once before scaling API/worker replicas.
    connection.execute(
        text(
            """
            INSERT INTO schema_migrations(version, description, applied_at)
            VALUES (:version, :description, :applied_at)
            ON CONFLICT (version) DO NOTHING
            """
        ),
        {
            "version": version,
            "description": description,
            "applied_at": datetime.now(timezone.utc),
        },
    )


def _add_job_runtime_columns(connection: Connection) -> None:
    """Add columns introduced after the original SQLite/Postgres schema.

    Existing rows receive safe defaults for non-null execution controls.  The
    operation is additive and does not rewrite or inspect media payloads.
    """

    inspector = inspect(connection)
    if "jobs" not in inspector.get_table_names():
        raise RuntimeError("cannot apply jobs migration: jobs table is missing")

    existing = {column["name"] for column in inspector.get_columns("jobs")}
    dialect = connection.dialect.name
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    false_default = "FALSE" if dialect == "postgresql" else "0"
    columns = {
        "idempotency_key": ("VARCHAR(255)", None, False),
        "attempt_count": ("INTEGER", "0", True),
        "max_attempts": ("INTEGER", "3", True),
        "next_retry_at": (timestamp_type, None, False),
        "retryable": ("BOOLEAN", false_default, True),
        "failure_code": ("VARCHAR(120)", None, False),
        "policy_blocked": ("BOOLEAN", false_default, True),
        "started_at": (timestamp_type, None, False),
        "completed_at": (timestamp_type, None, False),
    }
    for name, (column_type, default, non_null) in columns.items():
        if name in existing:
            continue
        default_sql = f" DEFAULT {default}" if default is not None else ""
        nullability_sql = " NOT NULL" if non_null else ""
        # PostgreSQL supports IF NOT EXISTS; SQLite is safe here because the
        # inspector check and the ALTER are executed in one migration run.
        if dialect == "postgresql":
            connection.execute(
                text(
                    f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {name} "
                    f"{column_type}{default_sql}{nullability_sql}"
                )
            )
        else:
            connection.execute(
                text(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}{default_sql}{nullability_sql}")
            )

    # Match the indexes declared by the ORM model for old databases.  A
    # composite unique index is the portable equivalent of the named unique
    # constraint on SQLite; PostgreSQL receives the actual constraint.
    index_names = {index["name"] for index in inspect(connection).get_indexes("jobs")}
    unique_names = {
        constraint.get("name")
        for constraint in inspect(connection).get_unique_constraints("jobs")
        if constraint.get("name")
    }
    if "ix_jobs_idempotency_key" not in index_names:
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_idempotency_key ON jobs (idempotency_key)"))
    if "ix_jobs_next_retry_at" not in index_names:
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_next_retry_at ON jobs (next_retry_at)"))

    unique_name = "uq_jobs_tenant_idempotency_key"
    if unique_name not in unique_names and unique_name not in index_names:
        if dialect == "postgresql":
            connection.execute(
                text(
                    "ALTER TABLE jobs ADD CONSTRAINT uq_jobs_tenant_idempotency_key "
                    "UNIQUE (tenant_id, idempotency_key)"
                )
            )
        else:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_tenant_idempotency_key "
                    "ON jobs (tenant_id, idempotency_key)"
                )
            )


def _create_budget_tables(connection: Connection) -> None:
    """Create the tenant budget and provider usage ledger tables.

    This migration is deliberately explicit instead of relying on
    ``Base.metadata.create_all``: a production database may already have a
    migration baseline from before budget enforcement was introduced.
    """

    dialect = connection.dialect.name
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    true_default = "TRUE" if dialect == "postgresql" else "1"
    json_type = "JSONB" if dialect == "postgresql" else "JSON"
    json_default = "'{}'::jsonb" if dialect == "postgresql" else "'{}'"
    timestamp_default = "NOW()" if dialect == "postgresql" else "CURRENT_TIMESTAMP"
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS tenant_budgets (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(36) NOT NULL UNIQUE,
                currency VARCHAR(3) NOT NULL DEFAULT 'USD',
                monthly_limit_micros BIGINT,
                task_limit_micros BIGINT,
                enforcement VARCHAR(20) NOT NULL DEFAULT 'block',
                enabled BOOLEAN NOT NULL DEFAULT {true_default},
                created_at {timestamp_type} NOT NULL DEFAULT {timestamp_default},
                updated_at {timestamp_type} NOT NULL DEFAULT {timestamp_default},
                CONSTRAINT fk_tenant_budgets_tenant
                    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS usage_ledger (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(36) NOT NULL,
                job_id VARCHAR(36) NOT NULL UNIQUE,
                period_key VARCHAR(7) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'reserved',
                decision VARCHAR(40) NOT NULL DEFAULT 'unconfigured',
                source VARCHAR(40) NOT NULL,
                provider VARCHAR(120),
                model_id VARCHAR(160),
                currency VARCHAR(3) NOT NULL DEFAULT 'USD',
                amount_micros BIGINT NOT NULL DEFAULT 0,
                usage_json {json_type} NOT NULL DEFAULT {json_default},
                cost_json {json_type} NOT NULL DEFAULT {json_default},
                metadata_json {json_type} NOT NULL DEFAULT {json_default},
                created_at {timestamp_type} NOT NULL DEFAULT {timestamp_default},
                updated_at {timestamp_type} NOT NULL DEFAULT {timestamp_default},
                CONSTRAINT fk_usage_ledger_tenant
                    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
                CONSTRAINT fk_usage_ledger_job
                    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
            """
        )
    )
    # Keep query plans equivalent to the ORM's index=True declarations when
    # the tables predate this migration or were created by a partial deploy.
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_tenant_budgets_tenant_id ON tenant_budgets (tenant_id)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_ledger_tenant_id ON usage_ledger (tenant_id)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_ledger_job_id ON usage_ledger (job_id)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_ledger_period_key ON usage_ledger (period_key)"))


def _add_completion_contract(connection: Connection) -> None:
    """Add durable execution/result identity without deleting legacy evidence."""
    from . import models

    columns = {column["name"] for column in inspect(connection).get_columns("jobs")}
    if "execution_generation" not in columns:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN execution_generation INTEGER NOT NULL DEFAULT 0"))
        connection.execute(text("UPDATE jobs SET execution_generation = attempt_count"))

    artifact_columns = {column["name"] for column in inspect(connection).get_columns("artifacts")}
    additions = {
        "output_role": "VARCHAR(80) NOT NULL DEFAULT 'primary'",
        "output_index": "INTEGER NOT NULL DEFAULT 0",
        "delivery_status": "VARCHAR(40) NOT NULL DEFAULT 'pending'",
        "legacy_duplicate_of": "VARCHAR(36)",
    }
    legacy = "output_role" not in artifact_columns
    for name, definition in additions.items():
        if name not in artifact_columns:
            connection.execute(text(f"ALTER TABLE artifacts ADD COLUMN {name} {definition}"))

    if legacy:
        # Preserve every ID and URI, including conflicting historical outputs.
        # Exact duplicate references are quarantined, not silently deleted.
        rows = connection.execute(text(
            "SELECT id, job_id, kind, uri FROM artifacts ORDER BY created_at, id"
        )).mappings()
        next_index: dict[tuple[str, str], int] = {}
        first_reference: dict[tuple[str, str, str], str] = {}
        for row in rows:
            if not row["job_id"]:
                continue
            group = (row["job_id"], row["kind"])
            identity = (*group, row["uri"])
            index = next_index.get(group, 0)
            duplicate_of = first_reference.get(identity)
            connection.execute(text(
                "UPDATE artifacts SET output_role=:role, output_index=:index, "
                "delivery_status=:status, legacy_duplicate_of=:duplicate WHERE id=:id"
            ), {
                "id": row["id"], "role": row["kind"], "index": index,
                "status": "archived_duplicate" if duplicate_of else "pending",
                "duplicate": duplicate_of,
            })
            next_index[group] = index + 1
            first_reference.setdefault(identity, row["id"])

    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_job_output "
        "ON artifacts (job_id, output_role, output_index)"
    ))
    for model in (models.ProviderOperation, models.ProviderEvent, models.UsageEvidence, models.UsageAdjustment, models.OutboxEvent):
        model.__table__.create(bind=connection, checkfirst=True)


def _create_storage_upload_intents(connection: Connection) -> None:
    from .models import StorageUploadIntent

    StorageUploadIntent.__table__.create(bind=connection, checkfirst=True)


def _create_artifact_storage(connection: Connection) -> None:
    from .models import ArtifactStorage

    ArtifactStorage.__table__.create(bind=connection, checkfirst=True)


def _create_artifact_archive_tasks(connection: Connection) -> None:
    from .models import ArtifactArchiveTask

    ArtifactArchiveTask.__table__.create(bind=connection, checkfirst=True)


def _widen_usage_ledger_status(connection: Connection) -> None:
    # pending_reconciliation is 22 characters. SQLite accepts this in a
    # VARCHAR(20), while PostgreSQL correctly rejects it. Preserve all rows
    # and widen existing PostgreSQL installations before deploying writers.
    if connection.dialect.name == "postgresql":
        connection.execute(text(
            "ALTER TABLE usage_ledger ALTER COLUMN status TYPE VARCHAR(40)"
        ))


def _create_personal_identity_tables(connection: Connection) -> None:
    from . import models

    for table in (models.User, models.Membership, models.ExternalIdentity, models.UserSession,
                  models.RefreshToken, models.AuthThrottle, models.AuthEvent):
        table.__table__.create(bind=connection, checkfirst=True)


def _create_asset_labels(connection: Connection) -> None:
    from .models import AssetLabel
    AssetLabel.__table__.create(bind=connection, checkfirst=True)


def _create_edit_revisions(connection: Connection) -> None:
    from .models import EditDocument, EditHead, PlanRevision, TimelineRevision
    for model in (EditDocument, PlanRevision, TimelineRevision, EditHead):
        model.__table__.create(bind=connection, checkfirst=True)


def _create_artifact_reviews(connection: Connection) -> None:
    from .models import ArtifactReview

    ArtifactReview.__table__.create(bind=connection, checkfirst=True)


def _create_artifact_shares(connection: Connection) -> None:
    from .models import ArtifactShare

    ArtifactShare.__table__.create(bind=connection, checkfirst=True)


def _create_billing_workbench(connection: Connection) -> None:
    from .models import ProjectBudget, RenderQuote, JobAction
    for table in (ProjectBudget, RenderQuote, JobAction):
        table.__table__.create(bind=connection, checkfirst=True)


def _create_preparation_quotes(connection: Connection) -> None:
    from .models import PreparationQuote
    PreparationQuote.__table__.create(bind=connection, checkfirst=True)


def _create_production_batches(connection: Connection) -> None:
    from .models import ProductionBatch, BatchItem, BatchRevision, BatchQuote
    for table in (ProductionBatch, BatchItem, BatchRevision, BatchQuote):
        table.__table__.create(bind=connection, checkfirst=True)


def _create_batch_runs(connection: Connection) -> None:
    from .models import BatchRenderQuote, BatchRun, BatchRunItem, BatchTaskAction
    for table in (BatchRenderQuote, BatchRun, BatchRunItem, BatchTaskAction):
        table.__table__.create(bind=connection, checkfirst=True)


def _create_dispatch_tables(connection: Connection) -> None:
    from .models import DispatchClock, DispatchTurn, JobDispatchStamp, ProviderDispatchGate
    for table in (DispatchClock, DispatchTurn, JobDispatchStamp, ProviderDispatchGate):
        table.__table__.create(bind=connection, checkfirst=True)
    connection.execute(text("INSERT INTO dispatch_clock (id, turn) VALUES (1, 0) ON CONFLICT (id) DO NOTHING"))


def _create_batch_delivery(connection: Connection) -> None:
    from .models import BatchReviewAction, DeliveryPackage, DeliveryPackageRequest
    for table in (BatchReviewAction, DeliveryPackage, DeliveryPackageRequest):
        table.__table__.create(bind=connection, checkfirst=True)


def _create_runtime_observability(connection: Connection) -> None:
    from .models import JobTrace, RuntimeCounter, WorkerPulse, OperationalAlert
    for model in (JobTrace, RuntimeCounter, WorkerPulse, OperationalAlert):
        model.__table__.create(connection, checkfirst=True)


def _create_database_recovery_state(connection: Connection) -> None:
    from .models import DatabaseRecoveryState,new_id
    DatabaseRecoveryState.__table__.create(connection,checkfirst=True)
    connection.execute(text("INSERT INTO database_recovery_state (id,instance_id,state,created_at,updated_at) VALUES (1,:instance,'active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING"),{'instance':new_id()})


def _create_legacy_tenant_adoptions(connection: Connection) -> None:
    from .models import LegacyTenantAdoption
    LegacyTenantAdoption.__table__.create(bind=connection,checkfirst=True)


def _create_storyboard_authoring(connection: Connection) -> None:
    from .models import ReferenceUsage, ReferenceAnalysis, StoryboardDocument, StoryboardRevision
    for model in (ReferenceUsage, ReferenceAnalysis, StoryboardDocument, StoryboardRevision):
        model.__table__.create(bind=connection, checkfirst=True)


def _create_shot_matching(connection: Connection) -> None:
    from .models import AssetSemanticRevision, ShotMatchReport, StoryboardSourceRevision, StoryboardSourceHead
    for model in (AssetSemanticRevision, ShotMatchReport, StoryboardSourceRevision, StoryboardSourceHead):
        model.__table__.create(bind=connection, checkfirst=True)


MIGRATIONS = (
    Migration(
        version="0001_jobs_runtime_and_idempotency",
        description="Add durable job idempotency and retry execution columns",
        apply=_add_job_runtime_columns,
    ),
    Migration(
        version="0002_budget_tables",
        description="Create tenant budgets and provider usage ledger",
        apply=_create_budget_tables,
    ),
    Migration(
        version="0003_completion_contract",
        description="Fence executions and preserve result, event and billing evidence",
        apply=_add_completion_contract,
    ),
    Migration(
        version="0004_storage_upload_intents",
        description="Persist idempotent direct-to-storage upload intents",
        apply=_create_storage_upload_intents,
    ),
    Migration(
        version="0005_artifact_storage",
        description="Bind archived artifact content to managed objects and retention",
        apply=_create_artifact_storage,
    ),
    Migration(
        version="0006_artifact_archive_tasks",
        description="Persist bounded archive retries and crash recovery leases",
        apply=_create_artifact_archive_tasks,
    ),
    Migration(
        version="0007_usage_ledger_status",
        description="Allow pending reconciliation status without discarding billing evidence",
        apply=_widen_usage_ledger_status,
    ),
    Migration(
        version="0008_personal_identity",
        description="Create personal users, memberships and revocable rotating sessions",
        apply=_create_personal_identity_tables,
    ),
    Migration(
        version="0009_asset_labels",
        description="Index user labels without changing source media or analysis evidence",
        apply=_create_asset_labels,
    ),
    Migration(
        version="0010_edit_revisions",
        description="Freeze plan and timeline revisions with an atomic edit head and request receipts",
        apply=_create_edit_revisions,
    ),
    Migration(
        version="0011_artifact_reviews",
        description="Append exact-content human decisions with CAS and durable review receipts",
        apply=_create_artifact_reviews,
    ),
    Migration(
        version="0012_artifact_shares",
        description="Bind revocable scoped share capabilities to an exact human approval",
        apply=_create_artifact_shares,
    ),
    Migration(version="0013_billing_workbench", description="Project ceilings, bound render quotes and durable task action receipts", apply=_create_billing_workbench),
    Migration(version="0014_preparation_quotes", description="Bind analysis and planning cost envelopes to immutable inputs and receipts", apply=_create_preparation_quotes),
    Migration(version="0015_production_batches", description="Version batch configuration, atomically confirm shared analysis and independent child plans", apply=_create_production_batches),
    Migration(version="0016_batch_runs", description="Bind selected batch render waves, original-job actions and fixed version receipts", apply=_create_batch_runs),
    Migration(version="0017_fair_dispatch", description="Persist fair queue turns and serialize bounded admission and provider dispatch", apply=_create_dispatch_tables),
    Migration(version="0018_batch_delivery", description="Persist selected-version bulk review receipts and immutable delivery package jobs", apply=_create_batch_delivery),
    Migration(version="0019_runtime_observability", description="Persist request origins, duplicate counts, worker pulses and alert transitions", apply=_create_runtime_observability),
    Migration(version="0020_database_recovery_state", description="Keep restored databases quarantined and fence foreign database execution tickets", apply=_create_database_recovery_state),
    Migration(version="0021_legacy_tenant_adoptions", description="Audit explicit personal-owner adoption of a quarantined unowned legacy Tenant", apply=_create_legacy_tenant_adoptions),
    Migration(version="0022_storyboard_authoring", description="Freeze reference ranges and evidence with versioned personal storyboards and locks", apply=_create_storyboard_authoring),
    Migration(version="0023_shot_matching", description="Version user semantic evidence, shared-requirement match reports and storyboard source decisions", apply=_create_shot_matching),
)


class DatabaseSchemaError(RuntimeError):
    """Fixed, non-sensitive startup errors; no implicit version downgrade."""


def require_current_schema(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        if not inspect(connection).has_table('schema_migrations'):
            raise DatabaseSchemaError('database_migration_required')
        applied=_applied_versions(connection)
    expected={'0000_baseline',*(migration.version for migration in MIGRATIONS)}
    if applied-expected:raise DatabaseSchemaError('database_version_newer_or_unknown')
    if applied!=expected:raise DatabaseSchemaError('database_migration_required')
    return applied


def migrate(engine: Engine) -> set[str]:
    """Apply all known migrations once and return applied versions.

    A database with no migration history is bootstrapped from the current ORM
    metadata.  This preserves existing installations that predate this runner:
    newly introduced tables are created, while columns on existing tables are
    handled by numbered migrations.  Once the baseline marker exists,
    ``create_all`` is never called implicitly, so schema drift cannot be
    silently hidden in production.
    """

    # Importing models registers all tables on Base before bootstrapping.
    from . import models  # noqa: F401
    from .database import Base

    with engine.begin() as connection:
        _ensure_migration_table(connection)
        applied = _applied_versions(connection)
        if applied-{'0000_baseline',*(migration.version for migration in MIGRATIONS)}:
            raise DatabaseSchemaError('database_version_newer_or_unknown')
        if not applied:
            Base.metadata.create_all(bind=connection)
            _record_migration(connection, ("0000_baseline", "Bootstrap current ORM schema"))
            applied = _applied_versions(connection)

        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            migration.apply(connection)
            _record_migration(connection, migration)
            applied.add(migration.version)

        return _applied_versions(connection)
