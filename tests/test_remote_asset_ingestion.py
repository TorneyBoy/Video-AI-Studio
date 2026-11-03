from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app
from app.api import routers
from app.services.storage_attestation import sign_attestation


@contextmanager
def make_client(tmp_path: Path) -> Generator[TestClient, None, None]:
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
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _tenant_project(client: TestClient) -> tuple[dict[str, str], dict]:
    tenant = client.post("/api/v1/tenants", json={"slug": f"remote-{uuid4().hex[:8]}", "name": "Remote"}).json()
    headers = {"X-Tenant-ID": tenant["id"]}
    project = client.post("/api/v1/projects", headers=headers, json={"name": "Object Storage"}).json()
    return headers, project


def test_register_remote_asset_is_ready_without_media_fetch(tmp_path: Path):
    with make_client(tmp_path) as client:
        headers, project = _tenant_project(client)
        response = client.post(
            "/api/v1/assets/register-remote",
            headers=headers,
            json={
                "name": "clip.mp4",
                "kind": "video/mp4",
                "uri": "s3://video-inputs/clip.mp4",
                "size_bytes": 1024,
                "project_id": project["id"],
            },
        )
        assert response.status_code == 201
        asset = response.json()
        assert asset["uri"] == "s3://video-inputs/clip.mp4"
        assert asset["metadata"]["storage"]["status"] == "ready"

        analysis = client.post(f"/api/v1/assets/{asset['id']}/analyze", headers=headers, json={})
        assert analysis.status_code == 201


def test_presigned_session_requires_completion_before_analysis(tmp_path: Path):
    with make_client(tmp_path) as client:
        headers, project = _tenant_project(client)
        response = client.post(
            "/api/v1/assets/remote-upload-session",
            headers=headers,
            json={
                "name": "clip.mp4",
                "content_type": "video/mp4",
                "size_bytes": 4,
                "object_uri": "https://cdn.example.test/video/clip.mp4",
                "upload_url": "https://storage.example.test/presigned?signature=redacted",
                "upload_headers": {"Content-Type": "video/mp4"},
                "project_id": project["id"],
            },
        )
        assert response.status_code == 201
        session = response.json()
        assert session["status"] == "created"
        assert session["upload_method"] == "PUT"
        assert session["complete_uri"].endswith("/remote-upload-complete")

        blocked = client.post(f"/api/v1/assets/{session['asset_id']}/analyze", headers=headers, json={})
        assert blocked.status_code == 409

        mismatch = client.post(
            f"/api/v1/assets/{session['asset_id']}/remote-upload-complete",
            headers=headers,
            json={"size_bytes": 3},
        )
        assert mismatch.status_code == 422

        completed = client.post(
            f"/api/v1/assets/{session['asset_id']}/remote-upload-complete",
            headers=headers,
            json={"size_bytes": 4, "etag": "etag-1"},
        )
        assert completed.status_code == 200
        assert completed.json()["metadata"]["storage"]["status"] == "ready"

        analysis = client.post(f"/api/v1/assets/{session['asset_id']}/analyze", headers=headers, json={})
        assert analysis.status_code == 201


def test_remote_upload_rejects_local_uri_and_missing_presigned_url(tmp_path: Path):
    with make_client(tmp_path) as client:
        headers, project = _tenant_project(client)
        invalid_uri = client.post(
            "/api/v1/assets/register-remote",
            headers=headers,
            json={"name": "clip.mp4", "kind": "video/mp4", "uri": "file:///tmp/clip.mp4", "project_id": project["id"]},
        )
        assert invalid_uri.status_code == 422
        missing_url = client.post(
            "/api/v1/assets/remote-upload-session",
            headers=headers,
            json={
                "name": "clip.mp4",
                "content_type": "video/mp4",
                "size_bytes": 4,
                "object_uri": "s3://video-inputs/clip.mp4",
                "project_id": project["id"],
            },
        )
        assert missing_url.status_code == 422

        insecure_uri = client.post(
            "/api/v1/assets/register-remote",
            headers=headers,
            json={"name": "clip.mp4", "kind": "video/mp4", "uri": "http://cdn.example.test/clip.mp4", "project_id": project["id"]},
        )
        assert insecure_uri.status_code == 422

        private_upload = client.post(
            "/api/v1/assets/remote-upload-session",
            headers=headers,
            json={
                "name": "clip.mp4",
                "content_type": "video/mp4",
                "size_bytes": 4,
                "object_uri": "s3://video-inputs/clip.mp4",
                "upload_url": "https://127.0.0.1/upload",
                "project_id": project["id"],
            },
        )
        assert private_upload.status_code == 422


