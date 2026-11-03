import hashlib
import hmac
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.config import Settings


def _signed_headers(*, tenant_id: str, principal: str = "user-1", secret: str = "proxy-secret", issued_at: int | None = None) -> dict[str, str]:
    timestamp = str(issued_at or int(time.time()))
    canonical = f"{timestamp}\n{principal}\n{tenant_id}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {
        "X-Auth-Principal": principal,
        "X-Auth-Tenant-ID": tenant_id,
        "X-Auth-Issued-At": timestamp,
        "X-Auth-Signature": signature,
    }


def _tenant(client: TestClient) -> dict:
    response = client.post("/api/v1/tenants", json={"slug": f"auth-{uuid4().hex[:8]}", "name": "Auth"})
    assert response.status_code == 201
    return response.json()


def test_development_header_remains_the_default(client: TestClient):
    tenant = _tenant(client)
    headers = {"X-Tenant-ID": tenant["id"]}
    response = client.post("/api/v1/projects", headers=headers, json={"name": "Development"})
    assert response.status_code == 201
    assert response.json()["tenant_id"] == tenant["id"]


def test_trusted_proxy_requires_signed_claims(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    tenant = _tenant(client)
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: Settings(auth_mode="trusted_proxy", trusted_proxy_shared_secret="proxy-secret"),
    )
    response = client.post("/api/v1/projects", json={"name": "Missing identity"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_identity_claim"

    response = client.post(
        "/api/v1/projects",
        headers={"X-Tenant-ID": tenant["id"]},
        json={"name": "Spoofed"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "client_tenant_header_forbidden"


def test_trusted_proxy_maps_signed_tenant_and_rejects_bad_signature(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    tenant = _tenant(client)
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: Settings(auth_mode="trusted_proxy", trusted_proxy_shared_secret="proxy-secret"),
    )
    headers = _signed_headers(tenant_id=tenant["id"])
    response = client.post("/api/v1/projects", headers=headers, json={"name": "Trusted"})
    assert response.status_code == 201
    assert response.json()["tenant_id"] == tenant["id"]

    bad = {**headers, "X-Auth-Signature": "0" * 64}
    response = client.post("/api/v1/projects", headers=bad, json={"name": "Bad"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_identity_signature"


def test_trusted_proxy_rejects_comma_joined_identity_claims(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    tenant = _tenant(client)
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: Settings(auth_mode="trusted_proxy", trusted_proxy_shared_secret="proxy-secret"),
    )
    headers = _signed_headers(tenant_id=tenant["id"], principal="user-1,spoofed")
    response = client.post("/api/v1/projects", headers=headers, json={"name": "Ambiguous identity"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_identity_claim"


def test_url_tenant_endpoints_require_the_same_authenticated_tenant(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    first = _tenant(client)
    second = _tenant(client)
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: Settings(auth_mode="trusted_proxy", trusted_proxy_shared_secret="proxy-secret"),
    )
    response = client.get(f"/api/v1/tenants/{first['id']}/budget", headers=_signed_headers(tenant_id=second["id"]))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "tenant_claim_mismatch"

    response = client.get(f"/api/v1/tenants/{first['id']}/budget", headers=_signed_headers(tenant_id=first["id"]))
    assert response.status_code == 200
    assert response.json()["tenant_id"] == first["id"]


def test_trusted_proxy_hides_public_tenant_provisioning(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: Settings(auth_mode="trusted_proxy", trusted_proxy_shared_secret="proxy-secret"),
    )
    response = client.post(
        "/api/v1/tenants",
        headers=_signed_headers(tenant_id="bootstrap-tenant"),
        json={"slug": "not-public", "name": "No"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "tenant_provisioning_requires_control_plane"


def test_trusted_proxy_settings_fail_closed_without_secret():
    with pytest.raises(ValueError, match="TRUSTED_PROXY_SHARED_SECRET"):
        Settings(auth_mode="trusted_proxy")
