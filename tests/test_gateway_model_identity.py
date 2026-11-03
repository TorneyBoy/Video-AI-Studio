from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services.provider_registry import ConfiguredProviderRegistry
from app.services.providers import ModelSnapshot, ProviderRequest
from media_gateway.model_identity import registered_result


@pytest.mark.parametrize("index,legacy,capability", [
    (0, "aliyun-hosted", "media_analysis"), (1, "aliyun-ice", "video_editing")])
def test_known_initial_alias_projects_to_registered_identity_without_rewriting(index, legacy, capability, monkeypatch):
    monkeypatch.setenv("MEDIA_GATEWAY_AUTHORIZATION", "Bearer fixture-key-not-a-live-credential")
    monkeypatch.setenv("MEDIA_GATEWAY_CALLBACK_SECRET", "fixture-callback-secret-not-live")
    config = json.loads((Path(__file__).resolve().parents[1] / "config/providers.aliyun.m1.json").read_text())
    registry = ConfiguredProviderRegistry.from_dict(config)
    entry = config["providers"][index]
    model_id, version = entry["model"]["model_id"], entry["model"]["version"]
    original = {"model": {"provider": legacy, "model_id": model_id, "version": version},
                "artifact_uri": "oss://fixture-bucket/immutable-original", "metrics": {"source_sha256": "a" * 64},
                "cost": {"status": "unknown"}}
    before = deepcopy(original)
    projected = registered_result(original, provider=entry["name"], legacy_provider=legacy, model_id=model_id, version=version)
    models = registry.model_registry()
    expected = models.get(entry["name"], model_id, version)
    models.validate_snapshot(expected, ModelSnapshot(**projected["model"]),
                             ProviderRequest(request_id="known-job", capability=capability, inputs={}))
    assert original == before and projected["artifact_uri"] == original["artifact_uri"]
    assert projected["cost"] == original["cost"]
    assert projected["metrics"]["gateway_identity_correction"]["stored_receipt_unchanged"] is True
    for changed in ({"provider": "another-provider"}, {"version": "another-release"}, {"model_id": "another-model"}):
        other = {**original, "model": {**original["model"], **changed}}
        assert registered_result(other, provider=entry["name"], legacy_provider=legacy,
                                 model_id=model_id, version=version) is other
