"""Result/charge races, execution fencing and atomic completion evidence."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base
from app.services.budgets import (
    budget_snapshot, preflight_job_budget, reconcile_usage_evidence, settle_provider_cost,
)
from app.services.completion import (
    accept_worker_result, execution_ticket, finish_job, lock_job, store_artifact,
)
from app.services.jobs import claim_job
from app.services.model_registry import ModelRegistry, ModelVersion
from app.services.provider_callbacks import ProviderCallbackError, apply_callback
from app.services.provider_adapters import HTTPProvider
from app.services.providers import ModelSnapshot, ProviderCapability, ProviderExecutionError, ProviderResult, ProviderRouter
from app.services.worker import process_queued_provider_jobs
from app.services.workflows import claim_workflow, create_workflow, recover_expired_workflows


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'completion.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as db:
        tenant = models.Tenant(slug=f"completion-{uuid4().hex}", name="Completion")
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


def result(amount=0.4):
    return ProviderResult(artifact_uri="https://cdn.example/result.mp4", artifact_type="video.rendered",
                          model=ModelSnapshot(provider="race-provider", model_id="test", version="1"),
                          cost={"amount": amount, "currency": "USD"})


def callback(job_id, event_id="done", amount=0.4):
    return {"event_id": event_id, "request_id": job_id, "status": "succeeded",
            "provider": "race-provider", "artifact_uri": "https://cdn.example/result.mp4",
            "artifact_type": "video.rendered", "cost": {"amount": amount, "currency": "USD"}}


def claim(db, job):
    assert claim_job(db, job)
    run = db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job.id))
    if run is None:
        run = create_workflow(db, job)
    assert claim_workflow(db, run, worker_id="worker", lease_seconds=60)
    db.commit()
    return run, execution_ticket(job, run)


def test_completion_rollback_is_atomic(database):
    factory, job_id = database
    with factory() as db:
        job = db.get(models.Job, job_id)
        claim(db, job)
        lock_job(db, job)
        settle_provider_cost(db, job=job, cost={"amount": 0.4}, usage={}, provider="race-provider")
        store_artifact(db, job, kind="video.rendered", uri="https://cdn.example/result.mp4")
        finish_job(db, job, "succeeded")
        db.rollback()
    with factory() as db:
        assert db.get(models.Job, job_id).status == "running"
        for model in (models.Artifact, models.UsageLedger, models.UsageEvidence, models.OutboxEvent):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_old_execution_cannot_publish_after_recovery(database):
    factory, job_id = database
    with factory() as db:
        job = db.get(models.Job, job_id)
        run, old_ticket = claim(db, job)
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        recover_expired_workflows(db)
        db.commit()
        _, new_ticket = claim(db, job)
        assert new_ticket.generation > old_ticket.generation
        assert accept_worker_result(db, job, old_ticket, result()) is False
        db.commit()
        assert job.status == "running"
        assert accept_worker_result(db, job, new_ticket) is True
        assert db.scalar(select(func.count()).select_from(models.Artifact)) == 0


def test_settled_charge_is_immutable_and_adjustment_is_explicit(database):
    factory, job_id = database
    with factory() as db:
        job = db.get(models.Job, job_id)
        settle_provider_cost(db, job=job, cost={"amount": 0.4}, usage={}, provider="race-provider")
        db.commit()
        settle_provider_cost(db, job=job, cost={"amount": 0.7}, usage={}, provider="race-provider")
        db.commit()
        ledger = db.scalar(select(models.UsageLedger))
        assert ledger.amount_micros == 400000
        evidence = db.scalar(select(models.UsageEvidence).where(models.UsageEvidence.disposition == "adjustment_pending"))
        adjustment = reconcile_usage_evidence(db, job=job, evidence=evidence, approved_by="operator", reason="vendor correction")
        db.commit()
        assert adjustment.delta_micros == 300000
        assert reconcile_usage_evidence(db, job=job, evidence=evidence, approved_by="operator", reason="repeat").id == adjustment.id
        assert budget_snapshot(db, None, job.tenant_id)["settled_micros"] == 700000
        assert ledger.amount_micros == 400000


def test_unknown_cost_is_not_a_free_settlement(database):
    factory, job_id = database
    with factory() as db:
        job = db.get(models.Job, job_id)
        job.payload = {**job.payload, "estimated_cost": {"amount": 0.5}}
        preflight_job_budget(db, job)
        db.commit()
        decision = settle_provider_cost(db, job=job, cost={}, usage={}, provider="race-provider")
        db.commit()
        ledger = db.scalar(select(models.UsageLedger))
        assert decision.audit["amount_micros"] is None
        assert ledger.status == "pending_reconciliation"
        assert ledger.amount_micros == 500000
        assert db.scalar(select(models.UsageEvidence)).amount_micros is None


@pytest.mark.parametrize("cost", [{"amount": 0.7, "currency": "USD"},
                                  {"status": "unknown", "currency": "CNY", "bill_confirmed": False}, None])
def test_http_failure_accounts_observed_charge_and_holds_unknown_reservation(database, cost):
    import json

    factory, job_id = database
    calls = []

    def transport(http_request, _timeout):
        calls.append(http_request.method)
        if http_request.method == "POST":
            return 202, b'{"provider_job_id":"gateway-operation","status":"queued"}'
        body = {"provider_job_id": "gateway-operation", "status": "failed", "retryable": False,
                "usage": {"output_seconds": 5}, "metrics": {"cloud_job_id": "paid-cloud-task"},
                "error": {"code": "wan_result_verification_failed"}}
        if cost is not None:
            body["cost"] = cost
        return 200, json.dumps(body).encode()

    provider = HTTPProvider(name="race-provider", base_url="https://gateway.example", capabilities=(
        ProviderCapability("video_generation", ("json",), ("video",), commercial_allowed=True),),
        model=ModelSnapshot(provider="race-provider", model_id="wan2.2-t2v-plus", version="wan2.2"),
        poll_path="/v1/wan/jobs/{provider_job_id}", transport=transport)
    with factory() as db:
        job = db.get(models.Job, job_id)
        job.payload = {**job.payload, "estimated_cost": {"amount": 0.5, "currency": "USD"}}
        assert preflight_job_budget(db, job).allowed
        db.commit()
        assert process_queued_provider_jobs(db, router=ProviderRouter([provider]))[0]["status"] == "failed"
    with factory() as db:
        job = db.get(models.Job, job_id)
        assert job.failure_code == "wan_result_verification_failed" and not job.retryable
        assert db.scalar(select(models.ProviderOperation)).external_job_id == "gateway-operation"
        ledger = db.scalar(select(models.UsageLedger))
        evidence = db.scalar(select(models.UsageEvidence))
        assert ledger.usage_json == evidence.usage_json == {"output_seconds": 5}
        assert ledger.cost_json == evidence.cost_json == (cost or {})
        if cost and "amount" in cost:
            assert ledger.status == "settled" and ledger.amount_micros == evidence.amount_micros == 700000
        else:
            assert ledger.status == "pending_reconciliation" and ledger.amount_micros == 500000
            assert evidence.amount_micros is None
            assert budget_snapshot(db, None, job.tenant_id)["reserved_micros"] == 500000
        assert db.scalar(select(func.count()).select_from(models.Artifact)) == 0
        assert process_queued_provider_jobs(db, router=ProviderRouter([provider])) == []
        assert calls == ["POST", "GET"]


def test_event_identity_survives_later_events_and_rejects_changed_content(database):
    factory, job_id = database
    with factory() as db:
        job = db.get(models.Job, job_id)
        progress = {"event_id": "progress", "status": "running", "provider": "race-provider"}
        apply_callback(db, job=job, payload=progress)
        db.commit()
        apply_callback(db, job=job, payload=callback(job_id))
        db.commit()
        assert apply_callback(db, job=job, payload=progress)[1] is True
        with pytest.raises(ProviderCallbackError, match="reused"):
            apply_callback(db, job=job, payload={**progress, "status": "cancelled"})
        db.rollback()
        assert job.status == "succeeded"
        assert db.scalar(select(func.count()).select_from(models.Artifact)) == 1


def test_late_callback_accounts_cost_without_reversing_cancellation(database):
    factory, job_id = database
    with factory() as db:
        job = db.get(models.Job, job_id)
        lock_job(db, job)
        finish_job(db, job, "cancelled")
        db.commit()
        apply_callback(db, job=job, payload=callback(job_id))
        db.commit()
        assert job.status == "cancelled"
        assert db.scalar(select(models.UsageLedger)).amount_micros == 400000
        assert db.scalar(select(models.ProviderEvent)).disposition == "late_evidence"
        assert db.scalar(select(func.count()).select_from(models.Artifact)) == 0
        assert db.scalar(select(func.count()).select_from(models.OutboxEvent)) == 1


@pytest.mark.parametrize("winner", ["callback", "cancel"])
def test_worker_does_not_overwrite_a_concurrent_result(database, winner):
    factory, job_id = database

    class RaceProvider:
        name = "race-provider"
        capabilities = (ProviderCapability("video_generation", ("json",), ("video",), commercial_allowed=True),)

        def execute(self, request):
            # A second connection commits while the worker is outside its DB
            # transaction. This also proves dispatch holds no result row lock.
            with factory() as second:
                job = second.get(models.Job, job_id)
                if winner == "callback":
                    apply_callback(second, job=job, payload=callback(job_id))
                else:
                    lock_job(second, job)
                    finish_job(second, job, "cancelled")
                second.commit()
            return result(0.7)

    with factory() as db:
        assert process_queued_provider_jobs(db, router=ProviderRouter([RaceProvider()]))[0]["status"] == "superseded"
    with factory() as db:
        assert db.get(models.Job, job_id).status == ("succeeded" if winner == "callback" else "cancelled")
        assert db.scalar(select(func.count()).select_from(models.Artifact)) == (1 if winner == "callback" else 0)
        assert db.scalar(select(models.UsageLedger)).amount_micros == (400000 if winner == "callback" else 700000)
        assert db.scalar(select(func.count()).select_from(models.OutboxEvent)) == 1


def test_ambiguous_submission_is_not_automatically_repeated(database):
    factory, job_id = database

    class UnknownProvider:
        name = "race-provider"
        capabilities = (ProviderCapability("video_generation", ("json",), ("video",), commercial_allowed=True),)
        calls = 0

        def execute(self, request):
            self.calls += 1
            raise ProviderExecutionError("connection lost after submission", code="provider_transport_error", retryable=True)

    provider = UnknownProvider()
    with factory() as db:
        response = process_queued_provider_jobs(db, router=ProviderRouter([provider]))
        assert response[0]["status"] == "failed"
        assert db.get(models.Job, job_id).retryable is False
        assert db.scalar(select(models.ProviderOperation)).state == "unknown"
        assert process_queued_provider_jobs(db, router=ProviderRouter([provider])) == []
        assert provider.calls == 1


def test_invalid_provider_charge_is_retained_without_delivering(database):
    factory, job_id = database

    class InvalidCostProvider:
        name = "race-provider"
        capabilities = (ProviderCapability("video_generation", ("json",), ("video",), commercial_allowed=True),)

        def execute(self, request):
            value = result()
            return ProviderResult(artifact_uri=value.artifact_uri, artifact_type=value.artifact_type,
                                  model=value.model, cost={"amount": "not-money"})

    with factory() as db:
        response = process_queued_provider_jobs(db, router=ProviderRouter([InvalidCostProvider()]))
        assert response[0]["status"] == "failed"
        assert db.get(models.Job, job_id).failure_code == "budget_cost_invalid"
        assert db.scalar(select(models.UsageEvidence)).disposition == "invalid_amount"
        assert db.scalar(select(func.count()).select_from(models.Artifact)) == 0


def test_model_rejection_keeps_cloud_identity_and_charge_without_delivery(database):
    factory, job_id = database
    expected = ModelVersion(provider="race-provider", model_id="test", version="1",
        capabilities=("video_generation",), weights_license="provider-managed", commercial_allowed=True,
        status="approved", integrity_mode="provider_version", integrity_reference="fixed-test-release")

    class WrongModelProvider:
        name = "race-provider"
        model = expected.snapshot()
        capabilities = (ProviderCapability("video_generation", ("json",), ("video",), commercial_allowed=True),)

        def execute(self, request):
            return ProviderResult(artifact_uri="https://cdn.example/rejected.mp4", artifact_type="video.rendered",
                model=ModelSnapshot(provider="unexpected-provider", model_id="test", version="1"),
                provenance={"provider_job_id": "known-paid-job"}, cost={"amount": 0.7, "currency": "USD"})

    with factory() as db:
        response = process_queued_provider_jobs(db, router=ProviderRouter([WrongModelProvider()],
                                               model_registry=ModelRegistry([expected])))
        assert response[0]["status"] == "failed"
        job = db.get(models.Job, job_id)
        assert job.failure_code == "model_snapshot_mismatch" and not job.retryable
        assert db.scalar(select(models.ProviderOperation)).external_job_id == "known-paid-job"
        assert db.scalar(select(models.UsageLedger)).amount_micros == 700000
        assert db.scalar(select(models.UsageEvidence)).amount_micros == 700000
        assert db.scalar(select(func.count()).select_from(models.Artifact)) == 0
