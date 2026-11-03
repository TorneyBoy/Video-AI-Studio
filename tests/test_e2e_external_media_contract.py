"""End-to-end control-plane contracts for externally hosted media work.

These tests model the production boundary: bytes live in object storage and
inference is supplied by a hosted Provider.  The only execution doubles here
are an in-process Provider contract and a signed callback; no media is
downloaded, transcoded or sent to a real vendor.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import routers
from app.database import Base, get_db
from app.main import app
from app.services.provider_callbacks import verify_signature
from app.services.providers import ModelSnapshot, ProviderCapability, ProviderResult, ProviderRouter
from app.services.storage_attestation import sign_attestation
from app.services.worker import process_queued_analysis_jobs


@contextmanager
def _api(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / (uuid4().hex + '.db')}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    previous_storage_secret = routers.settings.storage_attestation_secret
    previous_callback_secret = routers.settings.provider_webhook_secret
    previous_provider_mode = routers.settings.provider_mode
    routers.settings.storage_attestation_secret = "storage-contract-secret"
    routers.settings.provider_webhook_secret = "provider-contract-secret"
    routers.settings.provider_mode = "api_only"
    try:
        with TestClient(app) as client:
            yield client, factory
    finally:
        routers.settings.storage_attestation_secret = previous_storage_secret
        routers.settings.provider_webhook_secret = previous_callback_secret
        routers.settings.provider_mode = previous_provider_mode
        app.dependency_overrides.clear()
        engine.dispose()


class _HostedAnalysisContract:
    name = "hosted-analysis-contract"
    capabilities = (
        ProviderCapability(
            "media_analysis",
            ("media",),
            ("analysis/json",),
            quality_score=1.0,
            commercial_allowed=True,
        ),
    )

    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return ProviderResult(
            artifact_uri="https://cdn.example.test/analysis.json",
            artifact_type="analysis",
            model=ModelSnapshot(provider=self.name, model_id="analysis-v1", version="2026-10"),
            provenance={
                "analysis": {
                    "schema_version": "1",
                    "media": {"duration_seconds": 4.0},
                    "shots": [{"start": 0.0, "end": 4.0, "quality": 0.95}],
                }
            },
            usage={"seconds": 4},
            cost={"amount": 0.10, "currency": "USD"},
        )


def _tenant(client: TestClient, slug: str) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/v1/tenants", json={"slug": slug, "name": slug.title()})
    assert response.status_code == 201, response.text
    tenant = response.json()
    return tenant, {"X-Tenant-ID": tenant["id"]}


def _signed_callback(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"provider-contract-secret", raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={digest}"


def test_external_upload_analysis_generation_callback_and_budget_contract(tmp_path: Path):
    """A tenant can complete one hosted-media flow without local media work."""

    with _api(tmp_path) as (client, factory):
        tenant, headers = _tenant(client, f"flow-{uuid4().hex[:8]}")
        other_tenant, other_headers = _tenant(client, f"other-{uuid4().hex[:8]}")
        project = client.post("/api/v1/projects", headers=headers, json={"name": "Hosted Flow"}).json()
        other_project = client.post("/api/v1/projects", headers=other_headers, json={"name": "Other"}).json()

        configured = client.put(
            f"/api/v1/tenants/{tenant['id']}/budget",
            headers=headers,
            json={"currency": "USD", "monthly_limit_micros": 1000000, "task_limit_micros": 700000},
        )
        assert configured.status_code == 200, configured.text

        asset_id = f"asset_{uuid4().hex}"
        sha256 = "a" * 64
        session_response = client.post(
            "/api/v1/assets/remote-upload-session",
            headers=headers,
            json={
                "asset_id": asset_id,
                "name": "camera.mp4",
                "content_type": "video/mp4",
                "size_bytes": 4096,
                "sha256": sha256,
                "object_uri": "s3://media-bucket/tenant-a/camera.mp4",
                "upload_url": "https://upload.example.test/camera-upload",
                "project_id": project["id"],
            },
        )
        assert session_response.status_code == 201, session_response.text
        session = session_response.json()
        assert session["status"] == "created"

        # A pending object cannot enter any Provider workflow, and another
        # tenant cannot even observe the asset through a tenant-scoped path.
        pending_generation = client.post(
            "/api/v1/jobs/video-generation",
            headers={**headers, "Idempotency-Key": "pending-generation"},
            json={"project_id": project["id"], "prompt": "cinematic", "reference_asset_ids": [asset_id]},
        )
        assert pending_generation.status_code == 409
        assert client.get(f"/api/v1/assets/{asset_id}", headers=other_headers).status_code == 404
        assert client.post(f"/api/v1/assets/{asset_id}/analyze", headers=other_headers).status_code == 404

        expires_at = datetime.fromisoformat(session["expires_at"])
        nonce = f"nonce-{uuid4().hex}"
        attestation = {
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "nonce": nonce,
        }
        attestation["signature"] = sign_attestation(
            "storage-contract-secret",
            asset_id=asset_id,
            object_uri=session["object_uri"],
            size_bytes=4096,
            sha256=sha256,
            expires_at=datetime.fromisoformat(attestation["expires_at"]),
            nonce=nonce,
        )
        # The proof must be no later than the upload intent expiry.
        assert datetime.fromisoformat(attestation["expires_at"]) <= expires_at
        completed = client.post(
            f"/api/v1/assets/{asset_id}/remote-upload-complete",
            headers=headers,
            json={"size_bytes": 4096, "sha256": sha256, "etag": "etag-a", "attestation": attestation},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["metadata"]["storage"]["status"] == "ready"

        analysis = client.post(
            f"/api/v1/assets/{asset_id}/analyze",
            headers={**headers, "Idempotency-Key": "analysis-once"},
            json={"shot_analysis": True, "options": {"estimated_cost": {"amount": 0.10, "currency": "USD"}}},
        )
        assert analysis.status_code == 201, analysis.text
        analysis_job = analysis.json()
        analysis_replay = client.post(
            f"/api/v1/assets/{asset_id}/analyze",
            headers={**headers, "Idempotency-Key": "analysis-once"},
            json={"shot_analysis": True},
        )
        assert analysis_replay.status_code == 201
        assert analysis_replay.json()["id"] == analysis_job["id"]

        usage = client.get("/api/v1/billing/usage", headers=headers)
        assert usage.status_code == 200
        assert len(usage.json()) == 1
        assert usage.json()[0]["status"] == "reserved"

        provider = _HostedAnalysisContract()
        with factory() as db:
            worker_result = process_queued_analysis_jobs(db, router=ProviderRouter([provider]))
        assert worker_result == [{"job_id": analysis_job["id"], "status": "succeeded", "asset_id": asset_id}]
        assert provider.requests[0].inputs["asset_uri"] == session["object_uri"]
        assert provider.requests[0].tenant_id == tenant["id"]

        generation = client.post(
            "/api/v1/jobs/video-generation",
            headers={**headers, "Idempotency-Key": "generation-once"},
            json={
                "project_id": project["id"],
                "prompt": "cinematic launch",
                "reference_asset_ids": [asset_id],
                "provider": "hosted-video",
                "model_id": "video-v1",
                "model_version": "2026-10",
                "options": {"estimated_cost": {"amount": 0.40, "currency": "USD"}},
            },
        )
        assert generation.status_code == 201, generation.text
        generation_job = generation.json()
        assert generation_job["payload"]["provider_request"]["reference_asset_ids"] == [asset_id]

        # Idempotency is tenant scoped: the same client key in another tenant
        # creates an independent Job, and its budget view never sees A's rows.
        other_generation = client.post(
            "/api/v1/jobs/video-generation",
            headers={**other_headers, "Idempotency-Key": "generation-once"},
            json={"project_id": other_project["id"], "prompt": "other tenant"},
        )
        assert other_generation.status_code == 201
        assert other_generation.json()["id"] != generation_job["id"]
        assert client.get("/api/v1/billing/usage", headers=other_headers).json() == []
        assert client.get(f"/api/v1/jobs/{generation_job['id']}", headers=other_headers).status_code == 404

        callback_body = {
            "event_id": "generation-complete-1",
            "request_id": generation_job["id"],
            "status": "succeeded",
            "provider": "hosted-video",
            "external_job_id": "vendor-generation-1",
            "artifact_uri": "https://cdn.example.test/rendered.mp4",
            "artifact_type": "video.rendered",
            "usage": {"seconds": 8},
            "cost": {"amount": 0.40, "currency": "USD"},
        }
        raw, signature = _signed_callback(callback_body)
        callback = client.post(
            f"/api/v1/provider-callbacks/{generation_job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "hosted-video"},
        )
        assert callback.status_code == 200, callback.text
        assert callback.json()["status"] == "succeeded"
        artifacts = client.get(f"/api/v1/artifacts?job_id={generation_job['id']}", headers=headers)
        assert artifacts.status_code == 200
        assert [item["uri"] for item in artifacts.json()] == [callback_body["artifact_uri"]]

        replay = client.post(
            f"/api/v1/provider-callbacks/{generation_job['id']}",
            content=raw,
            headers={"X-Provider-Signature": signature, "X-Provider-Name": "hosted-video"},
        )
        assert replay.status_code == 200
        assert len(client.get(f"/api/v1/artifacts?job_id={generation_job['id']}", headers=headers).json()) == 1

        budget = client.get(f"/api/v1/tenants/{tenant['id']}/budget", headers=headers)
        assert budget.status_code == 200
        assert budget.json()["settled_micros"] == 500000
        assert budget.json()["reserved_micros"] == 0
        assert budget.json()["remaining_micros"] == 500000


def test_external_upload_attestation_is_bound_to_object_and_tenant(tmp_path: Path):
    with _api(tmp_path) as (client, _factory):
        tenant, headers = _tenant(client, f"attest-{uuid4().hex[:8]}")
        project = client.post("/api/v1/projects", headers=headers, json={"name": "Attestation"}).json()
        asset_id = f"asset_{uuid4().hex}"
        client.post(
            "/api/v1/assets/remote-upload-session",
            headers=headers,
            json={
                "asset_id": asset_id,
                "name": "clip.mp4",
                "content_type": "video/mp4",
                "size_bytes": 100,
                "object_uri": "s3://bucket/clip.mp4",
                "upload_url": "https://upload.example.test/clip",
                "project_id": project["id"],
            },
        ).json()
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        nonce = f"nonce-{uuid4().hex}"
        proof = {
            "expires_at": expires.isoformat(),
            "nonce": nonce,
            "signature": sign_attestation(
                "storage-contract-secret",
                asset_id=asset_id,
                object_uri="s3://bucket/other-object.mp4",
                size_bytes=100,
                sha256=None,
                expires_at=expires,
                nonce=nonce,
            ),
        }
        rejected = client.post(
            f"/api/v1/assets/{asset_id}/remote-upload-complete",
            headers=headers,
            json={"size_bytes": 100, "attestation": proof},
        )
        assert rejected.status_code == 401
        assert rejected.json()["detail"]["code"] == "attestation_invalid"
        assert client.get(f"/api/v1/assets/{asset_id}", headers=headers).json()["metadata"]["storage"]["status"] == "created"

        # Ensure the helper used to issue callback signatures is itself kept
        # compatible with the callback verifier contract.
        callback_raw, callback_signature = _signed_callback({"event_id": "probe", "status": "running"})
        assert verify_signature(callback_raw, callback_signature, "provider-contract-secret")
