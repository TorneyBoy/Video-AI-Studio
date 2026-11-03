from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base, get_db
from app.main import app
from app.services.providers import ModelSnapshot, ProviderCapability, ProviderResult, ProviderRouter
from app.services.provider_callbacks import ProviderCallbackError, apply_callback
from app.services.worker import process_queued_provider_jobs


def _client(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / (uuid4().hex + '.db')}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return engine, factory


class CostProvider:
    name = "cost-provider"
    capabilities = (ProviderCapability("video_generation", ("json",), ("json",), commercial_allowed=True),)

    def __init__(self, amount: float):
        self.amount = amount
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return ProviderResult(
            artifact_uri="https://cdn.example/result.json",
            artifact_type="video.generation_manifest",
            model=ModelSnapshot(provider=self.name, model_id="model", version="1"),
            usage={"seconds": 2},
            cost={"amount": self.amount, "currency": "USD"},
        )


def test_submission_estimate_reserves_and_exceeding_job_is_policy_blocked(tmp_path: Path):
    engine, _factory = _client(tmp_path)
    try:
        with TestClient(app) as client:
            tenant = client.post("/api/v1/tenants", json={"slug": f"budget-{uuid4().hex[:8]}", "name": "Budget"}).json()
            headers = {"X-Tenant-ID": tenant["id"]}
            configured = client.put(
                f"/api/v1/tenants/{tenant['id']}/budget",
                headers=headers,
                json={"currency": "USD", "monthly_limit_micros": 1000000, "task_limit_micros": 700000},
            )
            assert configured.status_code == 200
            allowed = client.post(
                "/api/v1/jobs",
                headers=headers,
                json={"type": "video.generate", "payload": {"estimated_cost": {"amount": 0.5, "currency": "USD"}}},
            )
            assert allowed.status_code == 201
            assert allowed.json()["status"] == "queued"
            blocked = client.post(
                "/api/v1/jobs",
                headers=headers,
                json={"type": "video.generate", "payload": {"estimated_cost": {"amount": 0.8, "currency": "USD"}}},
            )
            assert blocked.status_code == 201
            body = blocked.json()
            assert body["status"] == "failed"
            assert body["policy_blocked"] is True
            assert body["failure_code"] == "budget_exceeded"
            ledger = client.get("/api/v1/billing/usage", headers=headers).json()
            assert len(ledger) == 2
            assert {item["status"] for item in ledger} == {"reserved", "blocked"}
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_invalid_submission_estimate_returns_validation_error(tmp_path: Path):
    engine, _factory = _client(tmp_path)
    try:
        with TestClient(app) as client:
            tenant = client.post("/api/v1/tenants", json={"slug": f"budget-invalid-{uuid4().hex[:8]}", "name": "Budget"}).json()
            response = client.post(
                "/api/v1/jobs",
                headers={"X-Tenant-ID": tenant["id"]},
                json={"type": "video.generate", "payload": {"estimated_cost": {"amount": "not-a-number"}}},
            )
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "budget_cost_invalid"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_provider_cost_is_settled_once_and_over_budget_blocks_delivery(tmp_path: Path):
    engine, factory = _client(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"worker-budget-{uuid4().hex[:8]}", name="Budget")
            db.add(tenant)
            db.flush()
            models_budget = models.TenantBudget(
                tenant_id=tenant.id,
                currency="USD",
                monthly_limit_micros=1000000,
                task_limit_micros=500000,
            )
            job = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                payload={"provider_request": {"capability": "video_generation", "prompt": "test"}},
            )
            db.add_all([models_budget, job])
            db.commit()
            provider = CostProvider(0.8)
            results = process_queued_provider_jobs(db, router=ProviderRouter([provider]))
            db.refresh(job)
            assert results[0]["status"] == "failed"
            assert job.status == "failed"
            assert job.policy_blocked is True
            assert job.failure_code == "budget_exceeded"
            assert db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id)) is None
            ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job.id))
            assert ledger is not None
            assert ledger.amount_micros == 800000
            assert ledger.decision == "over_budget"
            # A terminal job is not re-dispatched and therefore cannot create
            # a second charge row on another worker poll.
            assert process_queued_provider_jobs(db, router=ProviderRouter([provider])) == []
            assert provider.calls == 1
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_provider_callback_settles_cost_and_marks_over_budget(tmp_path: Path):
    engine, factory = _client(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"callback-budget-{uuid4().hex[:8]}", name="Budget")
            db.add(tenant)
            db.flush()
            db.add(models.TenantBudget(tenant_id=tenant.id, currency="USD", task_limit_micros=500000))
            job = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                payload={"provider_request": {"capability": "video_generation"}},
            )
            db.add(job)
            db.commit()
            updated, already = apply_callback(
                db,
                job=job,
                payload={
                    "event_id": "event-budget",
                    "status": "succeeded",
                    "provider": "remote-video",
                    "artifact_uri": "https://cdn.example/output.mp4",
                    "cost": {"amount": 0.8, "currency": "USD"},
                },
                provider="remote-video",
            )
            db.commit()
            assert already is False
            assert updated.status == "failed"
            assert updated.policy_blocked is True
            assert db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id)) is None
            ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job.id))
            assert ledger is not None
            assert ledger.decision == "over_budget"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_usage_ledger_endpoint_is_strictly_tenant_scoped(tmp_path: Path):
    engine, factory = _client(tmp_path)
    try:
        with factory() as db:
            first = models.Tenant(slug=f"ledger-one-{uuid4().hex[:8]}", name="One")
            second = models.Tenant(slug=f"ledger-two-{uuid4().hex[:8]}", name="Two")
            db.add_all([first, second])
            db.flush()
            first_job = models.Job(tenant_id=first.id, type="video.generate", payload={})
            second_job = models.Job(tenant_id=second.id, type="video.generate", payload={})
            db.add_all([first_job, second_job])
            db.flush()
            db.add_all(
                [
                    models.UsageLedger(
                        tenant_id=first.id,
                        job_id=first_job.id,
                        period_key="2026-10",
                        status="settled",
                        decision="allowed",
                        source="provider_result",
                        amount_micros=120000,
                        currency="USD",
                        usage_json={"seconds": 3},
                        cost_json={"amount": 0.12, "currency": "USD"},
                        metadata_json={},
                    ),
                    models.UsageLedger(
                        tenant_id=second.id,
                        job_id=second_job.id,
                        period_key="2026-10",
                        status="settled",
                        decision="allowed",
                        source="provider_result",
                        amount_micros=990000,
                        currency="USD",
                        usage_json={"seconds": 30},
                        cost_json={"amount": 0.99, "currency": "USD"},
                        metadata_json={},
                    ),
                ]
            )
            db.commit()

        with TestClient(app) as client:
            first_rows = client.get("/api/v1/billing/usage", headers={"X-Tenant-ID": first.id}).json()
            second_rows = client.get("/api/v1/billing/usage", headers={"X-Tenant-ID": second.id}).json()
            assert [row["tenant_id"] for row in first_rows] == [first.id]
            assert [row["tenant_id"] for row in second_rows] == [second.id]
            assert first_rows[0]["job_id"] != second_rows[0]["job_id"]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_duplicate_callback_does_not_reconcile_budget_twice(tmp_path: Path):
    engine, factory = _client(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"callback-replay-{uuid4().hex[:8]}", name="Budget")
            db.add(tenant)
            db.flush()
            db.add(models.TenantBudget(tenant_id=tenant.id, currency="USD", task_limit_micros=1000000))
            job = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                payload={"provider_request": {"capability": "video_generation", "provider": "remote-video"}},
            )
            db.add(job)
            db.commit()

            payload = {
                "event_id": "event-replay-budget",
                "status": "succeeded",
                "provider": "remote-video",
                "artifact_uri": "https://cdn.example/replay.mp4",
                "cost": {"amount": 0.42, "currency": "USD"},
                "usage": {"seconds": 4},
            }
            updated, already = apply_callback(db, job=job, payload=payload, provider="remote-video")
            db.commit()
            assert updated.status == "succeeded"
            assert already is False

            with pytest.raises(ProviderCallbackError, match="reused"):
                apply_callback(db, job=job, payload={**payload, "cost": {"amount": 99}}, provider="remote-video")
            db.rollback()
            replay, already = apply_callback(db, job=job, payload=payload, provider="remote-video")
            assert replay.status == "succeeded"
            assert already is True
            ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job.id))
            assert ledger is not None
            assert ledger.amount_micros == 420000
            assert db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id)) is not None
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_callback_currency_mismatch_is_policy_blocked(tmp_path: Path):
    engine, factory = _client(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"callback-currency-{uuid4().hex[:8]}", name="Budget")
            db.add(tenant)
            db.flush()
            db.add(models.TenantBudget(tenant_id=tenant.id, currency="USD", task_limit_micros=1000000))
            job = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                payload={"provider_request": {"capability": "video_generation", "provider": "remote-video"}},
            )
            db.add(job)
            db.commit()
            updated, already = apply_callback(
                db,
                job=job,
                payload={
                    "event_id": "event-currency-mismatch",
                    "status": "succeeded",
                    "provider": "remote-video",
                    "artifact_uri": "https://cdn.example/currency.mp4",
                    "cost": {"amount": 0.42, "currency": "EUR"},
                },
                provider="remote-video",
            )
            db.commit()
            assert already is False
            assert updated.status == "failed"
            assert updated.policy_blocked is True
            ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job.id))
            assert ledger is not None
            assert ledger.decision == "over_budget"
            assert "currency_mismatch" in ledger.metadata_json["reasons"]
            assert db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id)) is None
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_callback_rejects_mismatched_persisted_provider_task(tmp_path: Path):
    engine, factory = _client(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"callback-task-{uuid4().hex[:8]}", name="Budget")
            db.add(tenant)
            db.flush()
            job = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                payload={
                    "provider_request": {"capability": "video_generation", "provider": "remote-video"},
                    "provider_job_id": "vendor-task-expected",
                },
            )
            db.add(job)
            db.commit()
            with pytest.raises(ProviderCallbackError, match="external_job_id"):
                apply_callback(
                    db,
                    job=job,
                    payload={
                        "event_id": "event-wrong-provider-task",
                        "status": "succeeded",
                        "provider": "remote-video",
                        "external_job_id": "vendor-task-other",
                        "artifact_uri": "https://cdn.example/wrong-task.mp4",
                    },
                    provider="remote-video",
                )
            assert db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id)) is None
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
