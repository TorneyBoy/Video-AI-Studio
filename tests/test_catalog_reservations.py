"""Delayed cloud bills retain trusted reservations without granting free work."""

from copy import deepcopy
import json

import pytest
from sqlalchemy import func, select

from app import models
from app.api import routers
from app.services.budgets import (
    BudgetDataError, budget_snapshot, capability_reservation, preflight_job_budget, settle_provider_cost,
)
from app.services.providers import ModelSnapshot, ProviderCapability, ProviderResult, ProviderRouter
from app.services.worker import process_queued_provider_jobs
from test_budgets import _client
from test_capability_catalog import config


def snapshot():
    return {"id": "analysis:media_analysis:1", "available": True, "provider": "analysis",
            "model_id": "hosted-analysis", "model_version": "1", "capability": "media_analysis",
            "price": {"version": "envelope-1", "per_request": 0.5, "currency": "CNY", "billing_unit": "request"},
            "limits": {"max_duration_seconds": 30}}


def prepare(factory, *, trusted=True):
    with factory() as db:
        tenant = models.Tenant(slug="catalog-budget", name="Catalog budget")
        db.add(tenant)
        db.flush()
        db.add(models.TenantBudget(tenant_id=tenant.id, currency="CNY", monthly_limit_micros=1_000_000,
                                  task_limit_micros=600_000, enabled=True))
        job = models.Job(tenant_id=tenant.id, type="asset.analyze", payload={
            "capability_snapshot": snapshot(), "estimated_cost": {"amount": 0.5, "currency": "CNY"}})
        db.add(job)
        db.flush()
        preflight_job_budget(db, job, selected_capability=snapshot() if trusted else None)
        db.commit()
        return job.id


@pytest.mark.parametrize("change,allowed", [
    ({}, True), ({"untrusted": True}, False), ({"task_limit": 100_000}, False),
    ({"provider": "other"}, False), ({"model_id": "other"}, False),
    ({"currency": "USD"}, False), ({"snapshot_version": "2"}, False),
])
def test_unknown_cost_requires_matching_trusted_reservation_and_current_budget(tmp_path, change, allowed):
    engine, factory = _client(tmp_path)
    try:
        job_id = prepare(factory, trusted=not change.get("untrusted"))
        with factory() as db:
            job = db.get(models.Job, job_id)
            if "task_limit" in change:
                db.scalar(select(models.TenantBudget)).task_limit_micros = change["task_limit"]
            if "snapshot_version" in change:
                selected = deepcopy(job.payload["capability_snapshot"])
                selected["model_version"] = change["snapshot_version"]
                job.payload = {**job.payload, "capability_snapshot": selected}
            decision = settle_provider_cost(db, job=job, cost={"status": "unknown", "currency": change.get("currency", "CNY")},
                                            usage={"output_seconds": 12}, provider=change.get("provider", "analysis"),
                                            model_id=change.get("model_id", "hosted-analysis"), model_version="1")
            db.commit()
            assert decision.allowed is allowed
            ledger = db.scalar(select(models.UsageLedger))
            assert ledger.status == "pending_reconciliation"
            assert ledger.amount_micros == 500_000
            assert decision.audit["amount_micros"] is None
            assert db.scalar(select(models.UsageEvidence)).amount_micros is None
            assert budget_snapshot(db, db.scalar(select(models.TenantBudget)), job.tenant_id)["reserved_micros"] == 500_000
    finally:
        from app.database import get_db
        from app.main import app
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_repeated_reservation_then_known_bill_are_not_double_charged(tmp_path):
    engine, factory = _client(tmp_path)
    try:
        job_id = prepare(factory)
        with factory() as db:
            job = db.get(models.Job, job_id)
            preflight_job_budget(db, job, selected_capability=snapshot())
            settle_provider_cost(db, job=job, cost={"currency": "CNY"}, usage={}, provider="analysis", model_id="hosted-analysis", model_version="1")
            observed = {"amount": 0.03, "currency": "CNY", "bill_confirmed": True}
            settle_provider_cost(db, job=job, cost=observed, usage={}, provider="analysis", model_id="hosted-analysis", model_version="1")
            db.commit()
            ledger = db.scalar(select(models.UsageLedger))
            assert ledger.status == "settled" and ledger.amount_micros == 30_000
            assert db.scalar(select(func.count()).select_from(models.UsageLedger)) == 1
            assert db.scalar(select(func.count()).select_from(models.UsageEvidence)) == 2
            assert budget_snapshot(db, db.scalar(select(models.TenantBudget)), job.tenant_id)["reserved_micros"] == 0
    finally:
        from app.database import get_db
        from app.main import app
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_minute_reservation_uses_enforced_maximum_not_client_duration():
    selected = snapshot()
    selected["price"].update({"billing_unit": "started_minute", "per_request": 0.06})
    selected["limits"] = {"max_output_duration_seconds": 61}
    assert capability_reservation(selected)["amount"] == "0.12"
    selected["limits"] = {}
    with pytest.raises(BudgetDataError, match="duration limit"):
        capability_reservation(selected)


