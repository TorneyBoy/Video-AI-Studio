from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app


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


def _tenant_and_project(client: TestClient):
    tenant = client.post("/api/v1/tenants", json={"slug": f"t-{uuid4().hex[:8]}", "name": "Test"}).json()
    headers = {"X-Tenant-ID": tenant["id"]}
    project = client.post("/api/v1/projects", headers=headers, json={"name": "Workflow"}).json()
    return headers, project


def test_upload_session_and_content(tmp_path: Path, monkeypatch):
    from app.api import routers
    monkeypatch.setattr(routers.settings, "asset_storage_mode", "local")
    with make_client(tmp_path) as client:
        headers, project = _tenant_and_project(client)
        session = client.post(
            "/api/v1/assets/upload-session",
            headers=headers,
            json={"name": "clip.mp4", "content_type": "video/mp4", "size_bytes": 4, "project_id": project["id"]},
        )
        assert session.status_code == 201
        asset_id = session.json()["asset_id"]
        uploaded = client.post(
            f"/api/v1/assets/{asset_id}/content",
            headers=headers,
            files={"file": ("clip.mp4", b"test", "video/mp4")},
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["metadata"]["upload"]["status"] == "ready"
        analysis_headers = {**headers, "Idempotency-Key": "analysis-once"}
        analysis = client.post(
            f"/api/v1/assets/{asset_id}/analyze",
            headers=analysis_headers,
            json={"idempotency_key": "analysis-once", "max_attempts": 4},
        )
        assert analysis.status_code == 201
        assert analysis.json()["max_attempts"] == 4
        replay = client.post(f"/api/v1/assets/{asset_id}/analyze", headers=analysis_headers, json={"idempotency_key": "analysis-once"})
        assert replay.status_code == 201
        assert replay.json()["id"] == analysis.json()["id"]


def test_timeline_validate_and_render_manifest(tmp_path: Path):
    with make_client(tmp_path) as client:
        headers, project = _tenant_and_project(client)
        timeline = {
            "schema_version": "1.0",
            "id": "timeline-demo",
            "fps": 30,
            "duration": 2,
            "resolution": {"width": 1080, "height": 1920},
            "assets": [{"id": "a1", "type": "video", "uri": "file:///clip.mp4"}],
            "tracks": [{
                "id": "video-1",
                "type": "video",
                "clips": [{"id": "clip-1", "asset_id": "a1", "range": {"start": 0, "end": 2}}],
            }],
        }
        created = client.post(
            "/api/v1/timelines",
            headers=headers,
            json={"name": "Timeline", "project_id": project["id"], "fps": 30, "duration_ms": 2000, "timeline": timeline},
        )
        assert created.status_code == 201
        timeline_id = created.json()["id"]
        validation = client.post(f"/api/v1/timelines/{timeline_id}/validate", headers=headers)
        assert validation.status_code == 200
        assert validation.json()["valid"] is True
        render_headers = {**headers, "Idempotency-Key": "render-once"}
        render = client.post(
            f"/api/v1/timelines/{timeline_id}/render",
            headers=render_headers,
            json={"quality": "draft", "max_attempts": 2},
        )
        assert render.status_code == 201
        assert render.json()["job"]["type"] == "render.draft"
        assert render.json()["job"]["max_attempts"] == 2
        assert render.json()["manifest"]["content_hash"]
        replay = client.post(f"/api/v1/timelines/{timeline_id}/render", headers=render_headers, json={"quality": "draft"})
        assert replay.status_code == 201
        assert replay.json()["job"]["id"] == render.json()["job"]["id"]


def test_identity_profiles_require_active_scoped_consent(tmp_path: Path):
    with make_client(tmp_path) as client:
        headers, project = _tenant_and_project(client)
        denied = client.post(
            "/api/v1/consents",
            headers=headers,
            json={"subject_id": "model-1", "scope": "voice", "granted": False, "project_id": project["id"]},
        ).json()
        blocked = client.post(
            "/api/v1/voice-profiles",
            headers=headers,
            json={"name": "Denied", "consent_id": denied["id"], "project_id": project["id"]},
        )
        assert blocked.status_code == 422

        consent = client.post(
            "/api/v1/consents",
            headers=headers,
            json={"subject_id": "model-1", "scope": "voice face avatar", "granted": True, "project_id": project["id"]},
        ).json()
        voice = client.post(
            "/api/v1/voice-profiles",
            headers=headers,
            json={"name": "Model voice", "consent_id": consent["id"], "project_id": project["id"], "provider": "openvoice"},
        )
        assert voice.status_code == 201
        revoked = client.post(f"/api/v1/voice-profiles/{voice.json()['id']}/revoke", headers=headers)
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"
        consent_revoked = client.post(f"/api/v1/consents/{consent['id']}/revoke", headers=headers)
        assert consent_revoked.status_code == 200
        assert consent_revoked.json()["granted"] is False


def test_specialized_jobs_enforce_identity_and_rights_policy(tmp_path: Path, monkeypatch):
    from app.api import routers
    monkeypatch.setattr(routers.settings, "asset_storage_mode", "local")
    with make_client(tmp_path) as client:
        headers, project = _tenant_and_project(client)
        source = client.post(
            "/api/v1/assets",
            headers=headers,
            json={"name": "source.mp4", "kind": "video/mp4", "uri": "file:///source.mp4", "project_id": project["id"]},
        ).json()
        consent = client.post(
            "/api/v1/consents",
            headers=headers,
            json={"subject_id": "model-2", "scope": "voice face avatar", "granted": True, "project_id": project["id"]},
        ).json()
        voice = client.post(
            "/api/v1/voice-profiles",
            headers=headers,
            json={"name": "Voice", "consent_id": consent["id"], "project_id": project["id"], "provider": "cosyvoice"},
        ).json()
        avatar = client.post(
            "/api/v1/avatar-profiles",
            headers=headers,
            json={"name": "Avatar", "consent_id": consent["id"], "project_id": project["id"], "provider": "liveportrait"},
        ).json()
        voice_headers = {**headers, "Idempotency-Key": "voice-once"}
        voice_job = client.post(
            "/api/v1/jobs/voice",
            headers=voice_headers,
            json={"project_id": project["id"], "voice_profile_id": voice["id"], "text": "你好，世界"},
        )
        assert voice_job.status_code == 201
        voice_replay = client.post(
            "/api/v1/jobs/voice",
            headers=voice_headers,
            json={"project_id": project["id"], "voice_profile_id": voice["id"], "text": "你好，世界"},
        )
        assert voice_replay.status_code == 201
        assert voice_replay.json()["id"] == voice_job.json()["id"]
        avatar_job = client.post(
            "/api/v1/jobs/avatar",
            headers=headers,
            json={"project_id": project["id"], "avatar_profile_id": avatar["id"], "source_asset_id": source["id"], "mode": "motion_transfer"},
        )
        assert avatar_job.status_code == 201
        blocked = client.post(
            "/api/v1/jobs/enhancement",
            headers=headers,
            json={"project_id": project["id"], "asset_id": source["id"], "operations": ["authorized_object_cleanup"]},
        )
        assert blocked.status_code == 422
        allowed = client.post(
            "/api/v1/jobs/enhancement",
            headers=headers,
            json={"project_id": project["id"], "asset_id": source["id"], "operations": ["denoise"], "rights_confirmed": True},
        )
        assert allowed.status_code == 201

