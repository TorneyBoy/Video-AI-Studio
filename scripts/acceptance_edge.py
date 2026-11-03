"""SSH-only M1 workbench boundary; this is not the public C01 login service.

Bind loopback unconditionally. An authorized SSH tunnel establishes access to
one server-provisioned tenant. Browser identity headers are never forwarded.
No cloud credentials, internal mutation keys or media bytes pass this edge.
"""

from hashlib import sha256
import hmac
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import time
from urllib.parse import urlsplit


MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
PORT = 8080
HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
ORIGINS = {"http://" + host for host in HOSTS}
STATIC_PATHS = {"/", "/app.js", "/styles.css", "/share", "/share.js", "/share.css", "/healthz", "/readyz"}
STATIC_PATHS |= {"/web/" + name + ".js" for name in
                 ("state", "network", "projects", "tasks", "assets", "workspace", "editor", "clip-editor", "subtitle-editor", "layer-editor", "reviews", "delivery", "billing", "preparation", "batch-form", "batches", "batch-runs", "batch-delivery", "account", "uploads", "vendor-sha256")}


class Edge(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VideoAIPrivateAcceptance"

    def log_message(self, *_args):
        # Signed URLs and request bodies must not enter an access log.
        pass

    def send_problem(self, status):
        body = b'{"detail":{"code":"private_acceptance_request_rejected"}}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def proxy(self):
        target = urlsplit(self.path)
        host_values = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if (len(host_values) != 1 or host_values[0] not in HOSTS or len(origins) > 1
                or (origins and origins[0] not in ORIGINS)
                or target.scheme or target.netloc or target.fragment
                or not self.path.startswith("/") or self.path.startswith("//")
                or "%" in target.path or "\\" in target.path
                or any(piece in {".", ".."} for piece in target.path.split("/"))):
            return self.send_problem(403)
        path = target.path
        if (path not in STATIC_PATHS and not path.startswith("/api/v1/")
                or "/internal" in path or "/callback" in path or path.startswith("/api/v1/tenants")):
            return self.send_problem(404)
        if self.headers.get("Transfer-Encoding"):
            return self.send_problem(400)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
            return self.send_problem(400)
        length = int(lengths[0]) if lengths else 0
        if length > MAX_BODY:
            return self.send_problem(413)
        if self.command in {"POST", "PUT", "PATCH", "DELETE"} and length:
            if self.headers.get_content_type() != "application/json":
                return self.send_problem(415)
        body = self.rfile.read(length) if length else None
        if body is not None and len(body) != length:
            return self.send_problem(400)
        issued = str(int(time.time()))
        principal, tenant, secret = self.server.identity
        signature = hmac.new(secret.encode(), f"{issued}\n{principal}\n{tenant}".encode(), sha256).hexdigest()
        headers = {"X-Auth-Principal": principal, "X-Auth-Tenant-ID": tenant,
                   "X-Auth-Issued-At": issued, "X-Auth-Signature": signature,
                   "Connection": "close"}
        for name in ("Accept", "Content-Type", "Idempotency-Key"):
            values = self.headers.get_all(name, [])
            if len(values) > 1:
                return self.send_problem(400)
            if values:
                headers[name] = values[0]
        upstream = HTTPConnection("127.0.0.1", 8000, timeout=120)
        try:
            upstream.request(self.command, self.path, body, headers)
            response = upstream.getresponse()
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                return self.send_problem(502)
            self.send_response(response.status)
            for name in ("Content-Type", "Content-Disposition"):
                value = response.getheader(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            self.close_connection = True
        except (OSError, ValueError):
            self.send_problem(502)
        finally:
            upstream.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = proxy

    def setup(self):
        super().setup()
        self.connection.settimeout(30)


def main():
    tenant = os.environ["ACCEPTANCE_TENANT_ID"]
    principal = os.environ["ACCEPTANCE_PRINCIPAL_ID"]
    secret = os.environ["TRUSTED_PROXY_SHARED_SECRET"]
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in (tenant, principal)) or len(secret) < 32:
        raise ValueError("Private acceptance identity configuration is invalid")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Edge)
    server.identity = (principal, tenant, secret)
    server.serve_forever()


if __name__ == "__main__":
    main()
