"""FC event handler. Deploy this module on FC only, never on the control ECS.

The function receives two short lived, object-specific signatures. It has no
RAM execution role and does not write payloads, signatures or media to logs.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from detector import DETECTOR_VERSION, detect_shots


MAX_BYTES = 100 * 1024 * 1024
MAX_SECONDS = 30


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(value, *, host, path_prefix=None, exact_path=None):
    parsed = urlparse(value)
    path = unquote(parsed.path)
    if (parsed.scheme != "https" or parsed.netloc != host or parsed.username or parsed.password or parsed.fragment
            or not parsed.query or any(part in {".", ".."} for part in path.split("/"))
            or path_prefix is not None and not path.startswith(path_prefix)
            or exact_path is not None and path != exact_path):
        raise ValueError("Object signature is outside the project namespace")
    return value


def handler(event, context):
    try:
        if not isinstance(event, (str, bytes)) or len(event) > 65536:
            raise ValueError("Invalid event size")
        payload = json.loads(event)
        operation, tenant, digest = payload["operation_id"], payload["tenant_id"], payload["source_sha256"]
        if (not re.fullmatch(r"[0-9a-f]{64}", operation) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Invalid job identity")
        size, duration = payload["source_size_bytes"], payload["duration_seconds"]
        if (not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BYTES
                or isinstance(duration, bool) or not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS):
            raise ValueError("Input exceeds the trial profile")
        host = os.environ["PROJECT_OSS_HOST"]
        source = validate_url(payload["source_url"], host=host, path_prefix=f"/tenants/{tenant}/")
        target = payload["result_upload"]
        target_url = validate_url(target["url"], host=host, exact_path=f"/tenants/{tenant}/analyses/{operation}/shot-result.json")
        headers = target["headers"]
        if not isinstance(headers, dict) or len(headers) > 20 or any(not isinstance(v, str) for v in headers.values()):
            raise ValueError("Invalid result signature headers")
        normalized_headers = {str(k).lower(): v for k, v in headers.items()}
        if (normalized_headers.get("x-oss-forbid-overwrite") != "true"
                or normalized_headers.get("x-oss-object-acl") != "private"
                or normalized_headers.get("content-type") != "application/json"):
            raise ValueError("Result signature must prohibit overwrite and public access")
        opener, started = build_opener(NoRedirect()), time.monotonic()
        with tempfile.TemporaryDirectory(prefix="shot-") as folder:
            path, observed, total = Path(folder) / "source.media", hashlib.sha256(), 0
            with opener.open(Request(source, method="GET"), timeout=20) as response, path.open("wb") as output:
                if response.status != 200:
                    raise ValueError("Source download not confirmed")
                while True:
                    chunk = response.read(min(1024 * 1024, size - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size or time.monotonic() - started > 60:
                        raise ValueError("Source download exceeded its limit")
                    output.write(chunk)
                    observed.update(chunk)
            if total != size or observed.hexdigest() != digest:
                raise ValueError("Source object differs from the submitted integrity proof")
            shots, measurements = detect_shots(path, expected_duration=duration, deadline=started + 150)
        document = {"schema_version": "shot-boundaries/1", "detector_version": DETECTOR_VERSION,
            "operation_id": operation, "source_sha256": digest, "clock": "decoded-frame-pts-seconds",
            "shots": shots, "measurements": measurements}
        body = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > 1024 * 1024:
            raise ValueError("Analysis result exceeds its metadata limit")
        try:
            with opener.open(Request(target_url, data=body, headers=headers, method="PUT"), timeout=20) as response:
                if response.status != 200:
                    raise ValueError("Result upload not confirmed")
        except HTTPError as exc:
            # A duplicated invocation can only encounter this same immutable
            # result key. The gateway validates its source and operation IDs.
            if exc.code != 409:
                raise
        return json.dumps({"status": "completed", "operation_id": operation, "detector_version": DETECTOR_VERSION})
    except Exception:
        # Do not serialize urllib/SDK exceptions; they may contain signatures.
        raise RuntimeError("cloud_shot_analysis_failed") from None
