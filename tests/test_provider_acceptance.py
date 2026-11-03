"""Network-free production Provider admission gate tests."""

import json
from pathlib import Path

import pytest

from app.services.provider_acceptance import provider_acceptance, provider_acceptance_file


FIXTURE_PATH = Path(__file__).parents[1] / "config" / "providers.contract.fixture.json"


def test_contract_registry_acceptance_is_network_free(monkeypatch):
    def fail_network(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("provider acceptance must not open a network socket")

    monkeypatch.setattr("socket.socket", fail_network)
    report = provider_acceptance_file(FIXTURE_PATH, require_secrets=False)

    assert report.ok
    assert {check.code for check in report.checks} >= {
        "provider_mode_api_only",
        "asset_storage_external",
        "network_free_static_gate",
        "http_provider_only",
    }
    assert report.preflight.required_capabilities
    payload = report.as_dict()
    assert payload["ok"] is True
    assert payload["preflight"]["ok"] is True
    assert payload["summary"]["errors"] == 0


@pytest.mark.parametrize(
    ("provider_mode", "asset_storage_mode", "code"),
    [
        ("development_mock", "external", "provider_mode_api_only"),
        ("api_only", "local", "asset_storage_external"),
    ],
)
def test_acceptance_rejects_non_production_execution_modes(
    provider_mode: str,
    asset_storage_mode: str,
    code: str,
):
    config = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    report = provider_acceptance(
        config,
        require_secrets=False,
        provider_mode=provider_mode,
        asset_storage_mode=asset_storage_mode,
    )

    assert not report.ok
    failed = next(check for check in report.checks if check.code == code)
    assert failed.status == "error"


def test_acceptance_rejects_local_provider_kind_before_worker_start():
    config = {
        "providers": [
            {
                "name": "local-command",
                "kind": "command",
                "command": ["echo"],
                "capabilities": [
                    {
                        "name": "video_generation",
                        "commercial_allowed": True,
                        "cost": {"per_request": 0, "currency": "USD", "billing_unit": "request"},
                    }
                ],
                "model": {
                    "model_id": "development-only",
                    "version": "2026-10",
                    "license_snapshot": {"commercial_allowed": True},
                },
            }
        ]
    }
    report = provider_acceptance(
        config,
        required_capabilities=("video_generation",),
        require_secrets=False,
    )

    assert not report.ok
    assert any(check.code == "http_provider_only" and check.status == "error" for check in report.checks)
    assert any(check.code == "provider_kind_not_http" for check in report.preflight.errors)


def test_acceptance_file_rejects_non_object_json(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a JSON object"):
        provider_acceptance_file(path, require_secrets=False)
