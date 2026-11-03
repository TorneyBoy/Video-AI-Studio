"""Delivery gates, archive recovery and integrity without vendor spending."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models
from app.config import Settings
from app.database import Base
from app.services.archive_worker import process_archive_tasks
from app.services.artifact_delivery import archive_artifact, artifact_access
from app.services.storage_attestation import sign_attestation
from app.services.storage_gateway import StorageGatewayError


SECRET = "archive-test-proof-secret-32-characters"
SHA = "a" * 64


class ArchiveGateway:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.sign_hook = None
        self.proof_hash = SHA

    def archive_artifact(self, *, artifact_id, tenant_id, source_uri, expected_sha256):
        self.calls += 1
        if self.fail:
            raise StorageGatewayError("temporary_archive_failure", "Do not persist this signed URL")
        uri = f"oss://test-bucket/tenants/{tenant_id}/artifacts/{artifact_id}/output"
        expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        signature = sign_attestation(SECRET, asset_id=artifact_id, object_uri=uri, size_bytes=123,
                                     sha256=self.proof_hash, nonce="fixture-archive-proof-nonce", expires_at=expiry)
        return {"artifact_id": artifact_id, "object_uri": uri, "sha256": self.proof_hash, "size_bytes": 123,
                "attestation": {"signature": signature, "nonce": "fixture-archive-proof-nonce", "expires_at": expiry.isoformat()}}

    def artifact_access(self, *, artifact_id, tenant_id, object_uri, purpose, ttl_seconds):
        if self.sign_hook:
            self.sign_hook()
        return {"artifact_id": artifact_id, "url": "https://test-bucket.example/media?short-lived-signature",
                "purpose": purpose, "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)}


@pytest.fixture
def archive_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'delivery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        tenant = models.Tenant(id="tenant", slug="archive-tenant", name="Archive tenant")
        job = models.Job(id="render-job", tenant_id="tenant", type="render.draft", status="succeeded", payload={})
        db.add_all([tenant, job])
        db.flush()
        row = models.Artifact(id="artifact", tenant_id="tenant", job_id=job.id, kind="render",
            uri="oss://test-bucket/tenants/tenant/render/output.mp4", metadata_json={"output_sha256": SHA,
                "quality_control": {"status": "passed", "output_sha256": SHA, "size_bytes": 123}})
        db.add(row)
        db.commit()
        settings = Settings(storage_gateway_url="https://gateway.example", storage_gateway_api_key="test-key",
                            storage_attestation_secret=SECRET)
        yield SimpleNamespace(db=db, factory=factory, row=row, job=job, settings=settings, gateway=ArchiveGateway())
    engine.dispose()


def _archive(ctx):
    return archive_artifact(ctx.db, tenant_id="tenant", artifact_id="artifact", settings=ctx.settings, gateway=ctx.gateway)


def _access(ctx, tenant="tenant"):
    return artifact_access(ctx.db, tenant_id=tenant, artifact_id="artifact", purpose="preview", gateway=ctx.gateway)


def test_archive_integrity_idempotency_and_tenant_gate(archive_context):
    ctx = archive_context
    with pytest.raises(StorageGatewayError, match="not yet archived"):
        _access(ctx)
    saved = _archive(ctx)
    assert saved.sha256 == SHA and saved.size_bytes == 123
    assert _archive(ctx).object_uri == saved.object_uri and ctx.gateway.calls == 1
    assert _access(ctx)["purpose"] == "preview"
    with pytest.raises(StorageGatewayError) as caught:
        _access(ctx, tenant="other")
    assert caught.value.status_code == 404


def test_archive_different_content_cannot_be_delivered(archive_context):
    ctx = archive_context
    ctx.gateway.proof_hash = "b" * 64
    with pytest.raises(StorageGatewayError) as caught:
        _archive(ctx)
    assert caught.value.code == "storage_archive_verification_failed"
    assert ctx.db.get(models.ArtifactStorage, "artifact") is None


@pytest.mark.parametrize("gate", ["quality", "size", "job", "revocation", "retention"])
def test_delivery_rechecks_quality_job_retention_and_revocation(archive_context, gate):
    ctx = archive_context
    archived = _archive(ctx)
    if gate == "quality":
        ctx.row.metadata_json = {"quality_control": {"status": "failed"}}
    elif gate == "size":
        ctx.row.metadata_json = {"quality_control": {"status": "passed", "output_sha256": SHA, "size_bytes": 999}}
    elif gate == "job":
        ctx.job.policy_blocked = True
    elif gate == "revocation":
        ctx.row.delivery_status = "revoked"
    else:
        archived.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    ctx.db.commit()
    with pytest.raises(StorageGatewayError):
        _access(ctx)


def test_revocation_during_network_signing_does_not_return_url(archive_context):
    ctx = archive_context
    _archive(ctx)

    def revoke():
        with ctx.factory.begin() as other:
            other.get(models.Artifact, "artifact").delivery_status = "revoked"

    ctx.gateway.sign_hook = revoke
    with pytest.raises(StorageGatewayError) as caught:
        _access(ctx)
    assert caught.value.code == "artifact_delivery_forbidden"


def test_archive_queue_survives_failure_and_resumes_same_artifact(archive_context):
    ctx = archive_context
    ctx.gateway.fail = True
    first = process_archive_tasks(ctx.db, settings=ctx.settings, gateway=ctx.gateway)
    assert first[0]["archive_status"] == "pending"
    task = ctx.db.get(models.ArtifactArchiveTask, "artifact")
    assert task.attempts == 1 and task.last_error == "temporary_archive_failure"
    assert process_archive_tasks(ctx.db, settings=ctx.settings, gateway=ctx.gateway) == []
    task.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    ctx.db.commit()
    ctx.gateway.fail = False
    second = process_archive_tasks(ctx.db, settings=ctx.settings, gateway=ctx.gateway)
    assert second[0]["archive_status"] == "succeeded"
    assert ctx.db.scalar(select(models.ArtifactArchiveTask)).attempts == 2
    assert process_archive_tasks(ctx.db, settings=ctx.settings, gateway=ctx.gateway) == []
    assert ctx.gateway.calls == 2


def test_archive_queue_recovers_expired_lease_and_caps_retries(archive_context):
    ctx = archive_context
    ctx.db.add(models.ArtifactArchiveTask(artifact_id="artifact", tenant_id="tenant", status="running", attempts=4,
        lease_token="crashed-worker", lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    ctx.db.commit()
    ctx.gateway.fail = True
    result = process_archive_tasks(ctx.db, settings=ctx.settings, gateway=ctx.gateway)
    assert result[0]["archive_status"] == "failed"
    assert process_archive_tasks(ctx.db, settings=ctx.settings, gateway=ctx.gateway) == []
    assert ctx.gateway.calls == 1
