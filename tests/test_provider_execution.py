import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.config import get_settings
from app.database import Base
from app.services.providers import (
    LocalDeterministicProvider,
    ModelSnapshot,
    ProviderCapability,
    ProviderExecutionError,
    ProviderResult,
    ProviderRouter,
)
from app.services.worker import process_queued_provider_jobs


class FlakyProvider:
    name = "flaky"
    capabilities = (ProviderCapability("video_generation", ("json",), ("json",), quality_score=1.0, commercial_allowed=True),)

    def __init__(self, failures: int = 1):
        self.failures = failures
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderExecutionError(
                "upstream rejected before accepting work",
                code="upstream_timeout",
                provider=self.name,
                capability=request.capability,
                retryable=True,
                details={"submission_state": "rejected"},
            )
        return ProviderResult(
            artifact_uri="file:///tmp/flaky-result.json",
            artifact_type="video.generation_manifest",
            model=ModelSnapshot(provider=self.name, model_id="flaky", version="1"),
        )


def _database(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / (uuid4().hex + '.db')}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_provider_worker_dispatches_all_specialized_job_families(tmp_path: Path):
    engine, factory = _database(tmp_path)
    settings = get_settings()
    previous_root = settings.storage_root
    settings.storage_root = str(tmp_path / "storage")
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"provider-{uuid4().hex[:8]}", name="Provider")
            db.add(tenant)
            db.flush()
            project = models.Project(tenant_id=tenant.id, name="Provider project")
            db.add(project)
            db.flush()
            payloads = [
                {
                    "type": "voice.tts",
                    "provider_request": {"capability": "voice_tts", "text": "hello", "options": {}},
                },
                {
                    "type": "avatar.motion_transfer",
                    "provider_request": {
                        "capability": "motion_transfer",
                        "source_asset_id": "source-1",
                        "avatar_profile_id": "avatar-1",
                        "options": {},
                    },
                },
                {
                    "type": "video.generate",
                    "provider_request": {"capability": "video_generation", "prompt": "a red kite", "options": {}},
                },
                {
                    "type": "enhancement.restore",
                    "provider_request": {
                        "capability": "enhancement",
                        "asset_id": "asset-1",
                        "rights_confirmed": True,
                        "options": {},
                    },
                },
            ]
            for item in payloads:
                db.add(models.Job(tenant_id=tenant.id, project_id=project.id, type=item["type"], payload=item))
            db.commit()

            result = process_queued_provider_jobs(db, router=ProviderRouter([LocalDeterministicProvider()]))
            assert len(result) == 4
            assert {item["status"] for item in result} == {"succeeded"}
            artifacts = list(db.scalars(select(models.Artifact).where(models.Artifact.tenant_id == tenant.id)))
            assert len(artifacts) == 4
            runs = list(db.scalars(select(models.WorkflowRun).where(models.WorkflowRun.tenant_id == tenant.id)))
            assert len(runs) == 4
            assert {run.status for run in runs} == {"succeeded"}
            steps = list(
                db.scalars(
                    select(models.WorkflowStep).where(
                        models.WorkflowStep.workflow_run_id.in_([run.id for run in runs])
                    )
                )
            )
            assert len(steps) == 4
            assert {step.status for step in steps} == {"succeeded"}
            assert all(
                db.scalar(
                    select(models.WorkflowEvent).where(
                        models.WorkflowEvent.workflow_run_id == run.id,
                        models.WorkflowEvent.event_type == "workflow.succeeded",
                    )
                )
                for run in runs
            )
            assert {artifact.metadata_json["status"] for artifact in artifacts} == {"simulated"}
            for artifact in artifacts:
                path = Path(artifact.uri)
                assert path.exists()
                assert json.loads(path.read_text(encoding="utf-8"))["status"] == "simulated"
    finally:
        settings.storage_root = previous_root
        engine.dispose()


