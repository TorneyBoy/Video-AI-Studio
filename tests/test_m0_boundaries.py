"""Public write boundaries and same-origin console contract."""

import hashlib
import hmac
import time
from uuid import uuid4

import pytest

from app.api import deps, routers
from app.config import Settings


def identity(tenant_id):
    issued = str(int(time.time()))
    canonical = f"{issued}\nuser\n{tenant_id}".encode()
    return {"X-Auth-Principal": "user", "X-Auth-Tenant-ID": tenant_id,
            "X-Auth-Issued-At": issued,
            "X-Auth-Signature": hmac.new(b"proxy-secret", canonical, hashlib.sha256).hexdigest()}


def tenant(client):
    return client.post("/api/v1/tenants", json={"slug": uuid4().hex, "name": "M0"}).json()


def test_console_and_api_share_an_allowlisted_origin(client):
    assert "text/html" in client.get("/").headers["content-type"]
    assert "const API_BASE = '/api/v1'" in client.get("/web/state.js").text
    assert client.get("/web/manifest.json").status_code == 404
    assert client.get("/styles.css").status_code == 200
    assert client.get("/.env").status_code == 404
    assert client.get("/app/config.py").status_code == 404
    assert client.get("/healthz").status_code == 200
    item = tenant(client)
    assert client.post("/api/v1/projects", headers={"X-Tenant-ID": item["id"]}, json={"name": "Same origin"}).status_code == 201


def test_external_storage_cannot_be_bypassed_by_generic_or_byte_upload(client, monkeypatch):
    item = tenant(client)
    headers = {"X-Tenant-ID": item["id"]}
    monkeypatch.setattr(routers.settings, "asset_storage_mode", "external")
    assert client.post("/api/v1/assets", headers=headers, json={
        "name": "spoof", "kind": "video", "uri": "https://cdn.example/file.mp4",
        "metadata": {"upload": {"status": "ready"}},
    }).status_code == 409
    assert client.post("/api/v1/assets/upload-session", headers=headers, json={
        "name": "local.mp4", "content_type": "video/mp4", "size_bytes": 1,
    }).status_code == 409
    assert client.post("/api/v1/assets/not-an-asset/content", headers=headers,
                       files={"file": ("x.mp4", b"x", "video/mp4")}).status_code == 409


@pytest.mark.parametrize("path,body", [
    ("/jobs", {"type": "video.generate"}),
    ("/artifacts", {"kind": "video", "uri": "https://cdn.example/fake.mp4"}),
    ("/jobs/unknown/steps", {"name": "spoof"}),
    ("/jobs/unknown/status", {"status": "succeeded"}),
    ("/workflows/unknown/claim", {"worker_id": "spoof"}),
])
def test_signed_user_identity_does_not_grant_internal_execution(client, monkeypatch, path, body):
    item = tenant(client)
    monkeypatch.setattr(deps, "get_settings", lambda: Settings(
        auth_mode="trusted_proxy", trusted_proxy_shared_secret="proxy-secret", internal_api_secret="ops-secret"))
    method = client.patch if path.endswith("/status") else client.post
    response = method("/api/v1" + path, headers=identity(item["id"]), json=body)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "internal_execution_required"


def test_production_configuration_rejects_development_boundaries():
    with pytest.raises(ValueError, match="production requires"):
        Settings(app_environment="production")
    valid = Settings(app_environment="production", auth_mode="trusted_proxy",
                     trusted_proxy_shared_secret="proxy-secret", database_url="postgresql+psycopg://host/db",
                     storage_attestation_secret="storage-secret", provider_webhook_require_request_id=True)
    assert valid.asset_storage_mode == "external"
