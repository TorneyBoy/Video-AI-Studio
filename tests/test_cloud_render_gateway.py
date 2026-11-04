"""Durable rendering integration contracts with explicit cloud substitutes."""

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from timeline_engine import Timeline, create_manifest

from media_gateway.ice_client import ICEAPIError
from media_gateway.ice_timeline import compile_ice_timeline, oss_media_url
from media_gateway.main import GatewaySettings, create_app
from media_gateway.oss import OSSStore
from media_gateway.render_jobs import RenderOperation, RenderQuota, calculated_clip_cost


KEY = "cloud-render-gateway-test-key-32-characters"
DATA = b"fixture bytes: this is not a real cloud acceptance video"


class ObjectClient:
    def head_object(self, request):
        return SimpleNamespace(content_length=len(DATA), etag='"fixture-etag"')

    def get_object(self, request):
        assert request.if_match == '"fixture-etag"'
        return SimpleNamespace(body=io.BytesIO(DATA), etag='"fixture-etag"', request_id="oss-fixture-request")


class CloudClient:
    def __init__(self):
        self.submissions, self.polls, self.fail_submit = [], 0, False
        self.output, self.fps = None, 30
        self.output_config = None

    def submit_render(self, *, client_token, timeline, output):
        self.submissions.append(client_token)
        assert output["Video"]["Fps"] == 30
        assert "Fps" not in output
        self.output = output["MediaURL"]
        self.output_config = output
        if self.fail_submit:
            raise ICEAPIError("ice_request_unconfirmed", retryable=True)
        return {"job_id": "cloud-render", "request_id": "submit-request"}

    def render_status(self, job_id):
        assert job_id == "cloud-render"
        self.polls += 1
        return {"status": "succeeded", "media_url": self.output, "job_id": job_id, "request_id": "poll-request"}

    def media_info(self, uri):
        assert oss_media_url(uri, bucket="fixture-bucket", region="cn-beijing") == self.output
        return {"job_id": "probe-job", "request_id": "probe-request", "facts": {"size_bytes": len(DATA),
            "duration_seconds": 5, "container": "mp4", "has_audio": True,
            "video": {"width": 720, "height": 1280, "codec": "h264", "frame_rate": self.fps}}}


@pytest.fixture
def rendered_gateway(tmp_path, request):
    import alibabacloud_oss_v2 as oss

    cloud = CloudClient()
    store = OSSStore(bucket="fixture-bucket", region="cn-beijing", client=ObjectClient(),
                     credentials_provider=oss.credentials.StaticCredentialsProvider("fixture-id", "fixture-secret"))
    settings = GatewaySettings(api_key=KEY, attestation_secret=KEY, oss_bucket="fixture-bucket", oss_region="cn-beijing",
        database_url=f"sqlite:///{tmp_path / 'renders.db'}", ice_render_enabled=True, ice_render_max_operations=1,
        ice_render_features=getattr(request, "param", []))
    application = create_app(settings, store, ice_client=cloud)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client, application, cloud
    application.state.engine.dispose()


def _payload(request_id="request-1", *, volume=1, tenant="tenant-1"):
    timeline = Timeline.model_validate({"id": "timeline", "fps": 30, "duration": 5,
        "resolution": {"width": 720, "height": 1280}, "assets": [{"id": "source", "type": "video",
        "uri": "oss://fixture-bucket/tenants/tenant-1/uploads/asset/source"}],
        "tracks": [{"id": "video", "type": "video", "clips": [{"id": "clip", "asset_id": "source", "volume": volume,
                     "range": {"start": 0, "end": 5}, "source_start": 0, "source_end": 5}]}]})
    manifest = create_manifest(timeline, options={"quality": "draft"})
    return {"request_id": request_id, "tenant_id": tenant, "capability": "video_editing",
        "inputs": {"manifest": manifest.model_dump(mode="json"), "timeline": timeline.model_dump(mode="json"),
                   "asset_uris": [asset.uri for asset in manifest.assets]}, "parameters": {"quality": "draft"}}


def _headers(request_id="request-1"):
    return {"Authorization": "Bearer " + KEY, "X-Request-ID": request_id, "Idempotency-Key": request_id}


