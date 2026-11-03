"""Contract smoke tests for the hosted Provider Gateway boundary.

These tests use an in-process HTTP transport so they never call a real model
vendor.  They exercise the invariants that protect production retries:
stable request identity, remote-only artifacts, durable async polling, and
usage/cost metadata preservation.
"""

import json

import pytest

from app.services.provider_adapters import HTTPProvider
from app.services.provider_registry import ConfiguredProviderRegistry, ProviderRegistryError
from app.services.providers import (
    ModelSnapshot,
    ProviderCapability,
    ProviderExecutionError,
    ProviderExecutionPending,
    ProviderRequest,
)


def _capability():
    return (
        ProviderCapability(
            "video_generation",
            ("json",),
            ("video",),
            commercial_allowed=True,
        ),
    )


def _request(*, request_id: str = "gateway-smoke-1", parameters: dict | None = None):
    return ProviderRequest(
        request_id=request_id,
        capability="video_generation",
        inputs={
            "asset_uri": "s3://input-bucket/source.mp4",
            # These are intentionally local values.  A hosted provider must
            # never receive worker filesystem paths.
            "input_path": "/srv/private/source.mp4",
        },
        parameters=parameters or {},
        tenant_id="tenant-smoke",
        commercial_use=True,
    )


def test_http_provider_emits_stable_idempotency_headers_and_remote_inputs_only():
    captured: dict[str, object] = {}

    def transport(http_request, timeout):
        captured["method"] = http_request.method
        captured["headers"] = {key.lower(): value for key, value in http_request.header_items()}
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return 200, b'{"artifact_uri":"https://cdn.example/out.mp4","artifact_type":"video"}'

    provider = HTTPProvider(
        name="hosted-video",
        base_url="https://provider.example",
        capabilities=_capability(),
        model=ModelSnapshot(provider="hosted-video", model_id="video-v1", version="2026-10"),
        headers={"X-Trace": "smoke"},
        transport=transport,
    )
    result = provider.execute(
        _request(
            request_id="stable-request-id",
            parameters={"output_path": "/srv/private/output.mp4"},
        )
    )

    headers = captured["headers"]
    body = captured["body"]
    assert captured["method"] == "POST"
    assert headers["idempotency-key"] == "stable-request-id"
    assert headers["x-request-id"] == "stable-request-id"
    assert body["request_id"] == "stable-request-id"
    assert body["inputs"]["asset_uri"] == "s3://input-bucket/source.mp4"
    assert "input_path" not in body["inputs"]
    assert "output_path" not in body["parameters"]
    assert result.artifact_uri == "https://cdn.example/out.mp4"


def test_async_retry_resumes_polling_without_submitting_duplicate_work():
    calls: list[tuple[str, str, dict[str, str]]] = []
    poll_count = 0

    def transport(http_request, _timeout):
        nonlocal poll_count
        headers = {key.lower(): value for key, value in http_request.header_items()}
        calls.append((http_request.method, http_request.full_url, headers))
        if http_request.method == "POST":
            assert headers["idempotency-key"] == "async-request-id"
            return 202, b'{"task_id":"task/with slash","status":"queued"}'
        poll_count += 1
        if poll_count == 1:
            return 200, b'{"task_id":"task/with slash","status":"processing"}'
        return 200, (
            b'{"task_id":"task/with slash","status":"succeeded",'
            b'"artifact_uri":"s3://output-bucket/result.mp4",'
            b'"usage":{"seconds":12.5},"cost":{"amount":0.42,"currency":"USD"}}'
        )

    provider = HTTPProvider(
        name="hosted-video",
        base_url="https://provider.example",
        capabilities=_capability(),
        poll_path="/v1/tasks/{provider_job_id}",
        poll_interval_seconds=0,
        max_poll_attempts=1,
        transport=transport,
    )
    with pytest.raises(ProviderExecutionPending) as pending:
        provider.execute(_request(request_id="async-request-id"))
    assert pending.value.details["provider_job_id"] == "task/with slash"

    # The worker persists this ID in the pending failure audit and supplies it
    # on retry.  The adapter must poll the existing remote job and never POST
    # a second billable generation request.
    resumed = provider.execute(
        _request(
            request_id="async-request-id",
            parameters={"provider_job_id": "task/with slash"},
        )
    )
    assert resumed.artifact_uri == "s3://output-bucket/result.mp4"
    assert resumed.usage == {"seconds": 12.5}
    assert resumed.cost == {"amount": 0.42, "currency": "USD"}
    assert [method for method, _url, _headers in calls].count("POST") == 1
    assert [method for method, _url, _headers in calls].count("GET") == 2
    assert calls[1][1].endswith("/v1/tasks/task%2Fwith%20slash")
    assert calls[1][2]["x-request-id"] == "async-request-id"


def test_http_provider_rejects_non_remote_artifact_even_when_usage_is_present():
    provider = HTTPProvider(
        name="hosted-video",
        base_url="https://provider.example",
        capabilities=_capability(),
        transport=lambda _request, _timeout: (
            200,
            b'{"status":"succeeded","artifact_uri":"file:///tmp/out.mp4",'
            b'"usage":{"seconds":1},"cost":{"amount":0.01}}',
        ),
    )
    with pytest.raises(ProviderExecutionError) as error:
        provider.execute(_request())
    assert error.value.code == "provider_invalid_artifact_uri"


def test_http_provider_rejects_local_input_uri_before_transport():
    called = False

    def transport(_request, _timeout):
        nonlocal called
        called = True
        return 200, b'{"artifact_uri":"https://cdn.example/out.mp4"}'

    provider = HTTPProvider(
        name="hosted-video",
        base_url="https://provider.example",
        capabilities=_capability(),
        transport=transport,
    )
    request = ProviderRequest(
        request_id="local-input",
        capability="video_generation",
        inputs={"asset_uri": "file:///srv/private/source.mp4"},
    )
    with pytest.raises(ProviderExecutionError) as error:
        provider.execute(request)
    assert error.value.code == "provider_invalid_input_uri"
    assert called is False


def test_registry_rejects_invalid_callback_secret_environment_name():
    with pytest.raises(ProviderRegistryError, match="uppercase environment name"):
        ConfiguredProviderRegistry.from_dict(
            {
                "providers": [
                    {
                        "name": "hosted-video",
                        "kind": "http",
                        "endpoint": "https://provider.example",
                        "callback_secret_env": "provider-secret",
                        "capabilities": ["video_generation"],
                        "model": {},
                    }
                ]
            }
        )
