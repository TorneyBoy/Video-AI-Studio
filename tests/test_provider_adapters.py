import sys
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.services.provider_adapters import CommandProvider, HTTPProvider
from app.services.provider_registry import ConfiguredProviderRegistry, ProviderRegistryError
from app.services.providers import (
    ModelSnapshot,
    ProviderCapability,
    ProviderExecutionError,
    ProviderExecutionPending,
    ProviderRequest,
)


def capability():
    return (ProviderCapability("video_generation", ("json",), ("video",), commercial_allowed=True),)


def request(**parameters):
    return ProviderRequest(
        request_id="req-1",
        capability="video_generation",
        inputs={"prompt": "a red kite"},
        parameters=parameters,
        tenant_id="tenant-1",
        consent_ids=("consent-1",),
    )


def test_http_provider_normalizes_result_and_does_not_send_local_output_dir():
    captured = {}

    def transport(http_request, timeout):
        captured["url"] = http_request.full_url
        captured["body"] = http_request.data
        captured["timeout"] = timeout
        return 200, b'{"artifact_uri":"https://cdn.example/out.mp4","artifact_type":"video","model":{"model_id":"t2v","version":"7"}}'

    provider = HTTPProvider(
        name="remote-video",
        base_url="https://provider.example",
        capabilities=capability(),
        headers={"Authorization": "Bearer secret"},
        transport=transport,
    )
    result = provider.execute(request(output_dir="/tmp/private", quality="high"))
    assert captured["url"] == "https://provider.example/v1/generate"
    assert b"output_dir" not in captured["body"]
    assert captured["timeout"] == 120.0
    assert result.artifact_uri == "https://cdn.example/out.mp4"
    assert result.model.model_id == "t2v"
    assert result.model.version == "7"


def test_http_provider_classifies_rate_limit_as_retryable():
    provider = HTTPProvider(
        name="remote-video",
        base_url="https://provider.example",
        capabilities=capability(),
        transport=lambda _request, _timeout: (429, b"busy"),
    )
    with pytest.raises(ProviderExecutionError) as error:
        provider.execute(request())
    assert error.value.code == "provider_http_error"
    assert error.value.retryable is True
    assert error.value.details == {"http_status": 429}


def test_http_provider_redacts_secret_fields_in_model_snapshot():
    provider = HTTPProvider(
        name="remote-video",
        base_url="https://provider.example",
        capabilities=capability(),
        transport=lambda _request, _timeout: (
            200,
            b'{"artifact_uri":"https://cdn.example/out.mp4",'
            b'"model":{"model_id":"t2v","version":"7",'
            b'"license_snapshot":{"weights_license":"commercial","api_key":"do-not-persist"}}}',
        ),
    )
    result = provider.execute(request())
    assert result.model.license_snapshot["api_key"] == "[REDACTED]"


def test_http_provider_rejects_worker_local_artifact_uri():
    provider = HTTPProvider(
        name="remote-video",
        base_url="https://provider.example",
        capabilities=capability(),
        transport=lambda _request, _timeout: (200, b'{"artifact_uri":"file:///tmp/out.mp4"}'),
    )
    with pytest.raises(ProviderExecutionError) as error:
        provider.execute(request())
    assert error.value.code == "provider_invalid_artifact_uri"


def test_http_provider_polls_hosted_job_and_preserves_usage_cost_and_inline_result():
    calls = []

    def transport(http_request, _timeout):
        calls.append((http_request.method, http_request.full_url))
        if http_request.method == "POST":
            return 202, b'{"task_id":"task-7","status":"queued"}'
        if len(calls) == 2:
            return 200, b'{"task_id":"task-7","status":"processing"}'
        return 200, (
            b'{"task_id":"task-7","status":"succeeded",'
            b'"artifact_uri":"https://cdn.example/out.mp4",'
            b'"analysis":{"shots":[{"start":0,"end":1}]},'
            b'"usage":{"seconds":1.2},"cost":{"amount":0.04,"currency":"USD"}}'
        )

    provider = HTTPProvider(
        name="hosted-video",
        base_url="https://provider.example",
        capabilities=capability(),
        poll_path="/v1/tasks/{provider_job_id}",
        poll_interval_seconds=0,
        max_poll_attempts=3,
        transport=transport,
    )
    result = provider.execute(request())
    assert calls == [
        ("POST", "https://provider.example/v1/generate"),
        ("GET", "https://provider.example/v1/tasks/task-7"),
        ("GET", "https://provider.example/v1/tasks/task-7"),
    ]
    assert result.provenance["provider_job_id"] == "task-7"
    assert result.metrics["analysis"]["shots"][0]["end"] == 1
    assert result.usage == {"seconds": 1.2}
    assert result.cost == {"amount": 0.04, "currency": "USD"}


def test_http_provider_returns_durable_pending_when_poll_window_is_exhausted():
    provider = HTTPProvider(
        name="hosted-video",
        base_url="https://provider.example",
        capabilities=capability(),
        poll_path="/v1/tasks/{provider_job_id}",
        poll_interval_seconds=0,
        max_poll_attempts=1,
        transport=lambda req, _timeout: (
            (202, b'{"task_id":"task-9","status":"queued"}')
            if req.method == "POST"
            else (200, b'{"task_id":"task-9","status":"processing"}')
        ),
    )
    with pytest.raises(ProviderExecutionPending) as error:
        provider.execute(request())
    assert error.value.code == "provider_async_pending"
    assert error.value.retryable is True
    assert error.value.details["provider_job_id"] == "task-9"


