import json
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import models
from app.database import Base
from app.services.media_analysis import MediaAnalysisProvider
from app.services.worker import process_queued_analysis_jobs


def test_ffprobe_provider_normalizes_streams(tmp_path: Path, monkeypatch):
    payload = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.5", "size": "1234"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "30/1"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
        ],
    }
    def probe(command, **kwargs):
        assert command[:5] == ["ffprobe-fixture", "-v", "error", "-print_format", "json"]
        assert command[-1] == "file:///tmp/clip.mp4"
        assert kwargs["timeout"] > 0
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr("app.services.media_analysis.subprocess.run", probe)

    result = MediaAnalysisProvider(ffprobe_bin="ffprobe-fixture").analyze("file:///tmp/clip.mp4")

    assert result["media"]["status"] == "ok"
    assert result["media"]["duration_seconds"] == 2.5
    assert result["video"]["width"] == 1920
    assert result["audio"]["sample_rate"] == 48000.0
    assert len(result["streams"]) == 2


def test_analysis_worker_persists_metadata_and_artifact(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / (uuid4().hex + '.db')}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = models.Tenant(id=str(uuid4()), slug="analysis", name="Analysis")
        db.add(tenant)
        db.flush()
        project = models.Project(tenant_id=tenant.id, name="Demo")
        db.add(project)
        db.flush()
        asset = models.Asset(tenant_id=tenant.id, project_id=project.id, name="clip", kind="video", uri="file:///clip.mp4")
        db.add(asset)
        db.flush()
        job = models.Job(tenant_id=tenant.id, project_id=project.id, type="asset.analyze", payload={"asset_id": asset.id, "options": {}})
        db.add(job)
        db.commit()

        class FakeAnalyzer:
            def analyze(self, uri: str, *, options: dict):
                assert uri == "file:///clip.mp4"
                return {"schema_version": "1", "media": {"duration_seconds": 3.0}, "shots": []}

        result = process_queued_analysis_jobs(db, provider=FakeAnalyzer())
        db.refresh(job)
        db.refresh(asset)
        artifact = db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id))

        assert result[0]["status"] == "succeeded"
        assert job.status == "succeeded"
        assert job.started_at is not None and job.completed_at is not None
        assert asset.metadata_json["analysis"]["media"]["duration_seconds"] == 3.0
        assert asset.metadata_json["duration"] == 3.0
        assert artifact is not None and artifact.kind == "analysis"
