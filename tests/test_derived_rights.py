"""Revocation follows adopted media into later renders and signed delivery."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from timeline_engine import Timeline, create_manifest

from app import models, schemas
from app.database import Base
from app.services.artifact_delivery import artifact_access
from app.services.derived_rights import assert_asset_rights, assert_job_rights, assert_timeline_rights, asset_playback
from app.services.editing_errors import EditingError
from app.services.preparation_quotes import source_hash
from app.services.storage_gateway import StorageGatewayError


@pytest.fixture
def rights_case(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rights.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add(models.Tenant(id="tenant", slug="rights", name="Rights"))
        db.add(models.Project(id="project", tenant_id="tenant", name="Project"))
        db.flush()
        def asset(id, kind):
            return models.Asset(id=id, tenant_id="tenant", project_id="project", name=id, kind=kind,
                uri=f"oss://bucket/tenants/tenant/{id}", metadata_json={"storage": {"mode": "external", "status": "ready",
                    "sha256": "a" * 64, "size_bytes": 100}, "duration": 5})
        source, reference = asset("source", "video/mp4"), asset("reference", "image/png")
        consent = models.Consent(id="consent", tenant_id="tenant", project_id="project", subject_id="person",
                                 scope="person_replace", granted=True)
        profile = models.AvatarProfile(id="profile", tenant_id="tenant", project_id="project", name="Person",
            consent_id=consent.id, reference_asset_id=reference.id, status="ready", scopes=["person_replace"])
        db.add_all([source, reference, consent, profile]); db.flush()
        identity = {"profile_id": profile.id, "consent_id": consent.id, "reference_asset_id": reference.id, "source_duration": 5}
        inputs = {source.id: source_hash(source), reference.id: source_hash(reference)}
        produced = asset("produced", "video/mp4")
        origin = models.Job(id="origin", tenant_id="tenant", project_id="project", type="avatar.person_replace", status="succeeded",
            payload={"ai_production": {"request": {"operation": "person_replace"},
                     "snapshot": {"identity": identity, "sources": inputs}, "adopted_asset_id": produced.id}})
        origin_artifact = models.Artifact(id="origin-output", tenant_id="tenant", project_id="project", job_id=origin.id,
                                         uri=produced.uri, kind="video")
        produced.metadata_json = {**produced.metadata_json, "generated_media": {"status": "ok"},
            "provenance": {"operation": "person_replace", "identity": deepcopy(identity), "input_sources": deepcopy(inputs),
                           "job_id": origin.id, "artifact_id": origin_artifact.id}}
        db.add_all([origin, origin_artifact, produced]); db.commit()
        timeline = Timeline.model_validate({"id": "timeline", "fps": 30, "duration": 5, "resolution": {"width": 720, "height": 1280},
            "assets": [{"id": produced.id, "type": "video", "uri": produced.uri}],
            "tracks": [{"id": "video", "type": "video", "clips": [{"id": "clip", "asset_id": produced.id,
                         "range": {"start": 0, "end": 5}, "source_end": 5}]}]})
        manifest = create_manifest(timeline)
        frozen = assert_timeline_rights(db, tenant_id="tenant", project_id="project", timeline=timeline)
        render = models.Job(id="render", tenant_id="tenant", project_id="project", type="render.draft", status="succeeded",
                            payload={"manifest": manifest.model_dump(mode="json"), "derived_rights": frozen})
        artifact = models.Artifact(id="final", tenant_id="tenant", project_id="project", job_id=render.id, kind="render",
            uri="oss://bucket/tenants/tenant/final", metadata_json={"quality_control": {"status": "passed", "output_sha256": "b" * 64, "size_bytes": 123}})
        archive = models.ArtifactStorage(artifact_id=artifact.id, tenant_id="tenant", object_uri=artifact.uri,
            sha256="b" * 64, size_bytes=123, expires_at=datetime.now(timezone.utc) + timedelta(days=1), evidence_json={})
        db.add_all([render, artifact, archive]); db.commit()
        yield SimpleNamespace(db=db, factory=factory, source=source, reference=reference, profile=profile, consent=consent,
                              produced=produced, origin=origin, origin_artifact=origin_artifact,
                              render=render, artifact=artifact, timeline=timeline, frozen=frozen, root=tmp_path)
    engine.dispose()


@pytest.mark.parametrize("change", ["profile", "consent", "profile_expired", "consent_expired", "profile_scope", "consent_scope",
                                   "profile_tenant", "profile_project", "consent_project", "reference", "source", "usage", "output"])
def test_derived_rights_recheck_live_authorization_and_frozen_inputs(rights_case, change):
    ctx = rights_case
    assert assert_job_rights(ctx.db, ctx.render) == ctx.frozen
    if change == "profile": ctx.profile.revoked_at = datetime.now(timezone.utc)
    elif change == "consent": ctx.consent.granted = False
    elif change == "profile_expired": ctx.profile.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change == "consent_expired": ctx.consent.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change == "profile_scope": ctx.profile.scopes = ["lip_sync"]
    elif change == "consent_scope": ctx.consent.scope = "voice_clone"
    elif change == "profile_tenant": ctx.profile.tenant_id = "other"
    elif change == "profile_project": ctx.profile.project_id = "other"
    elif change == "consent_project": ctx.consent.project_id = "other"
    elif change == "reference": ctx.profile.reference_asset_id = ctx.source.id
    elif change == "source": ctx.source.metadata_json = {**ctx.source.metadata_json, "storage": {"sha256": "c" * 64}}
    elif change == "usage": ctx.db.add(models.ReferenceUsage(asset_id=ctx.source.id, tenant_id="tenant", project_id="project", allow_output=False))
    elif change == "output": ctx.origin_artifact.delivery_status = "revoked"
    ctx.db.commit()
    with pytest.raises(EditingError):
        assert_job_rights(ctx.db, ctx.render)


def test_provenance_cannot_remove_the_authoritative_identity_or_inputs(rights_case):
    ctx = rights_case
    metadata = deepcopy(ctx.produced.metadata_json)
    metadata["provenance"]["identity"] = None
    ctx.produced.metadata_json = metadata; ctx.db.commit()
    with pytest.raises(EditingError, match="输入签名"):
        assert_job_rights(ctx.db, ctx.render)


def test_input_alias_and_nested_generated_media_keep_their_authorization(rights_case):
    ctx = rights_case
    alias = ctx.timeline.model_dump(mode="json")
    alias["assets"][0]["id"] = "legacy-alias"
    alias["tracks"][0]["clips"][0]["asset_id"] = "legacy-alias"
    assert assert_timeline_rights(ctx.db, tenant_id="tenant", project_id="project", timeline=alias)["sources"]
    nested = models.Asset(id="nested", tenant_id="tenant", project_id="project", name="Nested", kind="video/mp4",
                          uri="oss://bucket/tenants/tenant/nested", metadata_json={"duration": 5})
    inputs = {ctx.produced.id: source_hash(ctx.produced)}
    origin = models.Job(id="nested-origin", tenant_id="tenant", project_id="project", type="video.generate", status="succeeded",
        payload={"ai_production": {"request": {"operation": "video_generation"}, "snapshot": {"identity": None, "sources": inputs},
                                   "adopted_asset_id": nested.id}})
    artifact = models.Artifact(id="nested-output", tenant_id="tenant", project_id="project", job_id=origin.id, kind="video", uri=nested.uri)
    nested.metadata_json = {"provenance": {"job_id": origin.id, "artifact_id": artifact.id, "identity": None,
                                           "operation": "video_generation", "input_sources": inputs}}
    ctx.db.add_all([origin, artifact, nested]); ctx.db.commit()
    assert_asset_rights(ctx.db, nested, tenant_id="tenant", project_id="project")
    ctx.consent.revoked_at = datetime.now(timezone.utc); ctx.db.commit()
    with pytest.raises(EditingError): assert_asset_rights(ctx.db, nested, tenant_id="tenant", project_id="project")
    with pytest.raises(EditingError): assert_timeline_rights(ctx.db, tenant_id="tenant", project_id="project", timeline=alias)


def test_regional_https_object_alias_cannot_erase_derived_identity(rights_case):
    ctx = rights_case
    alias = ctx.timeline.model_dump(mode="json")
    alias["assets"][0].update(id="unknown-id", uri="https://bucket.oss-cn-beijing.aliyuncs.com/tenants/tenant/produced")
    alias["tracks"][0]["clips"][0]["asset_id"] = "unknown-id"
    assert assert_timeline_rights(ctx.db, tenant_id="tenant", project_id="project", timeline=alias)["sources"]
    ctx.profile.status = "revoked"; ctx.db.commit()
    with pytest.raises(EditingError):
        assert_timeline_rights(ctx.db, tenant_id="tenant", project_id="project", timeline=alias)


@pytest.mark.parametrize("https_alias", [False, True])
def test_existing_ordinary_asset_id_for_a_derived_object_keeps_its_rights(rights_case, https_alias):
    ctx = rights_case
    uri = "https://bucket.oss-cn-beijing.aliyuncs.com/tenants/tenant/produced" if https_alias else ctx.produced.uri
    ordinary = models.Asset(id="ordinary-alias", tenant_id="tenant", project_id="project", name="Same object",
                            uri=uri, kind="video/mp4", metadata_json={})
    ctx.db.add(ordinary); ctx.db.commit()
    value = ctx.timeline.model_dump(mode="json")
    value["assets"][0].update(id=ordinary.id, uri=ordinary.uri)
    value["tracks"][0]["clips"][0]["asset_id"] = ordinary.id
    assert "object_derivations" in assert_timeline_rights(ctx.db, tenant_id="tenant", project_id="project", timeline=value)["sources"][ordinary.id]
    assert asset_playback(ctx.db, ordinary, tenant_id="tenant", gateway=AccessGateway())["url"]
    ctx.consent.granted = False; ctx.db.commit()
    with pytest.raises(EditingError): assert_timeline_rights(ctx.db, tenant_id="tenant", project_id="project", timeline=value)
    with pytest.raises(EditingError): asset_playback(ctx.db, ordinary, tenant_id="tenant", gateway=AccessGateway())


def test_dependency_cycles_fail_closed_and_unused_sources_do_not_block(rights_case):
    ctx = rights_case
    unused = ctx.timeline.model_dump(mode="json")
    unused["assets"].append({"id": "foreign", "type": "video", "uri": "oss://bucket/tenants/other/foreign"})
    assert assert_timeline_rights(ctx.db, tenant_id="tenant", project_id="project", timeline=unused) == ctx.frozen
    inputs = {ctx.produced.id: source_hash(ctx.produced), ctx.reference.id: source_hash(ctx.reference)}
    payload = deepcopy(ctx.origin.payload); payload["ai_production"]["snapshot"]["sources"] = inputs
    ctx.origin.payload = payload
    metadata = deepcopy(ctx.produced.metadata_json); metadata["provenance"]["input_sources"] = inputs
    ctx.produced.metadata_json = metadata; ctx.db.commit()
    with pytest.raises(EditingError, match="依赖循环"):
        assert_asset_rights(ctx.db, ctx.produced, tenant_id="tenant", project_id="project")


class AccessGateway:
    def __init__(self, hook=None): self.hook, self.calls = hook, 0
    def artifact_access(self, **kwargs):
        self.calls += 1
        assert kwargs["purpose"] == "preview"
        if self.hook: self.hook()
        return {"url": "https://fixture.invalid/signed", "expires_at": datetime.now(timezone.utc) + timedelta(seconds=kwargs["ttl_seconds"])}


def test_revoked_derived_media_cannot_get_a_new_preview_link(rights_case):
    ctx = rights_case; gateway = AccessGateway()
    assert artifact_access(ctx.db, tenant_id="tenant", artifact_id="final", purpose="preview", gateway=gateway)["url"]
    ctx.profile.status = "revoked"; ctx.db.commit()
    with pytest.raises(StorageGatewayError) as error:
        artifact_access(ctx.db, tenant_id="tenant", artifact_id="final", purpose="preview", gateway=gateway)
    assert error.value.code == "rights_profile_inactive" and gateway.calls == 1


def test_consent_revocation_during_signing_never_returns_the_signed_url(rights_case):
    ctx = rights_case
    def revoke():
        with ctx.factory.begin() as other:
            other.get(models.Consent, "consent").revoked_at = datetime.now(timezone.utc)
    gateway = AccessGateway(revoke)
    with pytest.raises(StorageGatewayError) as error:
        artifact_access(ctx.db, tenant_id="tenant", artifact_id="final", purpose="preview", gateway=gateway)
    assert error.value.code == "rights_consent_inactive" and gateway.calls == 1


def test_adopted_asset_preview_cannot_bypass_revocation_but_raw_reference_remains_viewable(rights_case):
    ctx = rights_case; gateway = AccessGateway()
    assert asset_playback(ctx.db, ctx.produced, tenant_id="tenant", gateway=gateway)["url"]
    ctx.consent.granted = False
    ctx.db.add(models.ReferenceUsage(asset_id=ctx.reference.id, tenant_id="tenant", project_id="project", allow_output=False))
    ctx.db.commit()
    with pytest.raises(EditingError): asset_playback(ctx.db, ctx.produced, tenant_id="tenant", gateway=gateway)
    assert asset_playback(ctx.db, ctx.reference, tenant_id="tenant", gateway=gateway)["url"]
    assert gateway.calls == 2


def test_asset_preview_rechecks_rights_after_remote_signing(rights_case):
    ctx = rights_case
    def revoke():
        with ctx.factory.begin() as other:
            other.get(models.AvatarProfile, "profile").status = "revoked"
    gateway = AccessGateway(revoke)
    with pytest.raises(EditingError): asset_playback(ctx.db, ctx.produced, tenant_id="tenant", gateway=gateway)
    assert gateway.calls == 1


def test_legacy_render_entry_blocks_new_work_but_keeps_original_receipt(rights_case, monkeypatch):
    from app.api import routers
    ctx = rights_case
    monkeypatch.setattr(routers.settings, "app_environment", "development")
    row = models.Timeline(id="legacy", tenant_id="tenant", project_id="project", name="Legacy", fps=30,
                          duration_ms=5000, timeline_json=ctx.timeline.model_dump(mode="json"))
    ctx.db.add(row); ctx.db.commit()
    request = schemas.RenderRequest(quality="draft")
    first = routers._render_timeline_job(ctx.db, row, request, tenant_id="tenant", key="first-render")
    assert first["job"].payload["derived_rights"] == ctx.frozen
    ctx.profile.revoked_at = datetime.now(timezone.utc); ctx.db.commit()
    assert routers._render_timeline_job(ctx.db, row, request, tenant_id="tenant", key="first-render")["job"].id == first["job"].id
    with pytest.raises(EditingError):
        routers._render_timeline_job(ctx.db, row, request, tenant_id="tenant", key="another-render")
    from app.services import render_quotes
    with pytest.raises(EditingError):
        render_quotes.quote_context(ctx.db, row, "tenant", request)


def test_saved_edit_revision_rechecks_derived_rights_without_rewriting_its_snapshot(rights_case):
    from app.services.editing import _capture_sources, _insert_revision, assert_render_sources
    ctx = rights_case
    document = models.EditDocument(id="document", tenant_id="tenant", project_id="project", origin_job_id=ctx.origin.id, name="Edit")
    ctx.db.add(document); ctx.db.flush()
    revision = _insert_revision(ctx.db, document, number=1, parent=None, key="initial", request_hash="d" * 64,
        template={"id": "template", "version": "1"}, sources=_capture_sources([ctx.produced]), plan={},
        timeline=ctx.timeline, operation={})
    ctx.db.commit()
    frozen = deepcopy(assert_render_sources(ctx.db, document, revision).timeline_json)
    ctx.consent.granted = False; ctx.db.commit()
    with pytest.raises(EditingError):
        assert_render_sources(ctx.db, document, revision)
    assert ctx.db.get(models.TimelineRevision, revision.id).timeline_json == frozen


def test_frozen_rights_cannot_be_bypassed_by_removing_the_manifest(rights_case):
    ctx = rights_case
    ctx.render.payload = {"derived_rights": ctx.frozen}; ctx.db.commit()
    with pytest.raises(EditingError, match="原输入"):
        assert_job_rights(ctx.db, ctx.render)


@pytest.mark.parametrize("already_remote", [False, True])
def test_worker_blocks_new_billing_but_polls_and_records_original_remote_after_revocation(rights_case, monkeypatch, already_remote):
    from app.services import worker
    from app.services.providers import ModelSnapshot, ProviderResult
    ctx = rights_case
    monkeypatch.setattr(worker, "_api_only_media_execution", lambda: True)
    settings = worker.get_settings().model_copy(update={"app_environment": "development", "storage_root": ctx.root})
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    ctx.db.delete(ctx.db.get(models.ArtifactStorage, "final"))
    ctx.db.delete(ctx.artifact)
    ctx.render.status = "queued"
    if already_remote:
        ctx.db.add(models.ProviderOperation(job_id=ctx.render.id, tenant_id="tenant", request_id=ctx.render.id,
            state="pending", provider_key="fixture", external_job_id="existing-remote"))
    ctx.consent.revoked_at = datetime.now(timezone.utc); ctx.db.commit()
    class Router:
        calls = 0
        def choose(self, request): return SimpleNamespace(name="fixture")
        def execute(self, request):
            self.calls += 1
            assert request.parameters["provider_job_id"] == "existing-remote"
            return ProviderResult(artifact_uri="oss://bucket/tenants/tenant/recovered", artifact_type="render",
                model=ModelSnapshot(provider="fixture", model_id="editor", version="1"),
                cost={"status": "unknown", "currency": "CNY"})
    router = Router()
    result = worker.process_queued_render_jobs(ctx.db, router=router)
    if already_remote:
        assert result[0]["status"] == "succeeded", result
        assert router.calls == 1
        ledger = ctx.db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == ctx.render.id))
        assert ledger.status == "pending_reconciliation"
        with pytest.raises(EditingError):
            assert_job_rights(ctx.db, ctx.render)
    else:
        assert result[0]["status"] == "failed", result
        assert router.calls == 0 and ctx.db.scalar(select(models.ProviderOperation)) is None


def test_revocation_during_provider_selection_blocks_the_new_billable_admission(rights_case, monkeypatch):
    from app.services import worker
    ctx = rights_case
    monkeypatch.setattr(worker, "_api_only_media_execution", lambda: True)
    settings = worker.get_settings().model_copy(update={"app_environment": "development", "storage_root": ctx.root})
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    ctx.db.delete(ctx.db.get(models.ArtifactStorage, "final")); ctx.db.delete(ctx.artifact)
    ctx.render.status = "queued"; ctx.db.commit()
    class Router:
        selected = 0
        def choose(self, request):
            self.selected += 1
            with ctx.factory.begin() as other:
                other.get(models.Consent, "consent").granted = False
            return SimpleNamespace(name="fixture")
        def execute(self, request): pytest.fail("revoked source must not create a new paid operation")
    router = Router()
    result = worker.process_queued_render_jobs(ctx.db, router=router)
    assert router.selected == 1 and result[0]["status"] == "failed", result
    assert ctx.db.scalar(select(models.ProviderOperation)) is None
    assert ctx.db.get(models.Job, "render").failure_code == "rights_consent_inactive"
