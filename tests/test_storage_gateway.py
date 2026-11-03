"""Storage contract tests use an in-process cloud substitute, never a cloud account."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app import models
from app.api import storage
from app.config import Settings
from app.database import get_db
from app.main import app
from app.services.storage_attestation import verify_attestation
from app.services.storage_gateway import StorageGateway, StorageGatewayError, configured_storage_gateway
from media_gateway.main import GatewaySettings, UploadIntent, create_app
from media_gateway.oss import OSSStore, ObjectStoreError


SECRET = "test-attestation-secret-32-characters"
KEY = "test-gateway-api-key-32-characters"
DATA = b"authorized fixture bytes; not a playable video"
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.mark.parametrize('status,code,expected', [(409, 'object_not_uploaded', 409), (410, 'upload_expired', 410),
    (422, 'object_hash_mismatch', 422), (422, 'object_size_mismatch', 422), (403, 'object_not_uploaded', 502),
    (503, {'not': 'a string'}, 503)])
def test_control_preserves_only_fixed_recovery_errors_without_upstream_secrets(status, code, expected):
    settings = Settings(storage_gateway_url='https://gateway.example', storage_gateway_api_key=KEY)
    raw = json.dumps({'code': code, 'message': 'credential=do-not-echo', 'upload_url': 'https://cloud.example/?secret=value'}).encode()
    adapter = StorageGateway(settings, transport=lambda request, timeout: (status, raw))
    with pytest.raises(StorageGatewayError) as failure:
        adapter.verify_upload(asset_id='source', tenant_id='owner', object_uri='oss://fixture/source')
    assert failure.value.status_code == expected
    assert 'do-not-echo' not in str(failure.value) and 'secret=value' not in str(failure.value)
    assert failure.value.code == (code if status == expected and isinstance(code, str) else 'storage_gateway_request_failed')


class FakeOSSClient:
    def __init__(self):
        self.data = DATA
        self.versioning = None
        self.observations = 0
        self.etag = '"immutable-etag"'

    def get_bucket_versioning(self, request):
        return SimpleNamespace(version_status=self.versioning)

    def head_object(self, request):
        return SimpleNamespace(content_length=len(self.data), etag=self.etag)

    def get_object(self, request):
        assert request.if_match == self.etag
        self.observations += 1
        return SimpleNamespace(body=io.BytesIO(self.data), etag=self.etag, request_id="fixture-request")

    def copy_object(self, request):
        assert request.forbid_overwrite is True and request.acl == "private"
        assert request.if_match == self.etag
        return SimpleNamespace()

    def presign(self, request, **kwargs):
        assert kwargs["expires"] <= timedelta(minutes=5)
        return SimpleNamespace(url=f"https://fixture-bucket.oss-cn-beijing.aliyuncs.com/{request.key}?fixture_signature=ephemeral",
                               expiration=datetime.now(timezone.utc) + kwargs["expires"])


@pytest.fixture
def gateway(tmp_path):
    import alibabacloud_oss_v2 as oss

    fake = FakeOSSClient()
    store = OSSStore(bucket="fixture-bucket", region="cn-beijing", client=fake,
                     credentials_provider=oss.credentials.StaticCredentialsProvider("test-id", "test-secret"))
    settings = GatewaySettings(api_key=KEY, attestation_secret=SECRET, oss_bucket="fixture-bucket",
                               oss_region="cn-beijing", database_url=f"sqlite:///{tmp_path / 'gateway.db'}")
    gateway_app = create_app(settings, store)
    with TestClient(gateway_app, raise_server_exceptions=False) as remote:
        yield remote, gateway_app, store, fake
    gateway_app.state.engine.dispose()


def _specification():
    return {"asset_id": "asset-1", "tenant_id": "tenant-1", "name": "source.mp4", "content_type": "video/mp4",
            "size_bytes": len(DATA), "sha256": SHA, "ttl_seconds": 3600}


def _headers():
    return {"Authorization": "Bearer " + KEY}


def test_real_sdk_stream_iterator_hashes_and_closes_without_unbounded_read(gateway):
    from alibabacloud_oss_v2.io_utils import StreamBodyReader

    _, _, store, fake = gateway

    class Response:
        closed = False

        def read(self):
            pytest.fail("SDK read() is unbounded and must not be called")

        def iter_bytes(self, *, block_size):
            assert block_size == 64 * 1024
            yield DATA[:8]
            yield DATA[8:]

        def close(self):
            self.closed = True

    response = Response()
    fake.get_object = lambda request: SimpleNamespace(body=StreamBodyReader(response), etag=fake.etag, request_id="sdk-stream")
    result = store.observe(key="tenants/tenant-1/source", expected_size=len(DATA), expected_hash=SHA)
    assert result["sha256"] == SHA and response.closed
    response.closed = False
    with pytest.raises(ObjectStoreError) as error:
        store._read_bounded(StreamBodyReader(response), 9)
    assert error.value.code == "object_size_limit" and response.closed


def test_oss_post_policy_binds_size_key_acl_and_overwrite_and_never_secret(gateway):
    remote, _, _, _ = gateway
    grant = remote.post("/v1/uploads", json=_specification(), headers=_headers())
    assert grant.status_code == 200
    result = grant.json()
    fields = result["upload_fields"]
    policy = json.loads(base64.b64decode(fields["policy"]))
    assert ["content-length-range", len(DATA), len(DATA)] in policy["conditions"]
    assert {"key": "tenants/tenant-1/uploads/asset-1/source"} in policy["conditions"]
    assert {"x-oss-forbid-overwrite": "true"} in policy["conditions"]
    assert {"x-oss-object-acl": "private"} in policy["conditions"]
    assert fields["x-oss-signature-version"] == "OSS4-HMAC-SHA256"
    assert "test-secret" not in grant.text
    assert result["upload_method"] == "POST" and result["upload_file_field"] == "file"


def test_gateway_observes_bytes_then_caches_integrity_not_client_metadata(gateway):
    remote, gateway_app, _, fake = gateway
    grant = remote.post("/v1/uploads", json=_specification(), headers=_headers()).json()
    verify = {"asset_id": "asset-1", "tenant_id": "tenant-1", "object_uri": grant["object_uri"]}
    first = remote.post("/v1/uploads/asset-1/verify", json=verify, headers=_headers())
    assert first.status_code == 200
    value, proof = first.json(), first.json()["attestation"]
    assert value["sha256"] == SHA and value["size_bytes"] == len(DATA)
    verify_attestation(SECRET, proof["signature"], asset_id="asset-1", object_uri=grant["object_uri"],
                       size_bytes=len(DATA), sha256=SHA, nonce=proof["nonce"],
                       expires_at=datetime.fromisoformat(proof["expires_at"]))
    assert remote.post("/v1/uploads/asset-1/verify", json=verify, headers=_headers()).status_code == 200
    assert fake.observations == 1
    with gateway_app.state.sessions() as db:
        assert db.get(UploadIntent, "asset-1").verified["sha256"] == SHA


@pytest.mark.parametrize("versioning", ["Enabled", "Suspended"])
def test_gateway_rejects_buckets_where_forbid_overwrite_is_ignored(gateway, versioning):
    remote, _, _, fake = gateway
    fake.versioning = versioning
    response = remote.post("/v1/uploads", json=_specification(), headers=_headers())
    assert response.status_code == 503
    assert response.json()["code"] == "bucket_versioning_incompatible"


def test_gateway_identity_expiry_hash_auth_and_size_fail_closed(gateway):
    remote, gateway_app, _, fake = gateway
    assert remote.post("/v1/uploads", json=_specification()).status_code == 401
    grant = remote.post("/v1/uploads", json=_specification(), headers=_headers()).json()
    changed = {**_specification(), "sha256": "0" * 64}
    assert remote.post("/v1/uploads", json=changed, headers=_headers()).status_code == 409
    verify = {"asset_id": "asset-1", "tenant_id": "tenant-1", "object_uri": grant["object_uri"]}
    assert remote.post("/v1/uploads/asset-1/verify", json={**verify, "tenant_id": "other"}, headers=_headers()).status_code == 404
    fake.data = b"x" * len(DATA)
    assert remote.post("/v1/uploads/asset-1/verify", json=verify, headers=_headers()).json()["code"] == "object_hash_mismatch"
    fake.data = b"wrong-size"
    assert remote.post("/v1/uploads/asset-1/verify", json=verify, headers=_headers()).json()["code"] == "object_size_mismatch"
    with gateway_app.state.sessions.begin() as db:
        db.get(UploadIntent, "asset-1").expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert remote.post("/v1/uploads/asset-1/verify", json=verify, headers=_headers()).status_code == 410
    assert remote.post("/v1/uploads", json=_specification(), headers=_headers()).status_code == 200


def test_oss_access_rejects_other_tenant_encoded_traversal_and_arbitrary_hosts(gateway):
    _, _, store, _ = gateway
    for uri in ["oss://fixture-bucket/tenants/other/file", "oss://fixture-bucket/tenants/tenant-1/%2e%2e/file",
                "https://untrusted.example/tenants/tenant-1/file", "oss://fixture-bucket/tenants/tenant-1/file?signature=x"]:
        with pytest.raises(ObjectStoreError):
            store.key_from_uri(uri, "tenant-1")


def test_control_plane_upload_round_trip_idempotency_and_no_credential_persistence(client, gateway, monkeypatch):
    remote, _, _, fake = gateway
    settings = Settings(storage_gateway_url="https://gateway.example", storage_gateway_api_key=KEY,
                        storage_attestation_secret=SECRET,
                        storage_upload_hosts=("fixture-bucket.oss-cn-beijing.aliyuncs.com",))
    requests = []

    def transport(request, timeout):
        requests.append(request)
        response = remote.post(request.full_url.replace("https://gateway.example", ""), content=request.data,
                               headers={"Authorization": request.get_header("Authorization"), "Content-Type": "application/json"})
        return response.status_code, response.content

    adapter = StorageGateway(settings, transport=transport)
    app.dependency_overrides[configured_storage_gateway] = lambda: adapter
    monkeypatch.setattr(storage, "get_settings", lambda: settings)
    tenant = client.post("/api/v1/tenants", json={"slug": "storage-test", "name": "Storage"}).json()["id"]
    headers = {"X-Tenant-ID": tenant, "Idempotency-Key": "one-upload"}
    spec = {key: value for key, value in _specification().items() if key not in {"asset_id", "tenant_id", "ttl_seconds"}}
    first = client.post("/api/v1/assets/upload-intents", json=spec, headers=headers)
    assert first.status_code == 201
    value = first.json()
    again = client.post("/api/v1/assets/upload-intents", json=spec, headers=headers)
    assert again.json()["asset_id"] == value["asset_id"]
    assert client.post("/api/v1/assets/upload-intents", json={**spec, "name": "different.mp4"}, headers=headers).status_code == 409
    asset = value["asset_id"]
    assert client.post(f"/api/v1/assets/{asset}/analyze", headers=headers, json={}).status_code == 409
    assert client.post(value["complete_uri"], headers={"X-Tenant-ID": "other"}).status_code == 404
    complete = client.post(value["complete_uri"], headers=headers)
    assert complete.status_code == 200
    assert complete.json()["metadata"]["storage"]["status"] == "ready"
    assert client.post(value["complete_uri"], headers=headers).status_code == 200
    assert fake.observations == 1
    assert client.post("/api/v1/assets/upload-intents", json=spec, headers=headers).json()["upload_url"] is None
    session_dependency = app.dependency_overrides[get_db]()
    db = next(session_dependency)
    try:
        assert db.scalar(select(func.count()).select_from(models.StorageUploadIntent)) == 1
        persisted = str(db.scalar(select(models.StorageUploadIntent)).request_json)
        persisted += str(db.get(models.Asset, asset).metadata_json)
        assert "x-oss-signature" not in persisted and "test-secret" not in persisted
    finally:
        session_dependency.close()
    assert all(len(request.data) < 2000 for request in requests)


def test_storage_adapter_rejects_untrusted_redirect_error_bodies_and_grants():
    settings = Settings(storage_gateway_url="https://gateway.example", storage_gateway_api_key=KEY,
                        storage_upload_hosts=("allowed.example",))
    rejected = StorageGateway(settings, transport=lambda req, timeout: (403, b"secret-token-in-upstream-body"))
    with pytest.raises(StorageGatewayError) as failure:
        rejected.upload_grant(asset_id="a", tenant_id="t", project_id=None, specification={})
    assert "secret-token" not in str(failure.value)
    wrong = {"asset_id": "a", "object_uri": "oss://bucket/key", "upload_url": "https://untrusted.example/secret",
             "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}
    adapter = StorageGateway(settings, transport=lambda req, timeout: (200, json.dumps(wrong).encode()))
    with pytest.raises(StorageGatewayError, match="domain"):
        adapter.upload_grant(asset_id="a", tenant_id="t", project_id=None, specification={})
