import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.api import routers
from app.database import Base, get_db
from app.main import app
from app.services.provider_callbacks import apply_callback


@contextmanager
def client_for(tmp_path: Path):
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
    previous = routers.settings.provider_webhook_secret
    previous_require_request_id = routers.settings.provider_webhook_require_request_id
    routers.settings.provider_webhook_secret = "callback-test-secret"
    routers.settings.provider_webhook_require_request_id = False
    try:
        with TestClient(app) as client:
            yield client
    finally:
        routers.settings.provider_webhook_secret = previous
        routers.settings.provider_webhook_require_request_id = previous_require_request_id
        app.dependency_overrides.clear()
        engine.dispose()


def signed(body: dict) -> tuple[bytes, str]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    digest = hmac.new(b"callback-test-secret", raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={digest}"


def signed_with_timestamp(body: dict, timestamp: str) -> tuple[bytes, str]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    digest = hmac.new(b"callback-test-secret", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={digest}"


def create_job(client: TestClient) -> tuple[dict, dict]:
    tenant = client.post("/api/v1/tenants", json={"slug": f"t-{uuid4().hex[:8]}", "name": "Callbacks"}).json()
    headers = {"X-Tenant-ID": tenant["id"]}
    project = client.post("/api/v1/projects", headers=headers, json={"name": "Remote provider"}).json()
    job = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "type": "video.generate",
            "project_id": project["id"],
            "payload": {"provider_request": {"capability": "video_generation", "provider": "remote-video"}},
        },
    ).json()
    return headers, job


def test_signed_provider_callback_persists_remote_artifact_and_is_idempotent(tmp_path: Path):
    with client_for(tmp_path) as client:
        _headers, job = create_job(client)
        body = {
            "event_id": "evt-1",
            "status": "succeeded",
            "provider": "remote-video",
            "external_job_id": "vendor-1",
            "artifact_uri": "https://cdn.example.com/output.mp4",
            "artifact_type": "video.rendered",
            "metadata": {"duration_seconds": 8.2},
        }
        raw, signature = signed(body)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "remote-video"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"
        artifacts = client.get(f"/api/v1/artifacts?job_id={job['id']}", headers=_headers).json()
        assert len(artifacts) == 1
        assert artifacts[0]["uri"] == body["artifact_uri"]
        replay = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "remote-video"},
        )
        assert replay.status_code == 200
        assert len(client.get(f"/api/v1/artifacts?job_id={job['id']}", headers=_headers).json()) == 1


def test_provider_callback_redacts_credential_shaped_metadata(tmp_path: Path):
    with client_for(tmp_path) as client:
        headers, job = create_job(client)
        body = {
            "event_id": "evt-redact",
            "status": "succeeded",
            "provider": "remote-video",
            "external_job_id": "vendor-redact",
            "artifact_uri": "https://cdn.example.com/redact.mp4",
            "metadata": {
                "duration_seconds": 2.0,
                "authorization": "Bearer should-not-persist",
                "nested": {"api_token": "secret-value", "safe": "kept"},
            },
        }
        raw, signature = signed(body)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "remote-video"},
        )
        assert response.status_code == 200
        artifact = client.get(f"/api/v1/artifacts?job_id={job['id']}", headers=headers).json()[0]
        metadata = artifact["metadata"]["metadata"]
        assert metadata["duration_seconds"] == 2.0
        assert metadata["authorization"] == "[REDACTED]"
        assert metadata["nested"] == {"api_token": "[REDACTED]", "safe": "kept"}


def test_provider_callback_rejects_bad_signature_and_local_uri(tmp_path: Path):
    with client_for(tmp_path) as client:
        _headers, job = create_job(client)
        body = {"event_id": "evt-2", "status": "succeeded", "artifact_uri": "file:///tmp/out.mp4"}
        raw, _signature = signed(body)
        denied = client.post(f"/api/v1/provider-callbacks/{job['id']}", content=raw, headers={"X-Provider-Signature": "sha256=bad"})
        assert denied.status_code == 401
        _raw, signature = signed(body)
        rejected = client.post(f"/api/v1/provider-callbacks/{job['id']}", content=raw, headers={"X-Provider-Signature": signature})
        assert rejected.status_code == 400


def test_provider_callback_binds_optional_request_id_to_target_job(tmp_path: Path):
    with client_for(tmp_path) as client:
        _headers, job = create_job(client)
        body = {
            "event_id": "evt-wrong-request",
            "request_id": "another-job",
            "status": "succeeded",
            "provider": "remote-video",
            "artifact_uri": "https://cdn.example.com/output.mp4",
        }
        raw, signature = signed(body)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "remote-video"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "callback request_id does not match the target job"


def test_provider_callback_can_require_request_id_in_production_mode(tmp_path: Path):
    with client_for(tmp_path) as client:
        _headers, job = create_job(client)
        routers.settings.provider_webhook_require_request_id = True
        body = {
            "event_id": "evt-required-request-id",
            "status": "running",
            "provider": "remote-video",
        }
        raw, signature = signed(body)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "remote-video"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "callback_request_id_required"


def test_provider_callback_strict_binding_requires_external_task_id(tmp_path: Path):
    with client_for(tmp_path) as client:
        _headers, job = create_job(client)
        routers.settings.provider_webhook_require_request_id = True
        body = {
            "event_id": "evt-required-external-id",
            "request_id": job["id"],
            "status": "running",
            "provider": "remote-video",
        }
        raw, signature = signed(body)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "remote-video"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "callback_external_job_id_required"


