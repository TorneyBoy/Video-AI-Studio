from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base, get_db
from app.main import app
from app.services.worker import process_queued_auto_edit_jobs


@pytest.fixture(autouse=True)
def local_planner_fixtures(monkeypatch):
    # These tests inject local/preanalysed fixtures. Production remote upload
    # and attestation are covered by the external ingestion/boundary suites.
    from app.api import routers
    monkeypatch.setattr(routers.settings, "asset_storage_mode", "local")


def _template_content() -> dict:
    return {
        "schema_version": "1.0",
        "id": "social-recap",
        "version": "1",
        "duration": 2.0,
        "fps": 30,
        "slots": [
            {
                "name": "hook",
                "type": "video",
                "constraints": {"allowed_asset_types": ["video"], "min_items": 1, "max_items": 1},
            },
            {
                "name": "body",
                "type": "video",
                "constraints": {"allowed_asset_types": ["video"], "min_items": 1, "max_items": 1},
            },
        ],
    }


def _make_client(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / (uuid4().hex + '.db')}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def override_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return engine, factory, TestClient(app)


def test_auto_edit_worker_compiles_timeline_and_preview_export_are_idempotent(tmp_path: Path):
    engine, factory, client = _make_client(tmp_path)
    try:
        with client:
            tenant = client.post("/api/v1/tenants", json={"slug": "flow", "name": "Flow"}).json()
            headers = {"X-Tenant-ID": tenant["id"]}
            project = client.post("/api/v1/projects", headers=headers, json={"name": "Launch"}).json()
            template = client.post(
                "/api/v1/templates",
                headers=headers,
                json={"name": "Recap", "version": "1", "project_id": project["id"], "content": _template_content()},
            ).json()
            for name, quality in (("a.mp4", 0.9), ("b.mp4", 0.8)):
                created = client.post(
                    "/api/v1/assets",
                    headers=headers,
                    json={
                        "name": name,
                        "kind": "video",
                        "uri": f"file:///{name}",
                        "project_id": project["id"],
                        "metadata": {"duration": 1.0, "analysis": {"shots": [{"duration": 1.0, "quality": quality, "start": 0, "end": 1.0}]}},
                    },
                )
                assert created.status_code == 201

            create_job = client.post(
                "/api/v1/jobs/auto-edit",
                headers={**headers, "Idempotency-Key": "auto-once"},
                json={"project_id": project["id"], "template_id": template["id"], "quality": "draft"},
            )
            assert create_job.status_code == 201
            job = create_job.json()
            replay = client.post(
                "/api/v1/jobs/auto-edit",
                headers={**headers, "Idempotency-Key": "auto-once"},
                json={"project_id": project["id"], "template_id": template["id"]},
            )
            assert replay.status_code == 201
            assert replay.json()["id"] == job["id"]

            with factory() as db:
                processed = process_queued_auto_edit_jobs(db)
                assert len(processed) == 1
                assert processed[0]["job_id"] == job["id"]
                assert processed[0]["status"] == "succeeded", processed
                db_job = db.get(models.Job, job["id"])
                assert db_job is not None
                timeline_id = db_job.payload["timeline_id"]
                assert db_job.status == "succeeded"

            timeline = client.get(f"/api/v1/jobs/{job['id']}/timeline", headers=headers)
            assert timeline.status_code == 200
            assert timeline.json()["id"] == timeline_id
            assert len(timeline.json()["timeline"]["tracks"][0]["clips"]) == 2

            preview = client.post(
                f"/api/v1/jobs/{job['id']}/preview",
                headers={**headers, "Idempotency-Key": "preview-once"},
            )
            assert preview.status_code == 201
            assert preview.json()["job"]["type"] == "render.draft"
            preview_replay = client.post(
                f"/api/v1/jobs/{job['id']}/preview",
                headers={**headers, "Idempotency-Key": "preview-once"},
            )
            assert preview_replay.status_code == 201
            assert preview_replay.json()["job"]["id"] == preview.json()["job"]["id"]

            export = client.post(
                f"/api/v1/jobs/{job['id']}/export",
                headers={**headers, "Idempotency-Key": "export-once"},
                json={"quality": "standard"},
            )
            assert export.status_code == 201
            assert export.json()["job"]["type"] == "render.standard"
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_auto_edit_waits_for_analysis_and_replans_from_committed_shots(tmp_path: Path):
    engine, factory, client = _make_client(tmp_path)
    try:
        with client:
            tenant = client.post("/api/v1/tenants", json={"slug": "deferred", "name": "Deferred"}).json()
            headers = {"X-Tenant-ID": tenant["id"]}
            project = client.post("/api/v1/projects", headers=headers, json={"name": "Deferred"}).json()
            template = client.post(
                "/api/v1/templates",
                headers=headers,
                json={"name": "Recap", "version": "1", "project_id": project["id"], "content": _template_content()},
            ).json()
            asset = client.post(
                "/api/v1/assets",
                headers=headers,
                json={"name": "late.mp4", "kind": "video", "uri": "file:///late.mp4", "project_id": project["id"]},
            ).json()
            created = client.post(
                "/api/v1/jobs/auto-edit",
                headers={**headers, "Idempotency-Key": "deferred-auto"},
                json={"project_id": project["id"], "template_id": template["id"], "asset_ids": [asset["id"]]},
            )
            assert created.status_code == 201
            job = created.json()

            with factory() as db:
                assert process_queued_auto_edit_jobs(db) == []
                db_job = db.get(models.Job, job["id"])
                assert db_job is not None and db_job.status == "queued"
                analysis_id = db_job.payload["analysis_job_ids"][0]
                analysis_job = db.get(models.Job, analysis_id)
                source = db.get(models.Asset, asset["id"])
                assert analysis_job is not None and source is not None
                source.metadata_json = {"duration": 1.0, "analysis": {"shots": [{"duration": 1.0, "quality": 0.95, "start": 0.0, "end": 1.0}]}}
                analysis_job.status = "succeeded"
                db.commit()

                processed = process_queued_auto_edit_jobs(db)
                assert processed[0]["status"] == "succeeded", processed
                db.refresh(db_job)
                assert db_job.payload["plan"]["selected"]
                assert db_job.payload["compiled"] is True
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()
