"""Durability, cost and cloud source boundaries; fixtures are not real acceptance."""

from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from media_gateway.analysis_jobs import AnalysisOperation, AnalysisQuota, calculated_analysis_cost
from media_gateway.ice_client import ICEAPIError
from media_gateway.main import GatewaySettings, create_app
from media_gateway.oss import ObjectStoreError
from media_gateway.shot_client import DETECTOR_VERSION, ShotServiceError


KEY = "fixture-analysis-gateway-key-at-least-32-characters"
DATA = b"fixture source bytes; no actual video processing"
SOURCE = "oss://fixture-bucket/tenants/tenant-1/uploads/asset/source"


class Store:
    credentials_provider = None

    def __init__(self, bucket="fixture-bucket"):
        self.bucket, self.objects, self.snapshots = bucket, {}, []

    def object_uri(self, key):
        return f"oss://{self.bucket}/{key}"

    def key_from_uri(self, uri, tenant):
        prefix = f"oss://{self.bucket}/tenants/{tenant}/"
        if not uri.startswith(prefix):
            raise ObjectStoreError("object_tenant_mismatch", "Wrong tenant", 403)
        return uri.split("/", 3)[3]

    def observe(self, *, key, expected_size, expected_hash):
        body = self.objects.get(key, DATA)
        digest = hashlib.sha256(body).hexdigest()
        if expected_size != len(body) or expected_hash and expected_hash != digest:
            raise ObjectStoreError("object_hash_mismatch", "Wrong content", 422)
        return {"size_bytes": len(body), "sha256": digest, "etag": "source-etag"}

    def put_verified(self, *, key, data, content_type):
        assert key not in self.objects or self.objects[key] == data
        self.objects[key] = data
        return self.observe(key=key, expected_size=len(data), expected_hash=hashlib.sha256(data).hexdigest())

    def access(self, *, key, ttl_seconds, purpose):
        assert ttl_seconds == 900
        return {"url": f"https://fixture-bucket.oss-cn-beijing.aliyuncs.com/{key}?secret=fixture-signature"}

    def result_grant(self, **kwargs):
        return {"url": "https://fixture-bucket.oss-cn-beijing.aliyuncs.com/result?secret=fixture-target", "headers": {}}

    def read_json(self, *, key, max_bytes):
        operation = key.split("/")[3]
        return {"schema_version": "shot-boundaries/1", "detector_version": DETECTOR_VERSION,
            "operation_id": operation, "source_sha256": hashlib.sha256(DATA).hexdigest(),
            "clock": "decoded-frame-pts-seconds", "shots": [{"start": 0, "end": 2}, {"start": 2, "end": 5}]}

    def snapshot(self, **kwargs):
        self.snapshots.append(kwargs)
        return {"size_bytes": 123, "sha256": "a" * 64, "etag": "snapshot-etag"}


class Ice:
    def __init__(self):
        self.submissions, self.duration, self.fail_submit = [], 5, False

    def media_info(self, uri):
        assert uri == SOURCE
        return {"job_id": "probe", "request_id": "probe-request", "facts": {
            "duration_seconds": self.duration, "size_bytes": len(DATA), "format": "mp4", "has_audio": True,
            "video": {"width": 720, "height": 1280, "codec": "h264", "frame_rate": 30},
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2}}}

    def submit_asr(self, **kwargs):
        self.submissions.append(kwargs)
        if self.fail_submit:
            raise ICEAPIError("ice_request_unconfirmed", retryable=True)
        return {"job_id": "asr-job", "request_id": "asr-submit"}

    def asr_status(self, job_id, **kwargs):
        assert job_id == "asr-job" and kwargs["object_uri"] == SOURCE
        return {"status": "succeeded", "request_id": "asr-poll", "segments": [{"start": 0, "end": 2, "text": "真实接口替身文本"}]}


class Detector:
    def __init__(self):
        self.submissions, self.fail_submit = [], False

    def submit_shots(self, *, operation_id, event):
        self.submissions.append(event)
        if self.fail_submit:
            raise ShotServiceError("shot_request_unconfirmed", retryable=True)
        return {"job_id": "shots_" + operation_id, "request_id": "shot-submit"}

    def shot_status(self, job_id):
        assert job_id.startswith("shots_")
        return {"status": "succeeded", "request_id": "shot-poll", "duration_ms": 1200}


@pytest.fixture
def gateway(tmp_path):
    store, ice, detector = Store(), Ice(), Detector()
    settings = GatewaySettings(api_key=KEY, attestation_secret=KEY, oss_bucket="fixture-bucket", oss_region="cn-beijing",
        database_url=f"sqlite:///{tmp_path / 'analysis.db'}", analysis_enabled=True,
        analysis_fc_account_id="1234567890123456", analysis_max_operations=1)
    app = create_app(settings, store, ice_client=ice, shot_client=detector)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, app=app, store=store, ice=ice, detector=detector)
    app.state.engine.dispose()


def headers(request="request-1"):
    return {"Authorization": "Bearer " + KEY, "X-Request-ID": request, "Idempotency-Key": request}


def payload(request="request-1", *, transcribe=True, checksum=None):
    return {"request_id": request, "tenant_id": "tenant-1", "capability": "media_analysis",
        "inputs": {"asset_id": "asset", "asset_uri": SOURCE, "uri": SOURCE, "kind": "video/mp4",
                   "sha256": checksum or hashlib.sha256(DATA).hexdigest()},
        "parameters": {"shot_analysis": True, "transcribe": transcribe}}