def test_provider_callback_accepts_fresh_timestamped_signature(tmp_path: Path):
    with client_for(tmp_path) as client:
        _headers, job = create_job(client)
        timestamp = str(int(time.time()))
        body = {"event_id": "evt-3", "status": "succeeded", "artifact_uri": "s3://bucket/output.mp4"}
        raw, signature = signed_with_timestamp(body, timestamp)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Timestamp": timestamp},
        )
        assert response.status_code == 200


def test_provider_callback_rejects_expired_timestamped_signature(tmp_path: Path):
    with client_for(tmp_path) as client:
        _headers, job = create_job(client)
        timestamp = str(int(time.time()) - routers.settings.provider_webhook_max_age_seconds - 1)
        body = {"event_id": "evt-expired", "status": "succeeded", "artifact_uri": "s3://bucket/output.mp4"}
        raw, signature = signed_with_timestamp(body, timestamp)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={
                "X-Provider-Signature": signature,
                "X-Provider-Timestamp": timestamp,
            },
        )
        assert response.status_code == 401


def test_provider_callback_replay_with_same_event_id_does_not_replace_artifact(tmp_path: Path):
    with client_for(tmp_path) as client:
        headers, job = create_job(client)
        first = {
            "event_id": "evt-idempotent",
            "status": "succeeded",
            "provider": "remote-video",
            "external_job_id": "vendor-idempotent",
            "artifact_uri": "https://cdn.example.com/first.mp4",
            "usage": {"seconds": 4.0},
            "cost": {"amount": 0.08, "currency": "USD"},
        }
        raw, signature = signed(first)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "remote-video"},
        )
        assert response.status_code == 200

        # Reusing an event ID with a different body is a conflict; the first
        # committed artifact and accounting evidence must remain unchanged.
        replay = {**first, "artifact_uri": "https://cdn.example.com/second.mp4", "cost": {"amount": 99}}
        replay_raw, replay_signature = signed(replay)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=replay_raw,
            headers={"X-Provider-Signature": replay_signature, "X-Provider-Name": "remote-video"},
        )
        assert response.status_code == 409
        artifacts = client.get(f"/api/v1/artifacts?job_id={job['id']}", headers=headers).json()
        assert len(artifacts) == 1
        assert artifacts[0]["uri"] == first["artifact_uri"]
        assert artifacts[0]["metadata"]["usage"] == first["usage"]
        assert artifacts[0]["metadata"]["cost"] == first["cost"]


def test_provider_callback_completes_attached_workflow(tmp_path: Path):
    with client_for(tmp_path) as client:
        headers, job = create_job(client)
        workflow = client.post(f"/api/v1/jobs/{job['id']}/workflow", headers=headers, json={}).json()
        body = {
            "event_id": "evt-workflow",
            "status": "succeeded",
            "provider": "remote-video",
            "external_job_id": "vendor-workflow",
            "artifact_uri": "https://cdn.example.com/workflow.mp4",
        }
        raw, signature = signed(body)
        response = client.post(
            f"/api/v1/provider-callbacks/{job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature},
        )
        assert response.status_code == 200
        completed = client.get(f"/api/v1/workflows/{workflow['id']}", headers=headers).json()
        assert completed["status"] == "succeeded"


def test_provider_callback_refreshes_stale_job_before_idempotency_check(tmp_path: Path):
    """A delayed callback request must observe a committed event marker.

    Provider retries can overlap in separate database sessions.  The second
    session deliberately keeps a stale Job instance while the first session
    commits the callback; ``apply_callback`` must lock/refresh before checking
    ``provider_callback`` so it returns an idempotent success instead of
    trying to settle a second artifact or usage row.
    """
    engine = create_engine(f"sqlite:///{tmp_path / (uuid4().hex + '.db')}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    payload = {
        "event_id": "evt-stale-session",
        "status": "succeeded",
        "provider": "remote-video",
        "external_job_id": "vendor-stale-session",
        "artifact_uri": "s3://bucket/stale-session.mp4",
        "artifact_type": "video.rendered",
        "cost": {"amount": 0.08, "currency": "USD"},
    }
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"callback-{uuid4().hex[:8]}", name="Callbacks")
            db.add(tenant)
            db.flush()
            job = models.Job(tenant_id=tenant.id, type="video.generate", payload={})
            db.add(job)
            db.commit()
            job_id = job.id

        stale_session = factory()
        writer_session = factory()
        try:
            stale_job = stale_session.get(models.Job, job_id)
            writer_job = writer_session.get(models.Job, job_id)
            assert stale_job is not None and writer_job is not None

            applied, already_applied = apply_callback(
                writer_session,
                job=writer_job,
                payload=payload,
                provider="remote-video",
            )
            writer_session.commit()
            assert applied.status == "succeeded"
            assert already_applied is False

            with patch.object(stale_session, "refresh", wraps=stale_session.refresh) as refresh:
                replayed, already_applied = apply_callback(
                    stale_session,
                    job=stale_job,
                    payload=payload,
                    provider="remote-video",
                )
                assert refresh.call_args.kwargs["with_for_update"] is True
            assert replayed.status == "succeeded"
            assert already_applied is True
            stale_session.commit()
            assert stale_session.query(models.Artifact).filter_by(job_id=job_id).count() == 1
            assert stale_session.query(models.UsageLedger).filter_by(job_id=job_id).count() == 1
        finally:
            stale_session.close()
            writer_session.close()
    finally:
        engine.dispose()