def test_register_only_remote_session_is_ready(tmp_path: Path):
    with make_client(tmp_path) as client:
        headers, _ = _tenant_project(client)
        response = client.post(
            "/api/v1/assets/remote-upload-session",
            headers=headers,
            json={
                "name": "voice.wav",
                "content_type": "audio/wav",
                "size_bytes": 12,
                "object_uri": "gs://studio-inputs/voice.wav",
                "register_only": True,
            },
        )
        assert response.status_code == 201
        assert response.json()["status"] == "ready"
        assert response.json()["upload_url"] is None


def _proof(*, asset_id: str, object_uri: str, size_bytes: int, sha256: str | None = None, expires_at: datetime | None = None, nonce: str = "nonce-1234567890") -> dict:
    expiry = expires_at or (datetime.now(timezone.utc) + timedelta(minutes=5))
    return {
        "expires_at": expiry.isoformat(),
        "nonce": nonce,
        "signature": sign_attestation(
            "storage-test-secret",
            asset_id=asset_id,
            object_uri=object_uri,
            size_bytes=size_bytes,
            sha256=sha256,
            expires_at=expiry,
            nonce=nonce,
        ),
    }


def test_configured_storage_attestation_is_required_and_asset_bound(tmp_path: Path):
    previous = routers.settings.storage_attestation_secret
    routers.settings.storage_attestation_secret = "storage-test-secret"
    try:
        with make_client(tmp_path) as client:
            headers, project = _tenant_project(client)
            session = client.post(
                "/api/v1/assets/remote-upload-session",
                headers=headers,
                json={
                    "name": "clip.mp4",
                    "content_type": "video/mp4",
                    "size_bytes": 4,
                    "object_uri": "s3://video-inputs/clip.mp4",
                    "upload_url": "https://storage.example.test/presigned?signature=redacted",
                    "project_id": project["id"],
                },
            ).json()
            complete_uri = f"/api/v1/assets/{session['asset_id']}/remote-upload-complete"
            missing = client.post(complete_uri, headers=headers, json={"size_bytes": 4})
            assert missing.status_code == 401

            proof = _proof(asset_id=session["asset_id"], object_uri=session["object_uri"], size_bytes=4)
            completed = client.post(complete_uri, headers=headers, json={"size_bytes": 4, "attestation": proof})
            assert completed.status_code == 200
            assert completed.json()["metadata"]["storage"]["attestation"]["nonce"] == proof["nonce"]

            # An exact retry is safe, while a newly minted proof cannot replay
            # the completion state for this asset.
            retry = client.post(complete_uri, headers=headers, json={"size_bytes": 4, "attestation": proof})
            assert retry.status_code == 200
            replay = client.post(
                complete_uri,
                headers=headers,
                json={"size_bytes": 4, "attestation": _proof(asset_id=session["asset_id"], object_uri=session["object_uri"], size_bytes=4, nonce="nonce-new-123456")},
            )
            assert replay.status_code == 409

            # The signature is bound to asset_id and cannot be moved to a
            # different upload session.
            other = client.post(
                "/api/v1/assets/remote-upload-session",
                headers=headers,
                json={
                    "name": "other.mp4",
                    "content_type": "video/mp4",
                    "size_bytes": 4,
                    "object_uri": "s3://video-inputs/other.mp4",
                    "upload_url": "https://storage.example.test/presigned?signature=other",
                    "project_id": project["id"],
                },
            ).json()
            wrong_asset = client.post(
                f"/api/v1/assets/{other['asset_id']}/remote-upload-complete",
                headers=headers,
                json={"size_bytes": 4, "attestation": proof},
            )
            assert wrong_asset.status_code == 401
    finally:
        routers.settings.storage_attestation_secret = previous


