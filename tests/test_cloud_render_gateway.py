"""Durable rendering integration contracts with explicit cloud substitutes."""

from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from timeline_engine import Timeline, create_manifest

from media_gateway.ice_client import ICEAPIError
from media_gateway.ice_timeline import oss_media_url
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
def rendered_gateway(tmp_path):
    import alibabacloud_oss_v2 as oss

    cloud = CloudClient()
    store = OSSStore(bucket="fixture-bucket", region="cn-beijing", client=ObjectClient(),
                     credentials_provider=oss.credentials.StaticCredentialsProvider("fixture-id", "fixture-secret"))
    settings = GatewaySettings(api_key=KEY, attestation_secret=KEY, oss_bucket="fixture-bucket", oss_region="cn-beijing",
        database_url=f"sqlite:///{tmp_path / 'renders.db'}", ice_render_enabled=True, ice_render_max_operations=1)
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
