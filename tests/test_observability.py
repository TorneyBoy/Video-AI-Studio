from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base, get_db
from app.main import app


def test_job_stats_are_tenant_scoped_and_metrics_are_aggregate(tmp_path: Path):
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
            first = client.post("/api/v1/tenants", json={"slug": "metrics-one", "name": "One"}).json()
            second = client.post("/api/v1/tenants", json={"slug": "metrics-two", "name": "Two"}).json()
            one_headers = {"X-Tenant-ID": first["id"]}
            two_headers = {"X-Tenant-ID": second["id"]}
            created = client.post("/api/v1/jobs", headers=one_headers, json={"type": "render.standard"})
            assert created.status_code == 201
            with factory() as db:
                db.add(
                    models.Job(
                        tenant_id=first["id"],
                        type="avatar.lip_sync",
                        status="failed",
                        failure_code="consent_inactive",
                        retryable=True,
                        policy_blocked=True,
                    )
                )
                db.add(models.Job(tenant_id=second["id"], type="private.other", status="queued"))
                db.commit()

            stats = client.get("/api/v1/jobs/stats", headers=one_headers)
            assert stats.status_code == 200
            body = stats.json()
            assert body["total"] == 2
            assert body["by_status"] == {"failed": 1, "queued": 1}
            assert body["by_type"] == {"avatar.lip_sync": 1, "render.standard": 1}
            assert body["by_failure_code"] == {"consent_inactive": 1}
            assert body["retryable"] == 1
            assert body["policy_blocked"] == 1

            other = client.get("/api/v1/jobs/stats", headers=two_headers).json()
            assert other["total"] == 1
            assert other["by_type"] == {"private.other": 1}

            metrics = client.get("/metrics")
            assert metrics.status_code == 200
            assert "video_ai_jobs{status=\"failed\"} 1" in metrics.text
            assert '# TYPE video_ai_jobs gauge' in metrics.text and 'video_ai_jobs_total' not in metrics.text
            assert metrics.headers['Content-Type']=='text/plain; version=0.0.4; charset=utf-8'
            assert "video_ai_jobs_retryable 1" in metrics.text
            # The global scrape intentionally carries no tenant identifiers.
            assert first["id"] not in metrics.text
            assert second["id"] not in metrics.text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
