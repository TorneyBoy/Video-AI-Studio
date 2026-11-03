"""Network-free tests for the opt-in Provider Gateway smoke harness."""

import json
import sys

from app.services.provider_registry import ConfiguredProviderRegistry
from app.services.provider_smoke import run_provider_smoke


def _registry(*, max_poll_attempts: int = 2):
    return ConfiguredProviderRegistry.from_dict(
        {
            "providers": [
                {
                    "name": "staging-video",
                    "kind": "http",
                    "endpoint": "https://staging-gateway.example",
                    "path": "/v1/video/submit",
                    "poll_path": "/v1/jobs/{provider_job_id}",
                    "poll_interval_seconds": 0,
                    "max_poll_attempts": max_poll_attempts,
                    "headers_env": {},
                    "capabilities": [
                        {
                            "name": "video_generation",
                            "commercial_allowed": True,
                            "cost": {"per_request": 0, "currency": "USD", "billing_unit": "request"},
                        }
                    ],
                    "model": {
                        "model_id": "staging-model",
                        "version": "staging-1",
                        "weights_sha256": "0" * 64,
                        "license_snapshot": {
                            "commercial_allowed": True,
                            "status": "approved",
                            "weights_license": "provider-managed",
                        },
                    },
                }
            ]
        }
    )


def _kwargs(**overrides):
    values = {
        "provider_name": "staging-video",
        "capability": "video_generation",
        "request_id": "smoke-request-1",
        "inputs": {"asset_uri": "s3://fixtures/source.mp4"},
    }
    values.update(overrides)
    return values


def test_dry_run_is_default_and_never_calls_gateway():
    calls = []

    def transport(request, timeout):
        calls.append((request.method, request.full_url, timeout))
        raise AssertionError("dry-run must not call a provider")

    result = run_provider_smoke(_registry(), transport=transport, **_kwargs())

    assert result.status == "pass"
    assert result.executed is False
    assert result.quality_claim is False
    assert result.quality_evidence == "not_evaluated"
    assert calls == []


def test_local_input_is_rejected_before_explicit_execution():
    calls = []

    result = run_provider_smoke(
        _registry(),
        execute=True,
        transport=lambda request, timeout: calls.append(request) or (200, b"{}"),
        **_kwargs(inputs={"asset_uri": "file:///tmp/source.mp4"}),
    )

    assert result.status == "fail"
    assert result.executed is False
    assert result.error_code == "provider_registry_error"
    assert calls == []


def test_model_identity_and_capability_are_validated_without_network():
    model_result = run_provider_smoke(_registry(), **_kwargs(model_version="wrong"))
    capability_result = run_provider_smoke(_registry(), **_kwargs(capability="face_transform"))

    assert model_result.status == "fail"
    assert capability_result.status == "fail"
    assert model_result.executed is False
    assert capability_result.executed is False


def test_explicit_execution_reports_pass_without_quality_claim():
    calls = []

    def transport(request, timeout):
        calls.append((request.method, request.full_url, dict(request.header_items())))
        return 200, json.dumps({"artifact_uri": "s3://fixtures/output.mp4", "artifact_type": "video"}).encode()

    result = run_provider_smoke(_registry(), execute=True, transport=transport, **_kwargs())

    assert result.status == "pass"
    assert result.executed is True
    assert result.artifact_uri == "s3://fixtures/output.mp4"
    assert result.quality_claim is False
    assert calls[0][0] == "POST"
    assert calls[0][2]["Idempotency-key"] == "smoke-request-1"


def test_pending_result_can_resume_same_remote_task_without_duplicate_post():
    calls = []
    poll_count = 0

    def transport(request, timeout):
        nonlocal poll_count
        calls.append(request.method)
        if request.method == "POST":
            return 202, b'{"task_id":"remote-task-1","status":"queued"}'
        poll_count += 1
        if poll_count == 1:
            return 200, b'{"task_id":"remote-task-1","status":"processing"}'
        return 200, b'{"task_id":"remote-task-1","status":"succeeded","artifact_uri":"s3://fixtures/out.mp4"}'

    first = run_provider_smoke(
        _registry(max_poll_attempts=1), execute=True, transport=transport, **_kwargs()
    )
    assert first.status == "pending"
    assert first.provider_job_id == "remote-task-1"
    assert first.poll_after_seconds == 0

    resumed = run_provider_smoke(
        _registry(max_poll_attempts=1),
        execute=True,
        provider_job_id=first.provider_job_id,
        transport=transport,
        **_kwargs(),
    )
    assert resumed.status == "pass"
    assert resumed.artifact_uri == "s3://fixtures/out.mp4"
    assert calls == ["POST", "GET", "GET"]


def test_cli_smoke_is_dry_run_by_default(tmp_path, monkeypatch, capsys):
    from app.cli import main

    config = {
        "providers": [
            {
                "name": "staging-video",
                "kind": "http",
                "endpoint": "https://staging-gateway.example",
                "path": "/v1/video/submit",
                "headers_env": {},
                "capabilities": [
                    {
                        "name": "video_generation",
                        "commercial_allowed": True,
                        "cost": {"per_request": 0, "currency": "USD", "billing_unit": "request"},
                    }
                ],
                "model": {
                    "model_id": "staging-model",
                    "version": "staging-1",
                    "weights_sha256": "0" * 64,
                    "license_snapshot": {
                        "commercial_allowed": True,
                        "status": "approved",
                        "weights_license": "provider-managed-staging",
                    },
                },
            }
        ]
    }
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "video-ai",
            "provider-smoke",
            "--path",
            str(path),
            "--provider",
            "staging-video",
            "--capability",
            "video_generation",
            "--request-id",
            "cli-smoke-1",
            "--inputs-json",
            '{"asset_uri":"s3://fixtures/source.mp4"}',
            "--json",
        ],
    )

    main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "pass"
    assert result["executed"] is False