def test_http_provider_callback_signature_is_verified_and_normalized():
    secret = "callback-secret"
    provider = HTTPProvider(
        name="hosted-video",
        base_url="https://provider.example",
        capabilities=capability(),
        callback_secret=secret,
    )
    payload = json.dumps(
        {"request_id": "req-1", "task_id": "task-7", "status": "succeeded", "artifact_uri": "https://cdn.example/out.mp4"},
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    event = provider.parse_callback(payload, signature=f"sha256={signature}")
    assert event.request_id == "req-1"
    assert event.provider_job_id == "task-7"
    with pytest.raises(ProviderExecutionError) as error:
        provider.parse_callback(payload, signature="sha256=bad")
    assert error.value.code == "provider_callback_unauthorized"


def test_command_provider_uses_argv_and_requires_artifact(tmp_path: Path):
    output = tmp_path / "result.json"
    provider = CommandProvider(
        name="local-gpu",
        capabilities=capability(),
        model=ModelSnapshot(provider="local-gpu", model_id="demo", version="1"),
        command_builder=lambda _request, path: [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ok')",
            str(path),
        ],
    )
    result = provider.execute(request(output_path=str(output)))
    assert result.artifact_uri == str(output)
    assert output.read_text() == "ok"


def test_command_provider_rejects_missing_output(tmp_path: Path):
    provider = CommandProvider(
        name="local-gpu",
        capabilities=capability(),
        model=ModelSnapshot(provider="local-gpu", model_id="demo", version="1"),
        command_builder=lambda _request, _path: ["true"],
    )
    with pytest.raises(ProviderExecutionError, match="output_path"):
        provider.execute(request())


def test_configured_command_provider_runs_real_local_media_path(tmp_path: Path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe are required for the real media adapter smoke test")
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=16x16:r=10", "-t", "0.2", str(source)],
        check=True,
        capture_output=True,
    )
    registry = ConfiguredProviderRegistry.from_dict(
        {
            "providers": [
                {
                    "name": "local-ffmpeg-copy",
                    "kind": "command",
                    "command": [ffmpeg, "-y", "-i", "{input_path}", "-c", "copy", "{output_path}"],
                    "artifact_type": "video.rendered",
                    "capabilities": [
                        {
                            "name": "video_editing",
                            "input_types": ["video"],
                            "output_types": ["video"],
                            "commercial_allowed": True,
                        }
                    ],
                    "model": {
                        "model_id": "ffmpeg-copy",
                        "version": "6.1",
                        "runtime_digest": "system-ffmpeg",
                        "weights_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                        "license_snapshot": {
                            "code_license": "LGPL-2.1-or-later",
                            "weights_license": "no-model-weights",
                            "commercial_allowed": True,
                        },
                    },
                }
            ]
        }
    )
    provider = registry.providers[0]
    result = provider.execute(
        ProviderRequest(
            request_id="real-media",
            capability="video_editing",
            inputs={"input_path": str(source)},
            parameters={"output_path": str(output)},
        )
    )
    assert result.artifact_uri == str(output)
    assert result.artifact_type == "video.rendered"
    assert output.stat().st_size > 0
    probe = subprocess.run([ffprobe, "-v", "error", str(output)], capture_output=True)
    assert probe.returncode == 0
    assert registry.health()[0].available is True
    routed = registry.router().execute(
        ProviderRequest(
            request_id="real-media-routed",
            capability="video_editing",
            inputs={"input_path": str(source)},
            parameters={"output_path": str(tmp_path / "routed.mp4")},
            model_id="ffmpeg-copy",
            model_version="6.1",
        )
    )
    assert routed.model.model_id == "ffmpeg-copy"


def test_configured_http_provider_exposes_polling_and_cost_policy(monkeypatch):
    monkeypatch.setenv("VIDEO_PROVIDER_CALLBACK_SECRET", "test-secret")
    registry = ConfiguredProviderRegistry.from_dict(
        {
            "providers": [
                {
                    "name": "hosted-video",
                    "kind": "http",
                    "endpoint": "https://provider.example",
                    "poll_path": "/v1/tasks/{provider_job_id}",
                    "poll_interval_seconds": 0,
                    "poll_timeout_seconds": 20,
                    "max_poll_attempts": 4,
                    "callback_secret_env": "VIDEO_PROVIDER_CALLBACK_SECRET",
                    "capabilities": [
                        {
                            "name": "video_generation",
                            "commercial_allowed": True,
                            "cost": {"per_minute": 0.12, "currency": "USD", "billing_unit": "second"},
                        }
                    ],
                    "model": {"model_id": "t2v", "version": "7", "license_snapshot": {"commercial_allowed": True}},
                }
            ]
        }
    )
    provider = registry.providers[0]
    assert provider.poll_path == "/v1/tasks/{provider_job_id}"
    assert provider.callback_secret == "test-secret"
    assert registry.registrations[0].capabilities[0].estimated_cost_per_minute == 0.12
    exported = registry.as_dict()["providers"][0]
    assert exported["callback_configured"] is True
    assert exported["capabilities"][0]["billing_unit"] == "second"


def test_provider_registry_rejects_invalid_command_config():
    with pytest.raises(ProviderRegistryError, match="command must be a non-empty argv array"):
        ConfiguredProviderRegistry.from_dict(
            {
                "providers": [
                    {
                        "name": "bad",
                        "kind": "command",
                        "command": [],
                        "capabilities": ["video_editing"],
                        "model": {},
                    }
                ]
            }
        )