def test_explicit_missing_reservation_recovery_preserves_observed_cost(tmp_path):
    engine, factory = _client(tmp_path)
    try:
        job_id = prepare(factory, trusted=False)
        with factory() as db:
            job = db.get(models.Job, job_id)
            ledger = db.scalar(select(models.UsageLedger))
            # Reproduce a legacy route that never reserved an estimate.
            db.delete(ledger)
            db.flush()
            job.payload = {key: value for key, value in job.payload.items() if key != "capability_snapshot"}
            original = settle_provider_cost(db, job=job, cost={"currency": "CNY"}, usage={},
                                            provider="analysis", model_id="hosted-analysis", model_version="1")
            assert not original.allowed
            evidence_id = db.scalar(select(models.UsageEvidence.id))
            preflight_job_budget(db, job, selected_capability=snapshot(),
                                 recovery_reason="legacy submission skipped server reservation")
            recovered = settle_provider_cost(db, job=job, cost={"currency": "CNY"}, usage={},
                                             provider="analysis", model_id="hosted-analysis", model_version="1")
            db.commit()
            assert recovered.allowed and recovered.audit["amount_micros"] is None
            assert db.scalar(select(models.UsageEvidence.id)) == evidence_id
            ledger = db.scalar(select(models.UsageLedger))
            assert ledger.status == "pending_reconciliation" and ledger.amount_micros == 500_000
            assert ledger.metadata_json["reservation_recovery"]["previous_audit"]["evidence_id"] == evidence_id
    finally:
        from app.database import get_db
        from app.main import app
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_worker_can_finish_against_retained_trusted_reservation(tmp_path):
    engine, factory = _client(tmp_path)
    try:
        job_id = prepare(factory)
        with factory() as db:
            job = db.get(models.Job, job_id)
            job.type = "video.generate"
            job.payload = {**job.payload, "provider_request": {"capability": "video_generation", "provider": "analysis"}}
            db.commit()

            class DelayedBillProvider:
                name = "analysis"
                capabilities = (ProviderCapability("video_generation", ("json",), ("json",), commercial_allowed=True),)

                def execute(self, request):
                    return ProviderResult(artifact_uri="https://cdn.example/result.json", artifact_type="video.generation_manifest",
                                          model=ModelSnapshot(provider=self.name, model_id="hosted-analysis", version="1"),
                                          cost={"currency": "CNY", "status": "unknown"})

            results = process_queued_provider_jobs(db, router=ProviderRouter([DelayedBillProvider()]))
            db.refresh(job)
            assert results[0]["status"] == job.status == "succeeded"
            ledger = db.scalar(select(models.UsageLedger))
            assert ledger.status == "pending_reconciliation" and ledger.amount_micros == 500_000
            assert db.scalar(select(func.count()).select_from(models.Artifact)) == 1
    finally:
        from app.database import get_db
        from app.main import app
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.mark.parametrize("verified", [True, False])
def test_production_analysis_route_pins_provider_and_server_estimate(client, monkeypatch, verified):
    tenant = client.post("/api/v1/tenants", json={"slug": "catalog-route", "name": "Catalog route"}).json()
    headers = {"X-Tenant-ID": tenant["id"]}
    client.put(f"/api/v1/tenants/{tenant['id']}/budget", headers=headers,
               json={"currency": "CNY", "monthly_limit_micros": 1_000_000, "task_limit_micros": 600_000})
    # The fixture creates an ordinary local-mode metadata row before switching
    # only the submission gate to production. No upload or vendor is invoked.
    monkeypatch.setattr(routers.settings, "asset_storage_mode", "local")
    asset_response = client.post("/api/v1/assets", headers=headers,
                                 json={"name": "test", "kind": "video", "uri": "https://media.example/source.mp4"})
    assert asset_response.status_code == 201, asset_response.text
    asset = asset_response.json()
    value = config()
    capability = value["providers"][0]["capabilities"][0]
    capability.update({"name": "media_analysis", "cost": {"per_request": 0.5, "currency": "CNY", "billing_unit": "request"}})
    if not verified:
        capability["acceptance"] = {"status": "testing"}
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    monkeypatch.setenv("PROVIDER_REGISTRY_JSON", json.dumps(value))
    monkeypatch.setattr(routers.settings, "app_environment", "production")
    request = {"options": {"estimated_cost": {"amount": 0, "currency": "CNY"}}, "idempotency_key": "analysis-once"}
    result = client.post(f"/api/v1/assets/{asset['id']}/analyze", headers=headers, json=request)
    if not verified:
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "capability_not_available"
        assert client.get("/api/v1/jobs", headers=headers).json() == []
        return
    assert result.status_code == 201, result.text
    job = result.json()
    assert job["payload"]["capability_snapshot"]["provider"] == "verified-gateway"
    assert job["payload"]["estimated_cost"]["amount"] == "0.5"
    assert job["payload"]["budget"]["reservation"]["basis"] == "server_capability_catalog"
    repeated = client.post(f"/api/v1/assets/{asset['id']}/analyze", headers=headers, json=request)
    assert repeated.json()["id"] == job["id"]
    ledger = client.get("/api/v1/billing/usage", headers=headers).json()
    assert len(ledger) == 1 and ledger[0]["amount_micros"] == 500_000