def finish(gateway, operation):
    for _ in range(15):
        response = gateway.client.get(f"/v1/analysis/{operation}", headers=headers())
        assert response.status_code == 200
        result = response.json()
        if result["status"] in {"succeeded", "failed"}:
            return result
    raise AssertionError("Fixture analysis did not finish")


def test_hosted_analysis_durable_inputs_hash_cloud_results_thumbnails_and_cost(gateway):
    client = gateway.client
    submitted = client.post("/v1/analysis", json=payload(), headers=headers())
    assert submitted.status_code == 200 and not gateway.detector.submissions and not gateway.ice.submissions
    assert client.post("/v1/analysis", json=payload(), headers=headers()).json() == submitted.json()
    assert client.post("/v1/analysis", json=payload(transcribe=False), headers=headers()).status_code == 409
    assert client.post("/v1/analysis", json=payload("request-2"), headers=headers("request-2")).status_code == 429
    operation = submitted.json()["provider_job_id"]
    assert client.get(f"/v1/analysis/{operation}", headers=headers("wrong-request")).status_code == 404
    result = finish(gateway, operation)
    assert result["status"] == "succeeded" and result["analysis"]["completeness"] == "complete"
    assert result["model"]["provider"] == "aliyun-hosted-analysis"
    assert result["analysis"]["shots"][1]["start"] == 2
    assert result["analysis"]["transcription"]["segments"][0]["end"] == 2
    assert len(gateway.detector.submissions) == len(gateway.ice.submissions) == 1
    assert [row["time_ms"] for row in gateway.store.snapshots] == [1000, 3500]
    assert result["analysis"]["shots"][0]["thumbnail_uri"].startswith("oss://fixture-bucket/tenants/tenant-1/")
    assert result["cost"]["status"] == "unknown" and result["cost"]["bill_confirmed"] is False
    assert "fixture-signature" not in str(result)
    with gateway.app.state.sessions() as db:
        row = db.get(AnalysisOperation, operation)
        assert "fixture-signature" not in str(row.progress) and db.get(AnalysisQuota, "trial").used == 1
    assert client.get(f"/v1/analysis/{operation}", headers=headers()).json() == result
    assert len(gateway.detector.submissions) == 1


@pytest.mark.parametrize("stage", ["shots", "asr"])
def test_uncertain_submissions_are_never_automatically_repeated(gateway, stage):
    target = gateway.detector if stage == "shots" else gateway.ice
    target.fail_submit = True
    operation = gateway.client.post("/v1/analysis", json=payload(), headers=headers()).json()["provider_job_id"]
    result = finish(gateway, operation)
    assert result["status"] == "succeeded" and result["analysis"]["completeness"] == ("partial" if stage == "asr" else "complete")
    assert result["cost"]["status"] == "unknown" and "amount" not in result["cost"]
    assert len(target.submissions) == 1
    assert result["analysis"]["shot_analysis" if stage == "shots" else "transcription"]["status"] == ("failed" if stage == "asr" else "ok")


def test_crash_after_submission_fence_does_not_resubmit_and_lease_recovers(gateway):
    operation = gateway.client.post("/v1/analysis", json=payload(), headers=headers()).json()["provider_job_id"]
    gateway.client.get(f"/v1/analysis/{operation}", headers=headers())
    with gateway.app.state.sessions.begin() as db:
        row = db.get(AnalysisOperation, operation)
        row.progress = {**row.progress, "shots": {"status": "submitting", "job_id": "shots_" + operation,
            "result_key": f"tenants/tenant-1/analyses/{operation}/shot-result.json"}}
        row.lease_token, row.lease_expires_at = "crashed", datetime.now(timezone.utc) - timedelta(seconds=1)
    result = finish(gateway, operation)
    assert not gateway.detector.submissions and result["cost"]["status"] == "unknown"
    assert result["analysis"]["shot_analysis"]["status"] == "ok"


def test_changed_source_is_rejected_before_paid_shot_or_asr_calls(gateway):
    operation = gateway.client.post("/v1/analysis", json=payload(checksum="b" * 64), headers=headers()).json()["provider_job_id"]
    result = finish(gateway, operation)
    assert result["status"] == "failed" and result["error"]["code"] == "object_hash_mismatch"
    assert not gateway.detector.submissions and not gateway.ice.submissions


def test_actual_duration_limits_apply_before_analysis_submission(gateway):
    gateway.ice.duration = 31
    operation = gateway.client.post("/v1/analysis", json=payload(), headers=headers()).json()["provider_job_id"]
    assert finish(gateway, operation)["error"]["code"] == "analysis_trial_input_limit"
    assert not gateway.detector.submissions and not gateway.ice.submissions


def test_published_cost_uses_seconds_and_does_not_claim_final_storage_bill():
    cost = calculated_analysis_cost(duration=30, shots="not_requested", asr="ok")
    assert cost["amount"] == "0.02085" and cost["bill_confirmed"] is False
    assert "OSS_snapshots" in cost["excludes"]
    assert calculated_analysis_cost(duration=30, shots="ok", asr="ok")["status"] == "unknown"
