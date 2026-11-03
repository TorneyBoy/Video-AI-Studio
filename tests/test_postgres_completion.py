"""Real row-lock races. TEST_POSTGRES_URL must point to a disposable test DB."""

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base
from app.migrations import migrate
from app.services.completion import finish_job, lock_job
from app.services.budgets import preflight_job_budget, settle_provider_cost
from test_catalog_reservations import snapshot as _reservation_snapshot
from app.services.provider_callbacks import apply_callback
from test_completion_contract import (
    test_old_execution_cannot_publish_after_recovery as _exercise_recovery,
    test_worker_does_not_overwrite_a_concurrent_result as _exercise_worker_race,
)


@pytest.fixture
def postgres_database():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for real PostgreSQL concurrency")
    admin = create_engine(url)
    schema = "test_completion_" + uuid4().hex
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    migrate(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as db:
        tenant = models.Tenant(slug=uuid4().hex, name="Postgres race")
        db.add(tenant)
        db.flush()
        job = models.Job(tenant_id=tenant.id, type="video.generate", payload={
            "provider_request": {"capability": "video_generation", "provider": "race-provider"},
        })
        db.add(job)
        db.commit()
        job_id = job.id
    yield factory, job_id
    engine.dispose()
    with admin.begin() as db:
        db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


@pytest.mark.parametrize("race", ["duplicate", "different_cost", "cancel"])
def test_concurrent_completions_and_cancel_serialize(postgres_database, race):
    factory, job_id = postgres_database
    barrier = Barrier(2)

    def execute(index):
        with factory() as db:
            job = db.get(models.Job, job_id)
            barrier.wait(timeout=10)
            if race == "cancel" and index == 1:
                lock_job(db, job)
                if job.status not in {"succeeded", "failed", "cancelled"}:
                    finish_job(db, job, "cancelled")
            else:
                apply_callback(db, job=job, payload={
                    "event_id": "same" if race == "duplicate" else f"done-{index}",
                    "request_id": job_id, "status": "succeeded", "provider": "race-provider",
                    "artifact_uri": "https://cdn.example/result.mp4", "artifact_type": "video.rendered",
                    "cost": {"amount": 0.4 if race != "different_cost" else 0.4 + index * 0.3},
                })
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, index) for index in range(2)]
        for future in futures:
            future.result(timeout=20)
    with factory() as db:
        job = db.get(models.Job, job_id)
        assert job.status in {"succeeded", "cancelled"}
        artifacts = db.scalar(select(func.count()).select_from(models.Artifact))
        assert artifacts == (1 if job.status == "succeeded" else 0)
        assert db.scalar(select(func.count()).select_from(models.UsageLedger)) == 1
        assert db.scalar(select(func.count()).select_from(models.OutboxEvent)) == 1
        assert db.scalar(select(func.count()).select_from(models.ProviderEvent)) == (1 if race in {"duplicate", "cancel"} else 2)


def test_postgres_migration_and_repeat_are_stable(postgres_database):
    factory, _ = postgres_database
    engine = factory.kw["bind"]
    before = migrate(engine)
    assert "0003_completion_contract" in before
    assert migrate(engine) == before
    assert "usage_adjustments" in Base.metadata.tables


def test_postgres_legacy_ledger_migration_retains_unknown_cost_evidence(postgres_database):
    factory, job_id = postgres_database
    engine = factory.kw["bind"]
    with factory() as db:
        job = db.get(models.Job, job_id)
        db.add(models.TenantBudget(tenant_id=job.tenant_id, currency="CNY",
                                  monthly_limit_micros=10_000_000, enabled=True))
        db.add(models.UsageLedger(tenant_id=job.tenant_id, job_id=job.id,
                                 period_key="2026-10", status="reserved",
                                 source="preflight", currency="CNY", amount_micros=500_000))
        db.commit()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE usage_ledger ALTER COLUMN status TYPE VARCHAR(20)"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version='0007_usage_ledger_status'"))
    versions = migrate(engine)
    assert "0007_usage_ledger_status" in versions
    assert next(column for column in inspect(engine).get_columns("usage_ledger")
                if column["name"] == "status")["type"].length == 40
    with factory() as db:
        job = db.get(models.Job, job_id)
        decision = settle_provider_cost(db, job=job, cost={"status": "unknown", "currency": "CNY"},
                                        usage={"duration_seconds": 12}, provider="aliyun-hosted-analysis")
        db.commit()
        assert not decision.allowed
        ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job_id))
        assert ledger.status == "pending_reconciliation"
        assert ledger.amount_micros == 500_000
        assert ledger.decision == "amount_unknown"
        evidence = db.scalar(select(models.UsageEvidence).where(models.UsageEvidence.job_id == job_id))
        assert evidence.amount_micros is None
        assert evidence.cost_json["status"] == "unknown"
    assert migrate(engine) == versions


def test_postgres_concurrent_catalog_reservations_keep_delayed_costs_reserved(postgres_database):
    factory, job_id = postgres_database
    with factory() as db:
        tenant_id = db.get(models.Job, job_id).tenant_id
        db.add(models.TenantBudget(tenant_id=tenant_id, currency="CNY", enabled=True,
                                  monthly_limit_micros=750_000, task_limit_micros=600_000))
        db.commit()
    barrier = Barrier(2)

    def submit(_index):
        with factory() as db:
            row = models.Job(tenant_id=tenant_id, type="asset.analyze",
                             payload={"capability_snapshot": _reservation_snapshot()})
            db.add(row)
            db.flush()
            barrier.wait(timeout=10)
            preflight_job_budget(db, row, selected_capability=_reservation_snapshot())
            db.commit()
            if row.status != "failed":
                decision = settle_provider_cost(db, job=row, cost={"currency": "CNY", "status": "unknown"},
                                                usage={}, provider="analysis", model_id="hosted-analysis", model_version="1")
                assert decision.allowed
                db.commit()
            return row.id, row.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result(timeout=30) for future in [pool.submit(submit, index) for index in range(2)]]
    assert sorted(status for _, status in outcomes) == ["failed", "queued"]
    with factory() as db:
        ledgers = list(db.scalars(select(models.UsageLedger)))
        assert sorted(row.status for row in ledgers) == ["blocked", "pending_reconciliation"]
        assert sum(row.amount_micros for row in ledgers if row.status == "pending_reconciliation") == 500_000


@pytest.mark.parametrize("winner", ["callback", "cancel"])
def test_postgres_worker_return_is_fenced(postgres_database, winner):
    _exercise_worker_race(postgres_database, winner)


def test_postgres_recovered_worker_cannot_publish(postgres_database):
    _exercise_recovery(postgres_database)
