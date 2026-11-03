"""Cloud function input boundaries; decoder execution belongs to M1 cloud acceptance."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


BODY = b"authorized fixture video placeholder"
HOST = "video-ai-huayuan-cn-beijing.oss-cn-beijing.aliyuncs.com"
OPERATION = "a" * 64


@pytest.fixture
def function(monkeypatch):
    folder = Path(__file__).parents[1] / "cloud_functions" / "shot_detection"

    def load(name, path):
        specification = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    detector = load("fixture_shot_detector", folder / "detector.py")
    monkeypatch.setitem(sys.modules, "detector", detector)
    handler = load("fixture_shot_handler", folder / "index.py")
    monkeypatch.setenv("PROJECT_OSS_HOST", HOST)
    requests, decoded = [], []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response(BODY if request.method == "GET" else b"")

    def decode(path, **kwargs):
        assert path.read_bytes() == BODY
        decoded.append(path)
        return [{"start": 0, "end": 5}], {"decoded_frames": 150}

    monkeypatch.setattr(handler, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(handler, "detect_shots", decode)
    return SimpleNamespace(module=handler, requests=requests, decoded=decoded)


def event():
    return {"operation_id": OPERATION, "tenant_id": "tenant-1", "source_sha256": hashlib.sha256(BODY).hexdigest(),
        "source_size_bytes": len(BODY), "duration_seconds": 5,
        "source_url": f"https://{HOST}/tenants/tenant-1/uploads/asset/source?signature=ephemeral-source",
        "result_upload": {"url": f"https://{HOST}/tenants/tenant-1/analyses/{OPERATION}/shot-result.json?signature=ephemeral-target",
            "headers": {"Content-Type": "application/json", "x-oss-object-acl": "private", "x-oss-forbid-overwrite": "true"}}}


def test_function_checks_source_hash_writes_only_metadata_and_cleans_up(function):
    response = function.module.handler(json.dumps(event()).encode(), None)
    assert json.loads(response)["operation_id"] == OPERATION
    assert [request.method for request in function.requests] == ["GET", "PUT"]
    result = json.loads(function.requests[-1].data)
    assert result["clock"] == "decoded-frame-pts-seconds" and result["source_sha256"] == hashlib.sha256(BODY).hexdigest()
    assert "ephemeral" not in str(result) and "ephemeral" not in response
    assert not function.decoded[0].exists()


@pytest.mark.parametrize("mutation", ["source-host", "source-tenant", "target-tenant", "target-path", "public-acl", "overwrite"])
def test_function_rejects_unrelated_objects_and_public_or_mutable_results(function, mutation):
    payload = event()
    if mutation == "source-host":
        payload["source_url"] = "http://169.254.169.254/latest/meta-data/"
    elif mutation == "source-tenant":
        payload["source_url"] = payload["source_url"].replace("tenant-1", "other")
    elif mutation == "target-tenant":
        payload["result_upload"]["url"] = payload["result_upload"]["url"].replace("tenant-1", "other")
    elif mutation == "target-path":
        payload["result_upload"]["url"] = payload["result_upload"]["url"].replace("shot-result.json", "source")
    elif mutation == "public-acl":
        payload["result_upload"]["headers"]["x-oss-object-acl"] = "public-read"
    else:
        payload["result_upload"]["headers"]["x-oss-forbid-overwrite"] = "false"
    with pytest.raises(RuntimeError, match="^cloud_shot_analysis_failed$"):
        function.module.handler(json.dumps(payload).encode(), None)
    assert not function.requests and not function.decoded


def test_hash_mismatch_prevents_decode_and_result_upload_without_leaking_signatures(function):
    payload = event()
    payload["source_sha256"] = "b" * 64
    with pytest.raises(RuntimeError) as caught:
        function.module.handler(json.dumps(payload).encode(), None)
    assert not function.decoded and len(function.requests) == 1
    assert "ephemeral" not in str(caught.value)
    assert function.module.NoRedirect().redirect_request(None, None, 302, None, None, "https://other.example") is None
