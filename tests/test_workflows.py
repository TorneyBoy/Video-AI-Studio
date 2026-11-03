from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base
from app.database import get_db
from app.main import app
from app.services.temporal_adapter import TemporalUnavailable, TemporalWorkflowAdapter, TemporalWorkflowSpec
from app.services.workflows import (
    WorkflowLeaseLost,
    claim_workflow,
    complete_workflow,
    create_workflow,
    heartbeat_workflow,
    recover_expired_workflows,
    retry_workflow,
)


def _factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / (uuid4().hex + '.db')}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_workflow_lease_recovers_and_completes(tmp_path: Path):
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"wf-{uuid4().hex[:8]}", name="Workflow")
            db.add(tenant); db.flush()
            job = models.Job(tenant_id=tenant.id, type="render.standard", payload={"x": 1}, max_attempts=3)
            db.add(job); db.flush()
            run = create_workflow(db, job, steps=[{"name": "encode"}])
            db.commit()

            claimed = claim_workflow(db, run, worker_id="worker-a", lease_seconds=10)
            assert claimed is True
            assert run.status == "running"
            assert run.attempt_count == 1
            db.commit()
            heartbeat_workflow(db, run, worker_id="worker-a", lease_seconds=10)
            assert run.heartbeat_at is not None
            with pytest.raises(WorkflowLeaseLost):
                heartbeat_workflow(db, run, worker_id="worker-b", lease_seconds=10)
            db.commit()

            expired_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            run.lease_expires_at = expired_at
            db.commit()
            recovered = recover_expired_workflows(db, now=expired_at + timedelta(seconds=1))
            assert recovered == [run]
            assert run.status == "queued"
            db.commit()
            assert claim_workflow(db, run, worker_id="worker-b", lease_seconds=10, now=expired_at + timedelta(seconds=1))
            complete_workflow(db, run, "succeeded", worker_id="worker-b", result_json={"ok": True})
            db.commit()
            assert run.status == "succeeded"
            events = list(db.scalars(select(models.WorkflowEvent).where(models.WorkflowEvent.workflow_run_id == run.id).order_by(models.WorkflowEvent.sequence)))
            assert [event.event_type for event in events] == [
                "workflow.created", "workflow.claimed", "workflow.heartbeat", "workflow.lease_expired", "workflow.claimed", "workflow.succeeded"
            ]
    finally:
        engine.dispose()


def test_workflow_retry_preserves_audit_and_respects_budget(tmp_path: Path):
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"retry-{uuid4().hex[:8]}", name="Retry")
            db.add(tenant); db.flush()
            job = models.Job(tenant_id=tenant.id, type="video.generate", max_attempts=1)
            db.add(job); db.flush()
            run = create_workflow(db, job); db.commit()
            assert claim_workflow(db, run, worker_id="worker", lease_seconds=5)
            retry_workflow(db, run, worker_id="worker", error="upstream timeout", failure_code="timeout")
            assert run.status == "failed"
            assert run.failure_code == "timeout"
            db.commit()
            events = list(db.scalars(select(models.WorkflowEvent).where(models.WorkflowEvent.workflow_run_id == run.id)))
            assert events[-1].event_type == "workflow.failed"
    finally:
        engine.dispose()


def test_workflow_api_exposes_lease_and_events(tmp_path: Path):
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
            tenant = client.post("/api/v1/tenants", json={"slug": "wf-api", "name": "Workflow API"}).json()
            headers = {"X-Tenant-ID": tenant["id"]}
            job = client.post("/api/v1/jobs", headers=headers, json={"type": "video.generate"}).json()
            created = client.post(f"/api/v1/jobs/{job['id']}/workflow", headers=headers, json={"steps": [{"name": "activity"}]} )
            assert created.status_code == 201
            workflow_id = created.json()["id"]
            claimed = client.post(f"/api/v1/workflows/{workflow_id}/claim", headers=headers, json={"worker_id": "api-worker", "lease_seconds": 30})
            assert claimed.status_code == 200
            beat = client.post(f"/api/v1/workflows/{workflow_id}/heartbeat", headers=headers, json={"worker_id": "api-worker", "lease_seconds": 30})
            assert beat.status_code == 200
            events = client.get(f"/api/v1/workflows/{workflow_id}/events", headers=headers)
            assert events.status_code == 200
            assert [item["event_type"] for item in events.json()] == ["workflow.created", "workflow.claimed", "workflow.heartbeat"]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.asyncio
async def test_temporal_adapter_is_optional():
    adapter = TemporalWorkflowAdapter()
    assert adapter.available is False
    with pytest.raises(TemporalUnavailable):
        await adapter.start(TemporalWorkflowSpec("demo"), object(), workflow_id="wf-1")

