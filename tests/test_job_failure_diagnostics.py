"""Public failure diagnostics and operator retry contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base, get_db
from app.main import app
from app.services.failure_diagnostics import failure_projection, public_payload
from app.services.jobs import InvalidJobTransition, request_job_retry
from app.services.workflows import create_workflow, request_workflow_retry


def _factory(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / (uuid4().hex + '.db')}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_failure_projection_is_structured_and_redacted():
    audit = {
        "schema_version": "provider-failure/1.0",
        "code": "provider_http_error",
        "message": "provider failed at https://provider.example/run?token=secret",
        "provider": "hosted-video",
        "capability": "video_generation",
        "retryable": True,
        "details": {
            "provider_job_id": "remote-123",
            "http_status": 503,
            "token": "must-not-leak",
            "url": "https://provider.example/private",
            "nested": {"reason": "upstream"},
        },
    }
    result = failure_projection(
        error=json.dumps(audit),
        failure_code="provider_http_error",
        retryable=True,
        payload={"provider_failure": audit},
    )
    assert result is not None
    assert result["code"] == "provider_http_error"
    assert result["message"] == "provider failed at [REDACTED_URL]"
    assert result["provider"] == "hosted-video"
    assert result["details"] == {"provider_job_id": "remote-123", "http_status": 503}
    assert "token" not in json.dumps(result)
    assert "provider.example" not in json.dumps(result)


def test_failure_message_redacts_inline_credentials():
    result = failure_projection(
        error="authorization: Bearer super-secret-token",
        failure_code="provider_http_error",
    )
    assert result is not None
    assert result["message"] == "authorization=[REDACTED]"
    assert "super-secret-token" not in result["message"]


def test_public_payload_preserves_large_plans_and_usage_but_redacts_provider_envelopes():
    class JobLike:
        payload = {
            "plan": {"selected": [{"asset_id": f"asset-{index}"} for index in range(25)]},
            "provider_request": {
                "capability": "video_generation",
                "prompt": "a private prompt",
                "asset_uri": "s3://private-bucket/source.mp4",
            },
            "provider_result": {
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "artifact_uri": "s3://private-bucket/output.mp4",
            },
            "api_token": "must-not-leak",
        }

    payload = public_payload(JobLike())
    assert len(payload["plan"]["selected"]) == 25
    assert payload["provider_request"] == {"capability": "video_generation"}
    assert payload["provider_result"]["usage"] == {"input_tokens": 12, "output_tokens": 4}
    assert "artifact_uri" not in payload["provider_result"]
    assert "must-not-leak" not in json.dumps(payload)
    assert "private-bucket" not in json.dumps(payload)


def test_operator_retry_is_idempotent_and_preserves_async_provider_identity(tmp_path: Path):
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"diag-{uuid4().hex[:8]}", name="Diagnostics")
            db.add(tenant)
            db.flush()
            audit = {
                "schema_version": "provider-failure/1.0",
                "code": "provider_async_pending",
                "message": "provider job is still processing",
                "retryable": True,
                "details": {"provider_job_id": "remote-task-1", "poll_after_seconds": 4},
            }
            job = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                status="failed",
                payload={"provider_failure": audit, "provider_request": {"capability": "video_generation"}},
                error=json.dumps(audit),
                failure_code="provider_async_pending",
                retryable=True,
                attempt_count=1,
                max_attempts=3,
            )
            db.add(job)
            db.flush()
            run = create_workflow(db, job, max_attempts=3)
            run.status = "failed"
            run.attempt_count = 1
            run.failure_code = "provider_async_pending"
            run.error = json.dumps(audit)
            run.completed_at = datetime.now(timezone.utc)
            db.commit()

            request_job_retry(db, job)
            request_workflow_retry(db, run)
            db.commit()
            first_due = job.next_retry_at
            event_count = len(
                list(db.scalars(select(models.WorkflowEvent).where(models.WorkflowEvent.workflow_run_id == run.id)))
            )

            # The second request is a no-op and cannot move the deadline or
            # append an additional event that would trigger duplicate work.
            request_job_retry(db, job)
            request_workflow_retry(db, run)
            db.commit()
            assert job.status == "queued"
            assert run.status == "queued"
            assert job.next_retry_at == first_due
            assert job.payload["provider_failure"]["details"]["provider_job_id"] == "remote-task-1"
            assert len(list(db.scalars(select(models.WorkflowEvent).where(models.WorkflowEvent.workflow_run_id == run.id)))) == event_count
    finally:
        engine.dispose()


def test_operator_retry_rejects_policy_failures_and_exhausted_budget(tmp_path: Path):
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"diag-policy-{uuid4().hex[:8]}", name="Diagnostics")
            db.add(tenant)
            db.flush()
            blocked = models.Job(
                tenant_id=tenant.id,
                type="voice.clone",
                status="failed",
                payload={},
                error="consent inactive",
                failure_code="consent_inactive",
                retryable=False,
                policy_blocked=True,
                attempt_count=1,
                max_attempts=3,
            )
            exhausted = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                status="failed",
                payload={},
                error="upstream timeout",
                failure_code="upstream_timeout",
                retryable=True,
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
                raise AssertionError("policy failure was retried")
            try:
                request_job_retry(db, exhausted)
            except InvalidJobTransition as exc:
                assert "budget" in str(exc)
            else:  # pragma: no cover - assertion guard
                raise AssertionError("exhausted job was retried")
    finally:
        engine.dispose()


def test_job_retry_api_returns_safe_failure_and_requeues_workflow(tmp_path: Path):
    engine, factory = _factory(tmp_path)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            tenant = client.post("/api/v1/tenants", json={"slug": "diag-api", "name": "Diagnostics API"}).json()
            headers = {"X-Tenant-ID": tenant["id"]}
            created = client.post("/api/v1/jobs", headers=headers, json={"type": "video.generate"})
            assert created.status_code == 201
            job_id = created.json()["id"]
            with factory() as db:
                job = db.get(models.Job, job_id)
                assert job is not None
                audit = {
                    "schema_version": "provider-failure/1.0",
                    "code": "provider_http_error",
                    "message": "upstream https://provider.example/private",
                    "retryable": True,
                    "details": {"provider_job_id": "remote-2", "token": "secret"},
                }
                job.status = "failed"
                job.error = json.dumps(audit)
                job.failure_code = "provider_http_error"
                job.retryable = True
                job.attempt_count = 1
                job.max_attempts = 3
                run = create_workflow(db, job, max_attempts=3)
                run.status = "failed"
                run.attempt_count = 1
                run.error = json.dumps(audit)
                run.failure_code = "provider_http_error"
                db.commit()

            read = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
            assert read.status_code == 200
            body = read.json()
            assert body["error"] == "upstream [REDACTED_URL]"
            assert body["failure"]["code"] == "provider_http_error"
            assert "token" not in json.dumps(body["failure"])
            assert "provider.example" not in json.dumps(body["payload"])
            retried = client.post(f"/api/v1/jobs/{job_id}/retry", headers=headers, json={"delay_seconds": 0})
            assert retried.status_code == 200
            assert retried.json()["status"] == "queued"
            repeated = client.post(f"/api/v1/jobs/{job_id}/retry", headers=headers, json={"delay_seconds": 20})
            assert repeated.status_code == 200
            assert repeated.json()["next_retry_at"] == retried.json()["next_retry_at"]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