def test_provider_worker_persists_structured_failure_audit(tmp_path: Path):
    engine, factory = _database(tmp_path)
    settings = get_settings()
    previous_root = settings.storage_root
    settings.storage_root = str(tmp_path / "storage")
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"provider-fail-{uuid4().hex[:8]}", name="Provider")
            db.add(tenant)
            db.flush()
            job = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                payload={
                    "provider_request": {
                        "capability": "video_generation",
                        "provider": "missing-provider",
                        "prompt": "test",
                    }
                },
            )
            db.add(job)
            db.commit()

            result = process_queued_provider_jobs(db, router=ProviderRouter([LocalDeterministicProvider()]))
            assert result[0]["status"] == "failed"
            db.refresh(job)
            audit = json.loads(job.error)
            assert audit["code"] == "provider_unavailable"
            assert audit["provider"] == "missing-provider"
            assert job.payload["provider_failure"]["request_id"] == job.id
            step = db.scalar(select(models.JobStep).where(models.JobStep.job_id == job.id))
            assert step.status == "failed"
            assert step.payload["audit"]["code"] == "provider_unavailable"
    finally:
        settings.storage_root = previous_root
        engine.dispose()


def test_provider_worker_requeues_retryable_failure_and_recovers(tmp_path: Path):
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"retry-{uuid4().hex[:8]}", name="Retry")
            db.add(tenant)
            db.flush()
            job = models.Job(
                tenant_id=tenant.id,
                type="video.generate",
                max_attempts=3,
                payload={"provider_request": {"capability": "video_generation", "prompt": "test"}},
            )
            db.add(job)
            db.commit()
            provider = FlakyProvider()

            first = process_queued_provider_jobs(db, router=ProviderRouter([provider]))
            db.refresh(job)
            assert first[0]["status"] == "retry_scheduled"
            assert job.status == "queued"
            assert job.attempt_count == 1
            assert job.retryable is True
            assert job.failure_code == "upstream_timeout"
            assert job.next_retry_at is not None
            run = db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job.id))
            workflow_step = db.scalar(select(models.WorkflowStep).where(models.WorkflowStep.workflow_run_id == run.id))
            assert run.status == "queued"
            assert workflow_step.status == "queued"

            # Simulate the scheduler reaching the backoff deadline.
            job.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
            second = process_queued_provider_jobs(db, router=ProviderRouter([provider]))
            db.refresh(job)
            assert second[0]["status"] == "succeeded"
            assert job.status == "succeeded"
            assert job.attempt_count == 2
            assert job.retryable is False
            assert job.failure_code is None
            db.refresh(run)
            db.refresh(workflow_step)
            assert run.status == "succeeded"
            assert workflow_step.status == "succeeded"
    finally:
        engine.dispose()


def test_provider_worker_blocks_revoked_consent_and_does_not_retry(tmp_path: Path):
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"policy-{uuid4().hex[:8]}", name="Policy")
            db.add(tenant)
            db.flush()
            consent = models.Consent(tenant_id=tenant.id, subject_id="subject", scope="voice", granted=True)
            db.add(consent)
            db.flush()
            consent.revoked_at = datetime.now(timezone.utc)
            job = models.Job(
                tenant_id=tenant.id,
                type="voice.tts",
                payload={
                    "provider_request": {
                        "capability": "voice_clone",
                        "text": "test",
                        "voice_profile_id": "voice-1",
                        "consent_id": consent.id,
                    }
                },
            )
            db.add(job)
            db.commit()

            result = process_queued_provider_jobs(db, router=ProviderRouter([LocalDeterministicProvider()]))
            db.refresh(job)
            assert result[0]["status"] == "failed"
            assert job.status == "failed"
            assert job.policy_blocked is True
            assert job.retryable is False
            assert job.failure_code == "consent_inactive"
            audit = json.loads(job.error)
            assert audit["code"] == "consent_inactive"
            assert audit["retryable"] is False
    finally:
        engine.dispose()