def test_render_submission_is_durable_idempotent_and_trial_quota_is_reserved_once(rendered_gateway):
    client, app, cloud = rendered_gateway
    first = client.post("/v1/render", json=_payload(), headers=_headers())
    assert first.status_code == 200 and cloud.submissions == []
    assert client.post("/v1/render", json=_payload(), headers=_headers()).json() == first.json()
    assert client.post("/v1/render", json=_payload(volume=0), headers=_headers()).status_code == 409
    assert client.post("/v1/render", json=_payload("request-2"), headers=_headers("request-2")).status_code == 429
    with app.state.sessions() as db:
        assert db.get(RenderQuota, "trial").used == 1
    operation = first.json()["provider_job_id"]
    assert client.get(f"/v1/render/{operation}", headers=_headers("wrong-request")).status_code == 404
    assert client.get(f"/v1/render/{operation}", headers=_headers()).json()["status"] == "running"
    result = client.get(f"/v1/render/{operation}", headers=_headers()).json()
    assert result["metrics"]["quality_control"]["status"] == "passed"
    assert result["model"]["provider"] == "aliyun-ice-editing"
    assert result["metrics"]["quality_control"]["output_sha256"] == hashlib.sha256(DATA).hexdigest()
    assert result["artifact_uri"].startswith("oss://fixture-bucket/tenants/tenant-1/renders/")
    assert result["cost"]["amount"] == "0.03" and result["cost"]["bill_confirmed"] is False
    assert client.get(f"/v1/render/{operation}", headers=_headers()).json() == result
    assert len(cloud.submissions) == 1 and cloud.polls == 1


def test_unknown_submit_retries_use_one_cloud_token_and_stop_at_bound(rendered_gateway):
    client, _, cloud = rendered_gateway
    operation = client.post("/v1/render", json=_payload(), headers=_headers()).json()["provider_job_id"]
    cloud.fail_submit = True
    for _ in range(3):
        assert client.get(f"/v1/render/{operation}", headers=_headers()).json()["status"] == "running"
    final = client.get(f"/v1/render/{operation}", headers=_headers()).json()
    assert final["status"] == "failed" and final["error"]["code"] == "ice_operation_reconciliation_required"
    assert cloud.submissions == [operation] * 3


def test_render_recovery_polls_saved_cloud_job_and_keeps_failed_qc_charge_evidence(rendered_gateway):
    client, app, cloud = rendered_gateway
    operation = client.post("/v1/render", json=_payload(), headers=_headers()).json()["provider_job_id"]
    with app.state.sessions.begin() as db:
        row = db.get(RenderOperation, operation)
        row.cloud_job_id, row.status = "cloud-render", "running"
        row.lease_token = "crashed-worker"
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        cloud.output = row.specification["output"]["MediaURL"]
    cloud.fps = 24
    result = client.get(f"/v1/render/{operation}", headers=_headers()).json()
    assert result["metrics"]["quality_control"]["status"] == "failed"
    assert result["cost"]["amount"] == "0.03" and cloud.submissions == []


def test_render_rejects_wrong_tenant_and_corrupt_manifest_before_any_cloud_call(rendered_gateway):
    client, _, cloud = rendered_gateway
    assert client.post("/v1/render", json=_payload(tenant="other"), headers=_headers()).status_code == 422
    payload = _payload()
    payload["inputs"]["manifest"]["duration"] = 99
    assert client.post("/v1/render", json=payload, headers=_headers()).status_code == 422
    assert client.post("/v1/render", json=_payload()).status_code == 401
    assert cloud.submissions == []


def test_actual_workspace_export_manifest_keeps_hash_and_reaches_cloud(rendered_gateway):
    client, app, cloud = rendered_gateway
    payload = _payload()
    timeline = Timeline.model_validate(payload["inputs"]["timeline"])
    manifest = create_manifest(timeline, options={"quality": "high", "source": "studio-flow"})
    payload["inputs"]["manifest"] = manifest.model_dump(mode="json")
    payload["parameters"]["quality"] = "high"
    operation = client.post("/v1/render", json=payload, headers=_headers()).json()["provider_job_id"]
    with app.state.sessions() as db:
        assert db.get(RenderOperation, operation).specification["manifest_hash"] == manifest.content_hash
    assert client.get(f"/v1/render/{operation}", headers=_headers()).json()["status"] == "running"
    assert cloud.output_config["Bitrate"] == 5000
    assert client.get(f"/v1/render/{operation}", headers=_headers()).json()["status"] == "succeeded"
    assert client.post("/v1/render", json=payload, headers=_headers()).json()["status"] == "succeeded"
    assert len(cloud.submissions) == 1


def test_tariff_rounds_minutes_and_distinguishes_unknown_regions_from_free():
    assert calculated_clip_cost(duration=61, width=1920, height=1080, region="cn-beijing")["amount"] == "0.12"
    assert "amount" not in calculated_clip_cost(duration=5, width=1920, height=1080, region="us-west-1")


