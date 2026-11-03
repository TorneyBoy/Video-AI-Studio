"""Regression contracts for an operator retry followed by worker recovery."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base, get_db
from app.main import app
from app.services.provider_adapters import HTTPProvider
from app.services.providers import ModelSnapshot, ProviderCapability, ProviderRouter
from app.services.jobs import InvalidJobTransition, request_job_retry
from app.services.worker import process_queued_provider_jobs


def _factory(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / (uuid4().hex + '.db')}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_failed_job_api_retry_resets_steps_and_worker_recovers_with_hosted_provider(tmp_path: Path):
    """A terminal operator retry must not hit the stale failed activity state.

    The first hosted call is a non-retryable 400, which gives the operator a
    terminal failed Job. The API retry is forced only because the operator has
    reviewed the provider response. The second call succeeds and proves that
    a failed activity is reset before the same request identity is dispatched.
    """

    engine, factory = _factory(tmp_path)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        calls: list[tuple[str, str, str | None]] = []

        def transport(http_request, _timeout):
            headers = {key.lower(): value for key, value in http_request.header_items()}
            calls.append((http_request.method, http_request.full_url, headers.get("idempotency-key")))
            if len(calls) == 1:
                return 400, b'{"error":{"code":"invalid_prompt","message":"retry after correction"}}'
            return 200, b'{"status":"succeeded","artifact_uri":"https://cdn.example/recovered.mp4","artifact_type":"video"}'

        provider = HTTPProvider(
            name="hosted-video",
            base_url="https://provider.example",
            capabilities=(
                ProviderCapability(
                    "video_generation",
                    ("json",),
                    ("video",),
                    commercial_allowed=True,
                ),
            ),
            model=ModelSnapshot(provider="hosted-video", model_id="video-v1", version="2026-10"),
            transport=transport,
        )

        with TestClient(app) as client:
            tenant = client.post("/api/v1/tenants", json={"slug": "retry-recovery", "name": "Retry Recovery"}).json()
            headers = {"X-Tenant-ID": tenant["id"]}
            with factory() as db:
                job = models.Job(
                    tenant_id=tenant["id"],
                    type="video.generate",
                    max_attempts=3,
                    payload={
                        "provider_request": {
                            "capability": "video_generation",
                            "provider": "hosted-video",
                            "prompt": "corrected prompt",
                        }
                    },
                )
                db.add(job)
                db.commit()
                job_id = job.id

                first = process_queued_provider_jobs(db, router=ProviderRouter([provider]))
                assert first[0]["status"] == "failed"
                db.refresh(job)
                assert job.status == "failed"
                assert job.attempt_count == 1

            retry = client.post(f"/api/v1/jobs/{job_id}/retry", headers=headers, json={"force": True})
            assert retry.status_code == 200, retry.text
            assert retry.json()["status"] == "queued"

            with factory() as db:
                retried_job = db.get(models.Job, job_id)
                assert retried_job is not None
                assert retried_job.attempt_count == 1  # queueing never burns an attempt
                assert retried_job.payload["provider_request"]["prompt"] == "corrected prompt"
                retried_step = db.scalar(select(models.JobStep).where(models.JobStep.job_id == job_id))
                assert retried_step is not None
                assert retried_step.status == "queued"
                retried_run = db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job_id))
                assert retried_run is not None
                assert retried_run.status == "queued"
                retried_workflow_step = db.scalar(
                    select(models.WorkflowStep).where(models.WorkflowStep.workflow_run_id == retried_run.id)
                )
                assert retried_workflow_step is not None
                assert retried_workflow_step.status == "queued"

                result = process_queued_provider_jobs(db, router=ProviderRouter([provider]))
                assert result == [{"job_id": job_id, "status": "succeeded", "output_uri": "https://cdn.example/recovered.mp4"}]
                db.refresh(retried_job)
                assert retried_job.status == "succeeded"
                assert retried_job.attempt_count == 2
                db.refresh(retried_step)
                assert retried_step.status == "succeeded"
                db.refresh(retried_run)
                assert retried_run.status == "succeeded"
                db.refresh(retried_workflow_step)
                assert retried_workflow_step.status == "succeeded"

            assert [method for method, _url, _key in calls] == ["POST", "POST"]
            assert calls[0][2] == calls[1][2] == job_id
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_operator_retry_keeps_policy_and_attempt_boundaries(tmp_path: Path):
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"retry-boundary-{uuid4().hex[:8]}", name="Retry Boundary")
            db.add(tenant)
            db.flush()
            blocked = models.Job(
                tenant_id=tenant.id,
                type="voice.clone",
                status="failed",
                retryable=True,
                policy_blocked=True,
                failure_code="consent_inactive",
                attempt_count=1,
                max_attempts=3,
            )
            exhausted = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                status="failed",
                retryable=True,
                policy_blocked=False,
                failure_code="provider_http_error",
                attempt_count=3,
                max_attempts=3,
            )
            db.add_all([blocked, exhausted])
            db.commit()

            try:
                request_job_retry(db, blocked, force=True)
            except InvalidJobTransition as exc:
                assert "Policy-blocked" in str(exc)
            else:  # pragma: no cover - assertion guard
                raise AssertionError("policy-blocked retry was accepted")

            try:
                request_job_retry(db, exhausted)
            except InvalidJobTransition as exc:
                assert "budget" in str(exc)
            else:  # pragma: no cover - assertion guard
                raise AssertionError("exhausted retry was accepted")

            db.refresh(blocked)
            db.refresh(exhausted)
            assert blocked.status == exhausted.status == "failed"
    finally:
        engine.dispose()
