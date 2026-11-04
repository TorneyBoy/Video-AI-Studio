"""Planned M1 gate: public catalogue and the actual chosen operation agree."""

from copy import deepcopy
import json
from pathlib import Path

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


def test_catalog_keeps_accepted_combinations_separate_and_rejects_malformed_rows(monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    value = config()
    limits = value["providers"][0]["capabilities"][0]["limits"]
    limits["feature_combinations"] = [["audio_tracks"], ["audio_fades"], ["audio_tracks", "audio_fades"],
                                      "do-not-flatten", ["audio_tracks", {"api_key": "never-expose"}]]
    result = capability_catalog(ConfiguredProviderRegistry.from_dict(value))
    combinations = result["capabilities"][0]["limits"]["feature_combinations"]
    assert combinations == [["audio_tracks"], ["audio_fades"], ["audio_fades", "audio_tracks"]]
    assert "never-expose" not in json.dumps(result)


def test_ai_generation_limits_have_distinct_public_shapes_and_keep_zero_reference_bound(monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    value = config()
    limits = value["providers"][0]["capabilities"][0]["limits"]
    limits.update({"generation_resolutions": ["480P", "1080P", "480P", {"width": 720, "height": 1280}],
                   "resolutions": [{"width": 720, "height": 1280}, "720P"],
                   "allowed_durations": [5, 5.0, 10, -1, 0, True, "5", float("nan"), float("inf")],
                   "generation_modes": ["wan-pro", "wan-std", "", " wan-std", "bad\nmode", False],
                   "min_reference_inputs": 0, "max_reference_inputs": 0,
                   "max_prompt_chars": 800, "max_negative_prompt_chars": 500})
    result = capability_catalog(ConfiguredProviderRegistry.from_dict(value))["capabilities"][0]["limits"]
    assert result["generation_resolutions"] == ["1080P", "480P"]
    assert result["resolutions"] == [{"width": 720, "height": 1280}]
    assert result["allowed_durations"] == [5, 10]
    assert result["generation_modes"] == ["wan-pro", "wan-std"]
    assert result["min_reference_inputs"] == result["max_reference_inputs"] == 0
    assert result["max_prompt_chars"] == 800 and result["max_negative_prompt_chars"] == 500


@pytest.mark.parametrize("invalid", [[], ["wan-std"], "15", None])
def test_output_mode_frame_rates_require_a_mapping(monkeypatch, invalid):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    value = config()
    value["providers"][0]["capabilities"][0]["limits"]["output_fps_by_mode"] = invalid
    result = capability_catalog(ConfiguredProviderRegistry.from_dict(value))["capabilities"][0]["limits"]
    assert "output_fps_by_mode" not in result


def test_output_mode_frame_rates_expose_only_finite_safe_specs(monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    value = config()
    value["providers"][0]["capabilities"][0]["limits"]["output_fps_by_mode"] = {
        "wan-std": 15, "wan-pro": 25, "": 30, " trailing ": 30, "bad\nmode": 30,
        "bool": True, "string": "15", "nan": float("nan"), "inf": float("inf"),
        "zero": 0, "huge": 121, "secret": {"api_key": "never-expose"}}
    result = capability_catalog(ConfiguredProviderRegistry.from_dict(value))["capabilities"][0]["limits"]
    assert result["output_fps_by_mode"] == {"wan-std": 15, "wan-pro": 25}


def test_person_template_declares_mode_fps_without_an_exact_generation_resolution():
    path = Path(__file__).resolve().parents[1] / "config/providers.aliyun.ai.example.json"
    catalog = capability_catalog(ConfiguredProviderRegistry.from_file(path))["capabilities"]
    person = next(entry for entry in catalog if entry["capability"] == "person_replacement")
    assert person["limits"]["output_fps_by_mode"] == {"wan-std": 15, "wan-pro": 25}
    assert set(person["limits"]["generation_modes"]) == {"wan-std", "wan-pro"}
    assert "generation_resolutions" not in person["limits"]
    generation = next(entry for entry in catalog if entry["capability"] == "video_generation")
    assert set(generation["limits"]["generation_resolutions"]) == {"480P", "1080P"}
    assert not person["available"]  # This template change is not quality acceptance.


@pytest.mark.parametrize("invalid", [True, -1, 0.5, "800", float("inf"), None])
def test_ai_count_limits_do_not_admit_coerced_or_invalid_numbers(monkeypatch, invalid):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-test-credential")
    value = config()
    limits = value["providers"][0]["capabilities"][0]["limits"]
    for field in ("min_reference_inputs", "max_reference_inputs", "max_prompt_chars", "max_negative_prompt_chars"):
        limits[field] = invalid
    result = capability_catalog(ConfiguredProviderRegistry.from_dict(value))["capabilities"][0]["limits"]
    assert not set(result).intersection({"min_reference_inputs", "max_reference_inputs", "max_prompt_chars", "max_negative_prompt_chars"})


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