def test_configured_storage_attestation_covers_register_only_and_register_remote(tmp_path: Path):
    previous = routers.settings.storage_attestation_secret
    routers.settings.storage_attestation_secret = "storage-test-secret"
    try:
        with make_client(tmp_path) as client:
            headers, project = _tenant_project(client)
            requested_id = f"asset-{uuid4().hex}"
            bypass = client.post(
                "/api/v1/assets/remote-upload-session",
                headers=headers,
                json={
                    "name": "voice.wav",
                    "content_type": "audio/wav",
                    "size_bytes": 12,
                    "object_uri": "gs://studio-inputs/voice.wav",
                    "register_only": True,
                    "asset_id": requested_id,
                    "project_id": project["id"],
                },
            )
            assert bypass.status_code == 401
            proof = _proof(asset_id=requested_id, object_uri="gs://studio-inputs/voice.wav", size_bytes=12)
            attested = client.post(
                "/api/v1/assets/remote-upload-session",
                headers=headers,
                json={
                    "name": "voice.wav",
                    "content_type": "audio/wav",
                    "size_bytes": 12,
                    "object_uri": "gs://studio-inputs/voice.wav",
                    "register_only": True,
                    "asset_id": requested_id,
                    "attestation": proof,
                    "project_id": project["id"],
                },
            )
            assert attested.status_code == 201
            assert attested.json()["status"] == "ready"
            assert attested.json()["upload_url"] is None

            # Direct registration accepts a caller-generated asset id so the
            # gateway can bind its proof before the record exists.
            registered = client.post(
                "/api/v1/assets/register-remote",
                headers=headers,
                json={
                    "name": "voice.wav",
                    "kind": "audio/wav",
                    "uri": "gs://studio-inputs/voice.wav",
                    "size_bytes": 12,
                    "asset_id": requested_id,
                    "project_id": project["id"],
                },
            )
            assert registered.status_code == 401
            registered_id = f"asset-{uuid4().hex}"
            registered_uri = "gs://studio-inputs/registered.wav"
            registered_proof = _proof(asset_id=registered_id, object_uri=registered_uri, size_bytes=12)
            registered = client.post(
                "/api/v1/assets/register-remote",
                headers=headers,
                json={
                    "name": "registered.wav",
                    "kind": "audio/wav",
                    "uri": registered_uri,
                    "size_bytes": 12,
                    "asset_id": registered_id,
                    "attestation": registered_proof,
                    "project_id": project["id"],
                },
            )
            assert registered.status_code == 201
            assert registered.json()["id"] == registered_id
    finally:
        routers.settings.storage_attestation_secret = previous


def test_configured_storage_attestation_rejects_expired_proof(tmp_path: Path):
    previous = routers.settings.storage_attestation_secret
    routers.settings.storage_attestation_secret = "storage-test-secret"
    try:
        with make_client(tmp_path) as client:
            headers, project = _tenant_project(client)
            session = client.post(
                "/api/v1/assets/remote-upload-session",
                headers=headers,
                json={
                    "name": "clip.mp4",
                    "content_type": "video/mp4",
                    "size_bytes": 4,
                    "object_uri": "s3://video-inputs/expired.mp4",
                    "upload_url": "https://storage.example.test/presigned?signature=expired",
                    "project_id": project["id"],
                },
            ).json()
            expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
            response = client.post(
                f"/api/v1/assets/{session['asset_id']}/remote-upload-complete",
                headers=headers,
                json={"size_bytes": 4, "attestation": _proof(asset_id=session["asset_id"], object_uri=session["object_uri"], size_bytes=4, expires_at=expiry)},
            )
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "attestation_expired"
    finally:
        routers.settings.storage_attestation_secret = previous

