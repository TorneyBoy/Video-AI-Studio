from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migrations import migrate


def _legacy_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE tenants (
                id VARCHAR(36) PRIMARY KEY,
                slug VARCHAR(100) NOT NULL,
                name VARCHAR(200) NOT NULL,
                settings JSON NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE projects (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(36) NOT NULL,
                name VARCHAR(200) NOT NULL,
                description TEXT,
                status VARCHAR(40) NOT NULL,
                metadata_json JSON NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE jobs (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(36) NOT NULL,
                project_id VARCHAR(36),
                type VARCHAR(80) NOT NULL,
                status VARCHAR(40) NOT NULL,
                payload JSON NOT NULL,
                error TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    return engine


def test_fresh_database_records_baseline_and_all_known_migrations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)

    versions = migrate(engine)
    assert versions == {"0000_baseline", "0001_jobs_runtime_and_idempotency", "0002_budget_tables", "0003_completion_contract", "0004_storage_upload_intents", "0005_artifact_storage", "0006_artifact_archive_tasks", "0007_usage_ledger_status", "0008_personal_identity", "0009_asset_labels", "0010_edit_revisions", "0011_artifact_reviews", "0012_artifact_shares", "0013_billing_workbench", "0014_preparation_quotes", "0015_production_batches", "0016_batch_runs", "0017_fair_dispatch", "0018_batch_delivery", "0019_runtime_observability", "0020_database_recovery_state", "0021_legacy_tenant_adoptions", "0022_storyboard_authoring", "0023_shot_matching"}
    assert {"tenant_budgets", "usage_ledger", "schema_migrations", "asset_labels", "edit_documents", "plan_revisions", "timeline_revisions", "edit_heads", "artifact_reviews", "artifact_shares", "project_budgets", "render_quotes", "job_actions", "preparation_quotes", "production_batches", "batch_items", "batch_revisions", "batch_quotes", "batch_render_quotes", "batch_runs", "batch_run_items", "batch_task_actions", "dispatch_clock", "dispatch_turns", "job_dispatch_stamps", "provider_dispatch_gates", "batch_review_actions", "delivery_packages", "delivery_package_requests", "job_traces", "runtime_counters", "worker_pulses", "operational_alerts", "database_recovery_state", "legacy_tenant_adoptions", "reference_usage", "reference_analyses", "storyboard_documents", "storyboard_revisions", "asset_semantic_revisions", "shot_match_reports", "storyboard_source_revisions", "storyboard_source_heads"}.issubset(
        inspect(engine).get_table_names()
    )

    # A second invocation is a no-op and does not duplicate indexes or rows.
    assert migrate(engine) == versions
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one() == len(versions)


def test_identity_upgrade_does_not_claim_existing_tenants_for_new_users(tmp_path):
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants VALUES (:id,:slug,:name,:settings,:created,:updated)"),
                           {"id": "existing-tenant", "slug": "existing", "name": "Existing workspace",
                            "settings": "{}", "created": datetime.now(timezone.utc), "updated": datetime.now(timezone.utc)})
    versions = migrate(engine)
    assert "0008_personal_identity" in versions
    assert {"users", "memberships", "external_identities", "user_sessions", "refresh_tokens",
            "auth_throttles", "auth_events"}.issubset(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM tenants WHERE id='existing-tenant'")).scalar_one() == "Existing workspace"
        assert connection.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM memberships")).scalar_one() == 0
    assert migrate(engine) == versions
    engine.dispose()


def test_legacy_jobs_database_gets_idempotency_runtime_and_budget_tables(tmp_path):
    engine = _legacy_engine(tmp_path)

    versions = migrate(engine)
    assert "0002_budget_tables" in versions
    columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
    assert {
        "idempotency_key",
        "attempt_count",
        "max_attempts",
        "next_retry_at",
        "retryable",
        "failure_code",
        "policy_blocked",
        "started_at",
        "completed_at",
    }.issubset(columns)

    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants(id, slug, name, settings, created_at, updated_at) VALUES (:id, :slug, :name, :settings, :created, :updated)"),
            {
                "id": "tenant-1",
                "slug": "tenant-1",
                "name": "Tenant",
                "settings": "{}",
                "created": now,
                "updated": now,
            },
        )
        base_job = {
            "id": "job-1",
            "tenant": "tenant-1",
            "type": "test",
            "status": "queued",
            "payload": "{}",
            "created": now,
            "updated": now,
            "key": "same-key",
        }
        connection.execute(
            text(
                "INSERT INTO jobs(id, tenant_id, type, status, payload, created_at, updated_at, idempotency_key) "
                "VALUES (:id, :tenant, :type, :status, :payload, :created, :updated, :key)"
            ),
            base_job,
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO jobs(id, tenant_id, type, status, payload, created_at, updated_at, idempotency_key) "
                    "VALUES (:id, :tenant, :type, :status, :payload, :created, :updated, :key)"
                ),
                {**base_job, "id": "job-2"},
            )

    assert migrate(engine) == versions


def test_completion_migration_preserves_and_quarantines_legacy_duplicates(tmp_path):
    engine = _legacy_engine(tmp_path)
    migrate(engine)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version='0003_completion_contract'"))
        connection.execute(text("DROP TABLE artifacts"))
        connection.execute(text("CREATE TABLE artifacts (id VARCHAR(36) PRIMARY KEY, job_id VARCHAR(36), kind VARCHAR(80), uri TEXT, created_at DATETIME)"))
        connection.execute(text("INSERT INTO artifacts VALUES ('first', 'job', 'video', 'https://cdn.example/one.mp4', '2026-10-17')"))
        connection.execute(text("INSERT INTO artifacts VALUES ('second', 'job', 'video', 'https://cdn.example/one.mp4', '2026-10-18')"))
        connection.execute(text("INSERT INTO artifacts VALUES ('third', 'job', 'video', 'https://cdn.example/two.mp4', '2026-10-19')"))
    migrate(engine)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, output_index, delivery_status, legacy_duplicate_of FROM artifacts ORDER BY created_at")).mappings().all()
        assert len(rows) == 3
        assert [row["output_index"] for row in rows] == [0, 1, 2]
        assert rows[1]["legacy_duplicate_of"] == "first"
        assert rows[1]["delivery_status"] == "archived_duplicate"
        assert rows[2]["legacy_duplicate_of"] is None
    assert migrate(engine)