def _with_audio(payload):
    timeline = payload["inputs"]["timeline"]
    timeline["tracks"].append({"id": "voice", "type": "audio", "clips": [
        {"id": "voice-clip", "asset_id": "source", "range": {"start": 1, "end": 3}, "source_start": 2, "source_end": 4,
         "effects": [{"type": "audio_fade", "schema_version": "audio-envelope/1", "in_seconds": 0.5, "out_seconds": 0.5}]}]})
    manifest = create_manifest(Timeline.model_validate(timeline), options={"quality": "draft"})
    payload["inputs"]["manifest"] = manifest.model_dump(mode="json")
    return payload


def test_new_effects_remain_disabled_without_consuming_quota(rendered_gateway):
    client, app, cloud = rendered_gateway
    result = client.post("/v1/render", json=_with_audio(_payload()), headers=_headers())
    assert result.status_code == 422
    with app.state.sessions() as db:
        assert db.get(RenderQuota, "trial") is None
    assert cloud.submissions == []
    health = client.get("/v1/render/health", headers=_headers()).json()
    assert health["enabled_features"] == [] and not health["cloud_verified"]


@pytest.mark.parametrize("rendered_gateway", [["audio_tracks", "audio_fades"]], indirect=True)
def test_explicit_effect_profile_is_frozen_idempotent_and_preserves_unknown_advanced_cost(rendered_gateway):
    client, app, cloud = rendered_gateway
    payload = _with_audio(_payload())
    first = client.post("/v1/render", json=payload, headers=_headers())
    assert first.status_code == 200, first.text
    operation = first.json()["provider_job_id"]
    assert client.post("/v1/render", json=payload, headers=_headers()).json() == first.json()
    with app.state.sessions() as db:
        row = db.get(RenderOperation, operation)
        assert row.specification["compiler_version"] == "ice-timeline/4"
        assert row.specification["optional_features"] == ["audio_fades", "audio_tracks"]
        voice = row.specification["cloud_timeline"]["AudioTracks"][0]["AudioTrackClips"][0]
        assert voice["In"] == 2 and voice["TimelineIn"] == 1
        assert row.specification["manifest_hash"] == payload["inputs"]["manifest"]["content_hash"]
        assert db.get(RenderQuota, "trial").used == 1
    assert client.get(f"/v1/render/{operation}", headers=_headers()).json()["status"] == "running"
    result = client.get(f"/v1/render/{operation}", headers=_headers()).json()
    assert result["status"] == "succeeded" and result["metrics"]["quality_control"]["status"] == "passed"
    assert result["metrics"]["compiler_version"] == "ice-timeline/4"
    assert result["cost"]["status"] == "unknown" and "amount" not in result["cost"]
    assert len(cloud.submissions) == 1


def test_render_feature_env_is_strict_and_does_not_change_trial_limits(monkeypatch):
    monkeypatch.setenv("STORAGE_GATEWAY_API_KEY", KEY)
    monkeypatch.setenv("STORAGE_ATTESTATION_SECRET", KEY)
    monkeypatch.setenv("OSS_BUCKET", "fixture-bucket")
    monkeypatch.setenv("OSS_REGION", "cn-beijing")
    monkeypatch.setenv("GATEWAY_ICE_RENDER_FEATURES", " audio_tracks, audio_fades,audio_tracks ")
    monkeypatch.delenv("GATEWAY_ICE_RENDER_MAX_OPERATIONS", raising=False)
    settings = GatewaySettings.from_env()
    assert settings.ice_render_features == ["audio_fades", "audio_tracks"]
    assert settings.ice_render_max_operations == 6
    monkeypatch.setenv("GATEWAY_ICE_RENDER_FEATURES", "audio_tracks,unimplemented_magic")
    with pytest.raises(ValueError, match="installed compiler"):
        GatewaySettings.from_env()


def _with_native_image(payload):
    timeline = payload["inputs"]["timeline"]
    timeline["assets"][0]["type"] = "image"
    timeline["tracks"][0]["clips"][0]["source_end"] = None
    payload["inputs"]["manifest"] = create_manifest(Timeline.model_validate(timeline), options={"quality": "draft"}).model_dump(mode="json")
    return payload


