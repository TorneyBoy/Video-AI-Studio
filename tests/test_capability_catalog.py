"""Planned M1 gate: public catalogue and the actual chosen operation agree."""

from copy import deepcopy
import json

import pytest

from app.services.capabilities import capability_catalog, require_available_capability
from app.services.provider_registry import ConfiguredProviderRegistry
from app.services.providers import ProviderExecutionError


def config():
    return {"providers": [{
        "name": "verified-gateway", "kind": "http", "endpoint": "https://gateway.example",
        "headers_env": {"Authorization": "TEST_CATALOG_API_KEY"},
        "model": {"model_id": "hosted-editor", "version": "2026-10",
                  "license_snapshot": {"commercial_allowed": True, "weights_license": "provider-managed",
                                       "integrity_mode": "provider_version", "integrity_reference": "vendor-release-2026-10"}},
        "capabilities": [{"name": "video_editing", "commercial_allowed": True,
                          "acceptance": {"status": "verified", "report_ref": "internal-report", "model_version": "2026-10"},
                          "price_version": "price-1", "cost": {"per_request": 0.01, "currency": "USD"},
                          "limits": {"max_duration_seconds": 60, "features": ["trim", "subtitles"], "api_key": "never-expose"}}],
    }]}


def test_catalog_is_network_free_and_excludes_credentials(monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    registry = ConfiguredProviderRegistry.from_dict(config())
    monkeypatch.setattr(registry.providers[0], "health_check", lambda **_: pytest.fail("catalogue must not probe"))
    result = capability_catalog(registry)
    item = result["capabilities"][0]
    assert item["available"] is True
    assert item["model_version"] == "2026-10"
    assert item["limits"] == {"max_duration_seconds": 60, "features": ["trim", "subtitles"]}
    encoded = json.dumps(result)
    assert "catalog-test-credential" not in encoded
    assert "gateway.example" not in encoded
    assert "internal-report" not in encoded
    assert "never-expose" not in encoded
    assert "weights_sha256" not in encoded


@pytest.mark.parametrize("change,status", [
    ({"acceptance": {"status": "unverified"}}, "awaiting_acceptance"),
    ({"acceptance": {"status": "verified", "model_version": "old", "report_ref": "report"}}, "awaiting_acceptance"),
    ({"price_version": None}, "awaiting_price"),
    ({"commercial_allowed": False}, "policy_unavailable"),
])
def test_unaccepted_versions_are_not_available(monkeypatch, change, status):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    value = config()
    value["providers"][0]["capabilities"][0].update(change)
    item = capability_catalog(ConfiguredProviderRegistry.from_dict(value))["capabilities"][0]
    assert item["status"] == status
    assert item["available"] is False


def test_selection_pins_a_verified_model_and_rejects_missing_version(monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    value = config()
    second = deepcopy(value["providers"][0])
    second["name"] = "unverified-gateway"
    second["capabilities"][0]["quality_score"] = 1.0
    second["capabilities"][0]["acceptance"] = {"status": "testing"}
    value["providers"].append(second)
    monkeypatch.setenv("PROVIDER_REGISTRY_JSON", json.dumps(value))
    selected = require_available_capability("video_editing")
    assert selected["provider"] == "verified-gateway"
    assert selected["price"]["version"] == "price-1"
    with pytest.raises(ProviderExecutionError, match="not open"):
        require_available_capability("video_editing", model_version="does-not-exist")


def test_hosted_evidence_does_not_fabricate_a_weights_hash(monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    registry = ConfiguredProviderRegistry.from_dict(config())
    model = registry.model_registry().all()[0]
    assert model.weights_sha256 is None
    assert model.integrity_mode == "provider_version"
    assert model.snapshot().license_snapshot["integrity_reference"] == "vendor-release-2026-10"
