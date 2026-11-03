from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app


def test_job_idempotency_steps_and_state_machine(tmp_path: Path):
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
    try:
        with TestClient(app) as client:
            tenant = client.post("/api/v1/tenants", json={"slug": "jobs", "name": "Jobs"}).json()
            headers = {"X-Tenant-ID": tenant["id"], "Idempotency-Key": "request-1"}
            first = client.post("/api/v1/jobs", headers=headers, json={"type": "demo", "steps": [{"name": "encode"}]})
            second = client.post("/api/v1/jobs", headers=headers, json={"type": "demo"})
            assert first.status_code == 201
            assert second.status_code == 201
            assert second.json()["id"] == first.json()["id"]

            job_id = first.json()["id"]
            steps = client.get(f"/api/v1/jobs/{job_id}/steps", headers=headers).json()
            assert len(steps) == 1
            step_id = steps[0]["id"]
            updated = client.patch(f"/api/v1/jobs/{job_id}/steps/{step_id}", headers=headers, json={"status": "running"})
            assert updated.status_code == 200
            assert updated.json()["started_at"] is not None
            running = client.patch(f"/api/v1/jobs/{job_id}/status", headers=headers, json={"status": "running"})
            assert running.status_code == 200
            assert running.json()["started_at"] is not None
            cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["completed_at"] is not None
            assert client.patch(f"/api/v1/jobs/{job_id}/status", headers=headers, json={"status": "queued"}).status_code == 409
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

