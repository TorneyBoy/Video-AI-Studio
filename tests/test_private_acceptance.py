"""Planned M1 boundary checks; no supplier calls or media processing."""

import hashlib
import hmac
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import threading
import time

import pytest

from app.api import deps
from app.config import Settings
from scripts import acceptance_edge


def test_verified_session_context_ignores_browser_storage(client, monkeypatch):
    monkeypatch.setattr(deps, "get_settings", lambda: Settings(
        auth_mode="trusted_proxy", trusted_proxy_shared_secret="s" * 32))
    issued = str(int(time.time()))
    headers = {"X-Auth-Principal": "operator", "X-Auth-Tenant-ID": "tenant-a",
               "X-Auth-Issued-At": issued,
               "X-Auth-Signature": hmac.new(b"s" * 32, f"{issued}\noperator\ntenant-a".encode(), hashlib.sha256).hexdigest()}
    # Session reports the same verified mode used by authenticate_request.
    from app.api import routers
    monkeypatch.setattr(routers, "get_settings", deps.get_settings)
    assert client.get("/api/v1/session", headers=headers).json() == {
        "auth_mode": "trusted_proxy", "principal_id": "operator", "tenant_id": "tenant-a"}
    assert client.get("/api/v1/session", headers={**headers, "X-Tenant-ID": "tenant-b"}).status_code == 400
    assert client.get("/api/v1/session").status_code == 401


@pytest.fixture
def private_edge(monkeypatch):
    observed = []

    class Upstream:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("127.0.0.1", 8000)

        def request(self, method, path, body, headers):
            observed.append((method, path, body, headers))

        def getresponse(self):
            return self

        status = 200

        def read(self, _limit):
            return b'{"ok":true}'

        def getheader(self, name):
            return "application/json" if name == "Content-Type" else None

        def close(self):
            pass

    monkeypatch.setattr(acceptance_edge, "HTTPConnection", Upstream)
    server = ThreadingHTTPServer(("127.0.0.1", 0), acceptance_edge.Edge)
    server.identity = ("operator", "tenant-a", "s" * 32)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, *, method="GET", headers=None, body=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(method, path, body, {"Host": "localhost:8080", **(headers or {})})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    try:
        yield request, observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_edge_strips_all_supplied_identity_and_internal_keys(private_edge):
    request, observed = private_edge
    assert request("/api/v1/session", headers={
        "X-Tenant-ID": "forged", "X-Auth-Tenant-ID": "forged", "X-Internal-API-Key": "forged",
        "Authorization": "Bearer attacker", "Cookie": "attacker=1"})[0] == 200
    headers = observed[0][3]
    assert headers["X-Auth-Tenant-ID"] == "tenant-a"
    assert not ({"X-Tenant-ID", "Authorization", "Cookie", "X-Internal-API-Key"} & headers.keys())
    canonical = f"{headers['X-Auth-Issued-At']}\noperator\ntenant-a".encode()
    assert headers["X-Auth-Signature"] == hmac.new(b"s" * 32, canonical, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("path,headers,status", [
    ("/api/v1/projects", {"Host": "attacker.example"}, 403),
    ("/api/v1/projects", {"Origin": "https://attacker.example"}, 403),
    ("/api/v1/tenants", {}, 404),
    ("/api/v1/internal/archive", {}, 404),
    ("/api/v1/jobs/a/callback", {}, 404),
    ("/api/v1/%2e%2e/internal", {}, 403),
    ("/.env.m1", {}, 404),
    ("/metrics", {}, 404),
])
def test_edge_rejects_cross_origin_admin_and_non_workbench_requests(private_edge, path, headers, status):
    request, observed = private_edge
    assert request(path, headers=headers)[0] == status
    assert not observed


def test_edge_forwards_json_and_idempotency_but_rejects_media_bytes(private_edge):
    request, observed = private_edge
    headers = {"Content-Type": "application/json", "Idempotency-Key": "one", "Origin": "http://localhost:8080"}
    assert request("/api/v1/projects", method="POST", headers=headers, body=b'{}')[0] == 200
    assert observed[0][2] == b'{}'
    assert observed[0][3]["Idempotency-Key"] == "one"
    assert request("/api/v1/assets/a/content", method="POST", headers={"Content-Type": "video/mp4"}, body=b'video')[0] == 415
    assert len(observed) == 1
