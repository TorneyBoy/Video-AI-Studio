import json
from pathlib import Path

import pytest

from app.services.provider_preflight import (
    HOSTED_CAPABILITIES,
    preflight_provider_registry,
    preflight_provider_registry_file,
)


FIXTURE_PATH = Path(__file__).parents[1] / "config" / "providers.contract.fixture.json"
SECRET_VALUES = {
    "CONTRACT_VIDEO_API_KEY": "video-secret-value",
    "CONTRACT_VIDEO_CALLBACK_SECRET": "video-callback-secret",
    "CONTRACT_VOICE_API_KEY": "voice-secret-value",
    "CONTRACT_VOICE_CALLBACK_SECRET": "voice-callback-secret",
    "CONTRACT_AVATAR_API_KEY": "avatar-secret-value",
    "CONTRACT_AVATAR_CALLBACK_SECRET": "avatar-callback-secret",
}


def test_contract_fixture_passes_without_network_and_covers_hosted_capabilities(monkeypatch):
    for name, value in SECRET_VALUES.items():
        monkeypatch.setenv(name, value)

    report = preflight_provider_registry_file(FIXTURE_PATH)

    assert report.ok
    assert not report.errors
    assert set(report.required_capabilities) == HOSTED_CAPABILITIES
    coverage = {
        check.details["capability"]
        for check in report.passed
        if check.code == "capability_covered"
    }
    assert coverage == HOSTED_CAPABILITIES
    assert all(check.code != "provider_health_requested" for check in report.checks)

    rendered = json.dumps(report.as_dict(), ensure_ascii=False)
    for secret in SECRET_VALUES.values():
        assert secret not in rendered


def test_missing_secret_is_reported_without_leaking_secret_value(monkeypatch):
    monkeypatch.delenv("CONTRACT_VIDEO_API_KEY", raising=False)
    monkeypatch.setenv("CONTRACT_VIDEO_CALLBACK_SECRET", SECRET_VALUES["CONTRACT_VIDEO_CALLBACK_SECRET"])
    for name in (
        "CONTRACT_VOICE_API_KEY",
        "CONTRACT_VOICE_CALLBACK_SECRET",
        "CONTRACT_AVATAR_API_KEY",
        "CONTRACT_AVATAR_CALLBACK_SECRET",
    ):
        monkeypatch.setenv(name, "configured")

    report = preflight_provider_registry_file(FIXTURE_PATH)
    assert not report.ok
    assert any(check.code == "missing_secret_env" for check in report.errors)
    assert "video-secret-value" not in json.dumps(report.as_dict())


def test_template_checks_can_be_run_without_secret_values(monkeypatch):
    monkeypatch.delenv("CONTRACT_VIDEO_API_KEY", raising=False)
    report = preflight_provider_registry_file(FIXTURE_PATH, require_secrets=False)
    assert report.ok
    assert any(check.code == "missing_secret_env" and check.status == "warning" for check in report.warnings)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("endpoint", "http://not-secure.example.test", "endpoint_not_https"),
        ("callback_secret_env", "lowercase-secret", "invalid_secret_env_name"),
        ("model.version", "unknown", "model_version_missing"),
    ],
)
def test_preflight_rejects_unsafe_production_registration(monkeypatch, field, value, code):
    for name in SECRET_VALUES:
        monkeypatch.setenv(name, "configured")
    config = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    provider = config["providers"][0]
    if field == "model.version":
        provider["model"]["version"] = value
    else:
        provider[field] = value

    report = preflight_provider_registry(config)
    assert not report.ok
    assert any(check.code == code for check in report.errors)


def test_preflight_rejects_missing_cost_and_commercial_license(monkeypatch):
    for name in SECRET_VALUES:
        monkeypatch.setenv(name, "configured")
    config = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    provider = config["providers"][0]
    provider["capabilities"][0]["cost"] = {}
    provider["capabilities"][0]["commercial_allowed"] = False
    provider["model"]["license_snapshot"]["commercial_allowed"] = False

    report = preflight_provider_registry(config)
    assert not report.ok
    codes = {check.code for check in report.errors}
    assert {"missing_cost_config", "capability_commercial_denied", "commercial_license_denied"} <= codes


def test_preflight_rejects_static_secret_header(monkeypatch):
    for name in SECRET_VALUES:
        monkeypatch.setenv(name, "configured")
    config = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config["providers"][0]["headers"] = {"Authorization": "never-print-this"}

    report = preflight_provider_registry(config)
    assert not report.ok
    assert any(check.code == "static_secret_header" for check in report.errors)
    assert "never-print-this" not in json.dumps(report.as_dict())


def test_preflight_rejects_suspended_model(monkeypatch):
    for name in SECRET_VALUES:
        monkeypatch.setenv(name, "configured")
    config = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config["providers"][0]["model"]["license_snapshot"]["status"] = "suspended"

    report = preflight_provider_registry(config)
    assert not report.ok
    assert any(check.code == "model_not_approved" for check in report.errors)


@pytest.mark.parametrize(
    ("endpoint", "code"),
    [
        ("https://user:password@gateway.example.com", "endpoint_contains_credentials_or_query"),
        ("https://gateway.example.com?token=secret", "endpoint_contains_credentials_or_query"),
        ("https://gateway.example.com/path#fragment", "endpoint_contains_credentials_or_query"),
    ],
)
def test_preflight_rejects_unsafe_provider_endpoint(monkeypatch, endpoint, code):
    for name in SECRET_VALUES:
        monkeypatch.setenv(name, "configured")
    config = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config["providers"][0]["endpoint"] = endpoint

    report = preflight_provider_registry(config)
    assert not report.ok
    assert any(check.code == code for check in report.errors)


def test_preflight_rejects_unsafe_header_values(monkeypatch):
    for name in SECRET_VALUES:
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv("CONTRACT_VIDEO_API_KEY", "header\r\ninjected")
    config = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config["providers"][0]["headers"] = {"X-Trace": ""}
    config["providers"][0]["headers_env"]["X-Trace"] = "CONTRACT_VIDEO_API_KEY"

    report = preflight_provider_registry(config)
    assert not report.ok
    codes = {check.code for check in report.errors}
    assert "invalid_header_value" in codes


def test_preflight_rejects_invalid_header_name(monkeypatch):
    for name in SECRET_VALUES:
        monkeypatch.setenv(name, "configured")
    config = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config["providers"][0]["headers"] = {"X Trace": "value"}

    report = preflight_provider_registry(config)
    assert not report.ok
    assert any(check.code == "invalid_header_name" for check in report.errors)
