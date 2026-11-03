import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.database import Base
from app.services.providers import ModelSnapshot, ProviderCapability, ProviderRequest, ProviderResult, ProviderRouter
from app.services.worker import process_queued_analysis_jobs, process_queued_provider_jobs, process_queued_render_jobs
from timeline_engine import AssetRef, AssetType, Clip, TimeRange, Timeline, Track, TrackType, create_manifest


class ExternalMediaProvider:
    name = "remote-media"
    capabilities = (
        ProviderCapability("media_analysis", ("media",), ("analysis/json",), quality_score=1.0, commercial_allowed=True),
        ProviderCapability("video_editing", ("json",), ("video",), quality_score=1.0, commercial_allowed=True),
        ProviderCapability("video_generation", ("json",), ("video",), quality_score=1.0, commercial_allowed=True),
    )

    def __init__(self):
        self.calls: list[ProviderRequest] = []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request)
        model = ModelSnapshot(provider=self.name, model_id="remote-v1", version="2026-10")
        if request.capability == "media_analysis":
            return ProviderResult(
                artifact_uri="https://cdn.example/analysis.json",
                artifact_type="analysis",
                model=model,
                provenance={
                    "analysis": {
                        "schema_version": "1",
                        "media": {"duration_seconds": 2.0},
                        "shots": [{"start": 0.0, "end": 2.0}],
                    }
                },
            )
        return ProviderResult(
            artifact_uri="https://cdn.example/render.mp4",
            artifact_type="video.rendered",
            model=model,
        )


def _database(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / (uuid4().hex + '.db')}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_api_only_analysis_uses_external_provider_and_persists_remote_artifact(tmp_path: Path):
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"remote-analysis-{uuid4().hex[:8]}", name="Remote")
            db.add(tenant)
            db.flush()
            project = models.Project(tenant_id=tenant.id, name="Remote project")
            db.add(project)
            db.flush()
            asset = models.Asset(tenant_id=tenant.id, project_id=project.id, name="clip", kind="video", uri="https://cdn.example/clip.mp4")
            db.add(asset)
            db.flush()
            job = models.Job(tenant_id=tenant.id, project_id=project.id, type="asset.analyze", payload={"asset_id": asset.id, "options": {}})
            db.add(job)
            db.commit()
            provider = ExternalMediaProvider()

            result = process_queued_analysis_jobs(db, router=ProviderRouter([provider]))

            db.refresh(asset)
            db.refresh(job)
            artifact = db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id))
            assert result[0]["status"] == "succeeded"
            assert job.status == "succeeded"
            assert provider.calls[0].capability == "media_analysis"
            assert asset.metadata_json["analysis"]["shots"]
            assert artifact is not None
            assert artifact.uri == "https://cdn.example/analysis.json"
            assert artifact.metadata_json["remote"] is True
    finally:
        engine.dispose()


def test_api_only_render_uses_external_provider_without_local_output(tmp_path: Path):
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"remote-render-{uuid4().hex[:8]}", name="Remote")
            db.add(tenant)
            db.flush()
            project = models.Project(tenant_id=tenant.id, name="Remote project")
            db.add(project)
            db.flush()
            timeline = Timeline(
                id="remote-timeline",
                fps=30,
                duration=2,
                resolution={"width": 1080, "height": 1920},
                assets=[AssetRef(id="a1", type=AssetType.VIDEO, uri="https://cdn.example/clip.mp4")],
                tracks=[Track(id="video", type=TrackType.VIDEO, clips=[Clip(id="c1", asset_id="a1", range=TimeRange(start=0, end=2))])],
            )
            manifest = create_manifest(timeline, options={"quality": "draft"})
            job = models.Job(
                tenant_id=tenant.id,
                project_id=project.id,
                type="render.draft",
                payload={"manifest": manifest.model_dump(mode="json")},
            )
            db.add(job)
            db.commit()
            provider = ExternalMediaProvider()

            result = process_queued_render_jobs(db, router=ProviderRouter([provider]))

            db.refresh(job)
            artifact = db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id))
            assert result[0]["status"] == "succeeded"
            assert job.status == "succeeded"
            assert provider.calls[0].capability == "video_editing"
            assert artifact is not None
            assert artifact.uri == "https://cdn.example/render.mp4"
            assert artifact.metadata_json["remote"] is True
    finally:
        engine.dispose()


def test_api_only_analysis_rejects_worker_local_asset_uri(tmp_path: Path):
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"remote-uri-{uuid4().hex[:8]}", name="Remote")
            db.add(tenant)
            db.flush()
            project = models.Project(tenant_id=tenant.id, name="Remote project")
            db.add(project)
            db.flush()
            asset = models.Asset(tenant_id=tenant.id, project_id=project.id, name="clip", kind="video", uri="/tmp/clip.mp4")
            db.add(asset)
            db.flush()
            job = models.Job(tenant_id=tenant.id, project_id=project.id, type="asset.analyze", payload={"asset_id": asset.id, "options": {}})
            db.add(job)
            db.commit()
            provider = ExternalMediaProvider()

            result = process_queued_analysis_jobs(db, router=ProviderRouter([provider]))

            db.refresh(job)
            assert result[0]["status"] == "failed"
            assert json.loads(result[0]["error"])["code"] == "remote_asset_uri_required"
            assert job.failure_code == "remote_asset_uri_required"
            assert provider.calls == []
    finally:
        engine.dispose()


def test_api_only_provider_jobs_send_remote_asset_uris(tmp_path: Path):
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            tenant = models.Tenant(slug=f"remote-provider-{uuid4().hex[:8]}", name="Remote")
            db.add(tenant)
            db.flush()
            project = models.Project(tenant_id=tenant.id, name="Remote project")
            db.add(project)
            db.flush()
            asset = models.Asset(tenant_id=tenant.id, project_id=project.id, name="reference", kind="video", uri="s3://bucket/reference.mp4")
            db.add(asset)
            db.flush()
            job = models.Job(
                tenant_id=tenant.id,
                project_id=project.id,
                type="video.generate",
                payload={"provider_request": {"capability": "video_generation", "asset_id": asset.id, "prompt": "cinematic"}},
            )
            db.add(job)
            db.commit()
            provider = ExternalMediaProvider()

            result = process_queued_provider_jobs(db, router=ProviderRouter([provider]))

            assert result[0]["status"] == "succeeded"
            assert provider.calls[0].inputs["asset_uri"] == "s3://bucket/reference.mp4"
            assert "input_path" not in provider.calls[0].inputs
    finally:
        engine.dispose()