@pytest.mark.parametrize("rendered_gateway", [["image_clips"]], indirect=True)
def test_known_native_image_timing_failure_cannot_be_enabled_into_new_chargeable_work(rendered_gateway):
    client, app, cloud = rendered_gateway
    result = client.post("/v1/render", json=_with_native_image(_payload()), headers=_headers())
    assert result.status_code == 422 and "renderer_quality_unverified" in result.text
    with app.state.sessions() as db:
        assert db.get(RenderQuota, "trial") is None
        assert db.query(RenderOperation).count() == 0
    health = client.get("/v1/render/health", headers=_headers()).json()
    assert health["blocked_execution_constraints"] == ["ice_image_end_unverified"]
    assert health["quality_scope"] == "structural_probe" and cloud.submissions == []


def _save_legacy_image_operation(app, cloud, *, paid):
    payload = _with_native_image(_payload())
    timeline = Timeline.model_validate(payload["inputs"]["timeline"])
    native, output = compile_ice_timeline(timeline, bucket="fixture-bucket", region="cn-beijing", enabled_features=["image_clips"])
    token = hashlib.sha256(b"tenant-1\nrequest-1").hexdigest()
    uri = f"oss://fixture-bucket/tenants/tenant-1/renders/{token}/output.mp4"
    output.update(MediaURL=oss_media_url(uri, bucket="fixture-bucket", region="cn-beijing"), Bitrate=1500)
    specification = {"timeline": timeline.model_dump(mode="json"), "cloud_timeline": native,
        "output": output, "manifest_hash": payload["inputs"]["manifest"]["content_hash"]}
    digest = hashlib.sha256(json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with app.state.sessions.begin() as db:
        db.add(RenderQuota(id="trial", used=1))
        db.add(RenderOperation(id=token, request_id="request-1", tenant_id="tenant-1", request_hash=digest,
            specification=specification, object_uri=uri, cloud_job_id="cloud-render" if paid else None,
            status="running" if paid else "queued", submit_attempts=1 if paid else 0))
    cloud.output = output["MediaURL"]
    return payload, token


@pytest.mark.parametrize("rendered_gateway", [["image_clips"]], indirect=True)
def test_saved_paid_image_replays_and_polls_its_original_identity_after_new_admission_fence(rendered_gateway):
    client, app, cloud = rendered_gateway
    payload, token = _save_legacy_image_operation(app, cloud, paid=True)
    replay = client.post("/v1/render", json=payload, headers=_headers())
    assert replay.status_code == 200 and replay.json()["provider_job_id"] == token
    result = client.get(f"/v1/render/{token}", headers=_headers()).json()
    assert result["status"] == "succeeded" and cloud.submissions == [] and cloud.polls == 1
    # Successful probe and successful original cloud task do not claim the
    # known Image issue passed or that visual/listening quality was inspected.
    assert result["metrics"]["render_quality_assessment"]["not_assessed_dimensions"] == ["facts", "narrative", "visual", "audio"]
    assert result["metrics"]["renderer_quality_findings"][0]["code"] == "ice_image_end_unverified"
    with app.state.sessions() as db:
        assert db.get(RenderQuota, "trial").used == 1
        assert db.get(RenderOperation, token).request_hash == hashlib.sha256(
            json.dumps(db.get(RenderOperation, token).specification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("rendered_gateway", [["image_clips"]], indirect=True)
def test_saved_queued_image_cannot_start_a_new_supplier_submission(rendered_gateway):
    client, app, cloud = rendered_gateway
    _, token = _save_legacy_image_operation(app, cloud, paid=False)
    result = client.get(f"/v1/render/{token}", headers=_headers()).json()
    assert result["status"] == "failed" and result["error"]["code"] == "renderer_quality_unverified"
    assert cloud.submissions == [] and cloud.polls == 0
    with app.state.sessions() as db:
        assert db.get(RenderQuota, "trial").used == 1
        assert db.get(RenderOperation, token).submit_attempts == 0
        assert db.get(RenderOperation, token).lease_token is None


def test_existing_m1_operation_without_compiler_metadata_replays_original_identity(rendered_gateway):
    client, app, cloud = rendered_gateway
    payload = _payload()
    first = client.post("/v1/render", json=payload, headers=_headers()).json()
    with app.state.sessions.begin() as db:
        row = db.get(RenderOperation, first["provider_job_id"])
        # Previous gateway releases stored this exact execution specification
        # without compiler audit metadata. Its persisted request hash survives.
        original = dict(row.specification)
        original.pop("compiler_version")
        original.pop("optional_features")
        assert row.request_hash == hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        row.specification = original
    assert client.post("/v1/render", json=payload, headers=_headers()).json() == first
    with app.state.sessions() as db:
        assert db.get(RenderQuota, "trial").used == 1
    assert cloud.submissions == []
