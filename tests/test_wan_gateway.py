"""Offline Wan HTTP/operation contracts; fixture bytes are not real media QA."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_gateway.database import Base
from media_gateway.ice_client import ICEAPIError
from media_gateway.oss import ObjectStoreError
from media_gateway.wan_client import GENERATION_PATH, REPLACEMENT_PATH, WanClient, WanSettings
from media_gateway.wan_jobs import WanOperation, WanQuota, WanRequest, _specification, register_wan_routes


DATA = b"explicit offline fixture, not a generated media sample"
KEY = "synthetic-wan-api-key"
RESULT_URL = "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result.mp4?Signature=fixture"
SOURCE = "oss://fixture-bucket/tenants/tenant-1/uploads/source/video.mp4"
REFERENCE = "oss://fixture-bucket/tenants/tenant-1/uploads/reference/image.png"


class Transport:
    def __init__(self):
        self.calls, self.submit_failure = [], None
        self.state, self.result_url = "SUCCEEDED", RESULT_URL
        self.result_data = DATA
        self.before_submit = None

    def __call__(self, request, timeout, limit):
        headers = {key.lower(): value for key, value in request.header_items()}
        self.calls.append((request.method, request.full_url, headers, request.data))
        if request.method == "POST":
            if self.before_submit:
                self.before_submit()
            assert headers["authorization"] == "Bearer " + KEY
            assert headers["x-dashscope-async"] == "enable"
            if self.submit_failure == "timeout":
                raise TimeoutError("synthetic private error must not escape")
            if self.submit_failure == "reject":
                return 400, b'{"code":"InvalidParameter"}'
            if self.submit_failure == "server_error":
                return 503, b'{"code":"ServiceUnavailable"}'
            if self.submit_failure == "malformed":
                return 200, b'{"output":{}}'
            return 200, b'{"output":{"task_id":"cloud-task","task_status":"PENDING"},"request_id":"cloud-submit"}'
        if "/api/v1/tasks/" in request.full_url:
            assert headers["authorization"] == "Bearer " + KEY
            return 200, json.dumps({"output": {"task_id": "cloud-task", "task_status": self.state,
                "results": {"video_url": self.result_url}}, "request_id": "cloud-poll"}).encode()
        assert request.full_url.startswith("https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/")
        assert "authorization" not in headers
        return 200, self.result_data


class Store:
    bucket, region = "fixture-bucket", "cn-beijing"

    def __init__(self):
        self.objects, self.grants = {}, []

    def object_uri(self, key):
        return "oss://fixture-bucket/" + key

    def key_from_uri(self, uri, tenant):
        prefix = self.object_uri("tenants/" + tenant + "/")
        if not uri.startswith(prefix) or ".." in uri:
            raise ObjectStoreError("object_tenant_mismatch", "Synthetic object is outside tenant", 403)
        return uri.removeprefix("oss://fixture-bucket/")

    def access(self, *, key, ttl_seconds, purpose):
        self.grants.append(key)
        return {"url": "https://fixture-bucket.oss-cn-beijing.aliyuncs.com/" + key + "?signed=fixture"}

    def put_verified(self, *, key, data, content_type):
        if key in self.objects and self.objects[key] != data:
            raise ObjectStoreError("immutable_output_conflict", "Cannot replace fixture output", 409)
        self.objects[key] = data
        return self.observe(key=key, expected_size=len(data), expected_hash=hashlib.sha256(data).hexdigest())

    def observe(self, *, key, expected_size, expected_hash):
        data = self.objects[key]
        assert expected_size == len(data)
        digest = hashlib.sha256(data).hexdigest()
        if expected_hash and digest != expected_hash:
            raise ObjectStoreError("object_hash_mismatch", "Synthetic result hash differs", 422)
        return {"size_bytes": len(data), "sha256": digest, "etag": "fixture"}


class Probe:
    def __init__(self):
        self.fps, self.duration, self.calls = 30, 5, []
        self.width, self.height = 1080, 1920

    def media_info(self, uri):
        self.calls.append(uri)
        return {"job_id": "probe-job", "request_id": "probe-request", "facts": {"size_bytes": len(DATA),
            "duration_seconds": self.duration, "container": "mp4", "has_audio": False,
            "video": {"width": self.width, "height": self.height, "frame_rate": self.fps, "codec": "h264"}}}


@pytest.fixture
def gateway(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "wan.db"), connect_args={"check_same_thread": False})
    sessions = sessionmaker(engine, expire_on_commit=False)
    transport, store, probe = Transport(), Store(), Probe()
    settings = WanSettings(enabled=True, api_key=KEY, max_operations=2)
    app = FastAPI()

    def authorize(authorization: str = Header(default="")):
        if authorization != "Bearer gateway-fixture":
            raise ObjectStoreError("unauthorized", "Gateway authorization required", 401)

    @app.exception_handler(ObjectStoreError)
    async def store_error(request, exc):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=exc.status)

    register_wan_routes(app, settings=settings, sessions=sessions, store=store, authorize=authorize,
                        client=WanClient(settings, transport=transport), ice_client=probe)
    Base.metadata.create_all(engine)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, sessions=sessions, transport=transport, store=store, probe=probe, settings=settings)
    engine.dispose()


def payload(request_id="request-1", capability="video_generation", model="wan2.2-t2v-plus"):
    return {"request_id": request_id, "tenant_id": "tenant-1", "capability": capability,
            "model_id": model, "model_version": "wan2.2", "inputs": {"prompt": "授权商品的固定镜头"}, "parameters": {"duration": 5}}


def headers(request_id="request-1"):
    return {"Authorization": "Bearer gateway-fixture", "X-Request-ID": request_id, "Idempotency-Key": request_id}


def start(case, value=None):
    response = case.client.post("/v1/wan", json=value or payload(), headers=headers())
    assert response.status_code == 200, response.text
    return response.json()["provider_job_id"]


def poll(case, operation):
    response = case.client.get("/v1/wan/jobs/" + operation, headers=headers())
    assert response.status_code == 200, response.text
    return response.json()


def test_generation_is_durable_and_archived_only_after_real_contract_verification(gateway):
    case = gateway; operation = start(case)
    assert case.transport.calls == []
    assert start(case) == operation
    with case.sessions() as db:
        assert db.get(WanQuota, "wan").used == 1

    def committed_fence():
        with case.sessions() as db:
            assert db.get(WanOperation, operation).status == "submitting"
    case.transport.before_submit = committed_fence
    assert poll(case, operation)["status"] == "running"
    request = case.transport.calls[0]
    assert request[1].endswith(GENERATION_PATH)
    assert json.loads(request[3])["parameters"] == {"size": "1080*1920", "prompt_extend": False}
    result = poll(case, operation)
    assert result["status"] == "succeeded"
    assert result["artifact_uri"] == f"oss://fixture-bucket/tenants/tenant-1/generated/{operation}/output.mp4"
    assert result["metrics"]["media"]["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert result["metrics"]["media"]["status"] == "ok" and result["model"]["provider"] == "aliyun-wan"
    assert result["cost"]["status"] == "unknown" and "amount" not in result["cost"]
    assert poll(case, operation) == result
    assert len([call for call in case.transport.calls if call[0] == "POST"]) == 1


def test_image_generation_signs_only_managed_input(gateway):
    case = gateway; value = payload(model="wan2.2-i2v-plus")
    value["inputs"]["reference_asset_uris"] = [REFERENCE]
    operation = start(case, value); poll(case, operation)
    sent = json.loads(case.transport.calls[0][3])
    assert sent["input"]["img_url"].startswith("https://fixture-bucket.oss-cn-beijing.aliyuncs.com/tenants/tenant-1/")
    assert sent["parameters"]["resolution"] == "1080P"


def test_person_replacement_uses_distinct_official_contract_and_not_face_transform(gateway):
    case = gateway; value = payload(capability="person_replacement", model="wan2.2-animate-mix")
    case.probe.width, case.probe.height = 720, 1280
    value["inputs"] = {"source_asset_uri": SOURCE, "reference_asset_uris": [REFERENCE]}
    value["consent_ids"] = ["consent-1"]
    operation = start(case, value); poll(case, operation)
    request = case.transport.calls[0]
    assert request[1].endswith(REPLACEMENT_PATH)
    sent = json.loads(request[3])
    assert set(sent["input"]) == {"video_url", "image_url"}
    assert sent["parameters"] == {"mode": "wan-std", "check_image": True}
    assert poll(case, operation)["status"] == "succeeded"
    value["request_id"] = "invalid"; value["capability"] = "face_transform"
    assert case.client.post("/v1/wan", json=value, headers=headers("invalid")).status_code == 422


@pytest.mark.parametrize("failure", ["timeout", "malformed", "server_error"])
def test_unknown_submission_never_reposts_after_replay_or_lease_expiry(gateway, failure):
    case = gateway; case.transport.submit_failure = failure; operation = start(case)
    assert poll(case, operation)["submission_unknown"]
    assert start(case) == operation
    for _ in range(3):
        assert poll(case, operation)["submission_unknown"]
    assert len(case.transport.calls) == 1


def test_explicit_submission_rejection_is_terminal_and_never_resubmits(gateway):
    case = gateway; case.transport.submit_failure = "reject"; operation = start(case)
    result = poll(case, operation)
    assert result["status"] == "failed" and not result["retryable"]
    assert "artifact_uri" not in result
    assert start(case) == operation and poll(case, operation) == result
    assert len(case.transport.calls) == 1


def test_busy_lease_cannot_dispatch_but_expired_lease_can_resume(gateway):
    case = gateway; operation = start(case)
    with case.sessions.begin() as db:
        row = db.get(WanOperation, operation)
        row.lease_token = "other-worker"; row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert poll(case, operation)["status"] == "queued" and not case.transport.calls
    with case.sessions.begin() as db:
        db.get(WanOperation, operation).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert poll(case, operation)["status"] == "running"
    assert len(case.transport.calls) == 1


def test_crash_after_submit_fence_cannot_create_another_task(gateway):
    case = gateway; operation = start(case)
    with case.sessions.begin() as db:
        row = db.get(WanOperation, operation)
        row.status = "submitting"; row.lease_token = "crashed"
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert poll(case, operation)["submission_unknown"]
    assert case.transport.calls == []


def test_known_task_recovers_by_polling_without_reposting(gateway):
    case = gateway; operation = start(case)
    with case.sessions.begin() as db:
        row = db.get(WanOperation, operation); row.cloud_job_id = "cloud-task"; row.status = "running"
    result = poll(case, operation)
    assert result["status"] == "succeeded"
    assert all(call[0] != "POST" for call in case.transport.calls)


def test_unknown_cloud_state_keeps_known_identity_for_get_reconciliation(gateway):
    case = gateway; operation = start(case); poll(case, operation); case.transport.state = "UNKNOWN"
    result = poll(case, operation)
    assert result["status"] == "running" and result["error"]["code"] == "wan_remote_reconciliation_required"
    case.transport.state = "SUCCEEDED"
    assert poll(case, operation)["status"] == "succeeded"
    assert [call[0] for call in case.transport.calls].count("POST") == 1


def test_cloud_failure_retains_remote_identity_and_unknown_charge(gateway):
    case = gateway; operation = start(case); poll(case, operation); case.transport.state = "FAILED"
    result = poll(case, operation)
    assert result["status"] == "failed" and "artifact_uri" not in result
    assert result["provider_job_id"] == operation and result["metrics"]["cloud_job_id"] == "cloud-task"
    assert result["cost"] == {"status": "unknown", "currency": "CNY", "bill_confirmed": False}
    assert poll(case, operation) == result and len(case.transport.calls) == 2


def test_media_probe_retry_reuses_verified_archive_and_never_repeats_model_post(gateway, monkeypatch):
    case = gateway; operation = start(case); poll(case, operation)
    original = case.probe.media_info

    def unavailable(_uri):
        raise ICEAPIError("ice_temporarily_unavailable", retryable=True)

    monkeypatch.setattr(case.probe, "media_info", unavailable)
    assert poll(case, operation)["status"] == "running" and len(case.store.objects) == 1
    downloads = len([call for call in case.transport.calls if "/api/v1/" not in call[1]])
    monkeypatch.setattr(case.probe, "media_info", original)
    result = poll(case, operation)
    assert result["status"] == "succeeded" and result["metrics"]["media"]["probe_request_id"] == "probe-request"
    assert len([call for call in case.transport.calls if "/api/v1/" not in call[1]]) == downloads == 1
    assert [call[0] for call in case.transport.calls].count("POST") == 1


def test_oversized_download_is_rejected_before_oss_archive(gateway):
    case = gateway; operation = start(case); poll(case, operation)
    case.settings.max_output_bytes = len(DATA) - 1
    result = poll(case, operation)
    assert result["status"] == "failed" and "artifact_uri" not in result and not case.store.objects


@pytest.mark.parametrize("url", ["https://127.0.0.1/result.mp4", "https://evil.example/result.mp4", "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com@evil.example/x"])
def test_untrusted_result_hosts_never_reach_download(gateway, url):
    case = gateway; operation = start(case); poll(case, operation)
    case.transport.result_url = url
    result = poll(case, operation)
    assert result["status"] == "failed" and not case.store.objects
    assert len(case.transport.calls) == 2


@pytest.mark.parametrize("bad", ["fps", "duration", "size", "probe_identity"])
def test_unverified_result_is_not_published_as_usable_media(gateway, bad, monkeypatch):
    case = gateway; operation = start(case); poll(case, operation)
    if bad == "fps":
        case.probe.fps = 0
    elif bad == "duration":
        case.probe.duration = 7
    elif bad == "size":
        case.transport.result_data = b"different-length"
    else:
        original = case.probe.media_info

        def no_identity(uri):
            value = original(uri); value.pop("request_id"); return value

        monkeypatch.setattr(case.probe, "media_info", no_identity)
    result = poll(case, operation)
    assert result["status"] == "failed" and "artifact_uri" not in result


def test_request_identity_conflict_quota_and_default_off(gateway):
    case = gateway
    assert case.client.post("/v1/wan", json=payload()).status_code == 401
    assert case.client.post("/v1/wan", json=payload(), headers=headers("other")).status_code == 409
    operation = start(case)
    changed = payload(); changed["inputs"]["prompt"] = "不同镜头"
    assert case.client.post("/v1/wan", json=changed, headers=headers()).status_code == 409
    case.settings.max_operations = 1
    assert case.client.post("/v1/wan", json=payload("request-2"), headers=headers("request-2")).status_code == 429
    assert case.client.get("/v1/wan/jobs/" + operation, headers=headers("wrong")).status_code == 404
    case.settings.enabled = False
    assert case.client.get("/v1/wan/jobs/" + operation, headers=headers()).status_code == 503
    assert not WanSettings().enabled and WanSettings().max_operations == 0


def test_invalid_model_duration_and_cross_tenant_inputs_rejected_before_creation(gateway):
    case = gateway
    values = []
    wrong_duration = payload(); wrong_duration["parameters"]["duration"] = 10; values.append(wrong_duration)
    wrong_model = payload(model="unregistered-model"); values.append(wrong_model)
    wrong_source = payload(model="wan2.2-i2v-plus")
    wrong_source["inputs"]["source_asset_uri"] = SOURCE.replace("tenant-1", "tenant-2"); values.append(wrong_source)
    no_consent = payload(capability="person_replacement", model="wan2.2-animate-mix")
    no_consent["inputs"] = {"source_asset_uri": SOURCE, "reference_asset_uris": [REFERENCE]}; values.append(no_consent)
    for value in values:
        assert case.client.post("/v1/wan", json=value, headers=headers()).status_code == 422
    with case.sessions() as db:
        assert db.get(WanQuota, "wan") is None
    assert not case.transport.calls


@pytest.mark.parametrize('capability,model', [('video_generation', 'wan2.2-i2v-plus'),
                                            ('person_replacement', 'wan2.2-animate-mix')])
def test_actual_http_provider_body_passes_context_to_gateway_without_sending_ids_upstream(gateway, capability, model):
    from app.services.provider_adapters import HTTPProvider
    from app.services.providers import ProviderRequest

    case = gateway
    inputs = {'prompt': '授权镜头' if capability == 'video_generation' else '',
        'reference_asset_ids': ['reference'], 'reference_asset_uris': [REFERENCE],
        'source_asset_id': None, 'avatar_profile_id': None, 'audio_asset_id': None,
        'quality': 'high', 'operation': 'video_generation' if capability == 'video_generation' else 'person_replace'}
    parameters = {'duration': 5, 'output_dir': '/local-only', 'output_path': '/local-only/out.mp4'}
    consent_ids = ()
    if capability == 'person_replacement':
        inputs.update(source_asset_id='source', source_asset_uri=SOURCE, avatar_profile_id='profile', consent_id='consent-1')
        consent_ids = ('consent-1',)
    else:
        parameters['negative_prompt'] = '不要模糊'
    adapter = HTTPProvider(name='aliyun-wan', base_url='https://gateway.example', capabilities=())
    request = ProviderRequest(request_id='request-1', capability=capability, tenant_id='tenant-1', inputs=inputs,
        parameters=parameters, consent_ids=consent_ids, model_id=model, model_version='wan2.2')
    body = adapter._request_body(request)
    normalized = WanRequest.model_validate_json(body)
    spec = _specification(normalized, case.settings, case.store)
    assert spec['control_context']['reference_asset_ids'] == ['reference']
    operation = start(case, json.loads(body)); poll(case, operation)
    upstream = json.loads(case.transport.calls[0][3])
    assert not set(upstream['input']) & {'source_asset_id', 'reference_asset_ids', 'avatar_profile_id', 'operation'}
    assert 'output_dir' not in upstream['parameters'] and 'output_path' not in upstream['parameters']
    if capability == 'video_generation':
        assert upstream['input']['negative_prompt'] == '不要模糊'


def test_unknown_context_and_conflicting_negative_prompts_are_rejected(gateway):
    case = gateway
    unknown = payload(); unknown['inputs']['unrecognized_instruction'] = 'do something else'
    assert case.client.post('/v1/wan', json=unknown, headers=headers()).status_code == 422
    conflicting = payload(); conflicting['inputs']['negative_prompt'] = 'one'; conflicting['parameters']['negative_prompt'] = 'two'
    assert case.client.post('/v1/wan', json=conflicting, headers=headers()).status_code == 422
    wrong_references = payload(); wrong_references['inputs']['reference_asset_uris'] = {}
    assert case.client.post('/v1/wan', json=wrong_references, headers=headers()).status_code == 422


@pytest.mark.parametrize("origin", ["http://dashscope.aliyuncs.com", "https://evil.example",
                                   "https://dashscope.aliyuncs.com@evil.example", "https://dashscope.aliyuncs.com/other"])
def test_settings_reject_untrusted_api_origins(origin):
    with pytest.raises(ValueError):
        WanSettings(base_url=origin)


def test_result_redirect_is_not_followed_or_given_api_credentials():
    calls = []

    def redirect(request, timeout, limit):
        calls.append(request)
        return 302, b""

    client = WanClient(WanSettings(enabled=True, api_key=KEY), transport=redirect)
    with pytest.raises(ValueError, match="wan_result_download_unconfirmed"):
        client.download_video(RESULT_URL)
    assert len(calls) == 1 and calls[0].method == "GET"
    assert not calls[0].has_header("Authorization")
