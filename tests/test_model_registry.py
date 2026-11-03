from pathlib import Path

import pytest

from app.services.model_registry import ModelRegistry, ModelVersion, development_registry
from app.services.provider_registry import ConfiguredProviderRegistry
from app.services.providers import (
    ModelSnapshot,
    ProviderCapability,
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderRouter,
)


def _model(**kwargs) -> ModelVersion:
    values = {
        "provider": "gpu-video",
        "model_id": "motion-v1",
        "version": "2026.10",
        "capabilities": ("video_generation",),
        "code_license": "Apache-2.0",
        "weights_license": "commercial",
        "commercial_allowed": True,
        "allowed_regions": ("US", "CN"),
        "runtime_digest": "sha256:runtime",
        "weights_sha256": "a" * 64,
        "status": "approved",
    }
    values.update(kwargs)
    return ModelVersion(**values)


class RegisteredProvider:
    name = "gpu-video"
    model = ModelSnapshot(
        provider="gpu-video",
        model_id="motion-v1",
        version="2026.10",
        weights_sha256="a" * 64,
        runtime_digest="sha256:runtime",
    )
    capabilities = (
        ProviderCapability("video_generation", ("json",), ("video",), commercial_allowed=True),
    )

    def execute(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            artifact_uri="https://cdn.example/output.mp4",
            artifact_type="video",
            model=self.model,
        )


def test_registry_routes_only_approved_commercial_model_in_region():
    router = ProviderRouter(
        [RegisteredProvider()],
        model_registry=ModelRegistry([_model()]),
    )
    result = router.execute(
        ProviderRequest(
            request_id="r1",
            capability="video_generation",
            inputs={"prompt": "a kite"},
            region="CN",
            model_id="motion-v1",
            model_version="2026.10",
        )
    )
    assert result.model.model_id == "motion-v1"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"commercial_allowed": False}, "model_commercial_restricted"),
        ({"allowed_regions": ("US",)}, "model_region_restricted"),
        ({"status": "suspended"}, "model_not_active"),
        ({"weights_sha256": None}, "model_integrity_unverified"),
        ({"weights_license": None}, "model_license_unknown"),
    ],
)
def test_registry_rejects_policy_violations(changes, code):
    request = ProviderRequest(
        request_id="r1",
        capability="video_generation",
        inputs={"prompt": "a kite"},
        region="CN",
    )
    with pytest.raises(ProviderExecutionError) as error:
        ModelRegistry([_model(**changes)]).validate_request(_model(**changes), request)
    assert error.value.code == code


def test_registry_rejects_adapter_snapshot_drift():
    registry = ModelRegistry([_model()])
    request = ProviderRequest(request_id="r1", capability="video_generation", inputs={})
    with pytest.raises(ProviderExecutionError, match="different from the registered") as error:
        registry.validate_snapshot(
            _model(),
            ModelSnapshot(provider="gpu-video", model_id="other", version="2026.10"),
            request,
        )
    assert error.value.code == "model_snapshot_mismatch"


def test_registry_json_round_trip(tmp_path: Path):
    registry = ModelRegistry([_model(commit_sha="0123456789abcdef")])
    path = tmp_path / "models.json"
    registry.to_json(path)
    loaded = ModelRegistry.from_json(path)
    assert loaded.get("gpu-video", "motion-v1", "2026.10").commit_sha == "0123456789abcdef"


def test_development_router_keeps_manifest_adapter_executable(tmp_path: Path):
    from app.services.providers import LocalDeterministicProvider

    router = ProviderRouter(
        [LocalDeterministicProvider()],
        model_registry=development_registry(),
    )
    result = router.execute(
        ProviderRequest(
            request_id="dev-1",
            capability="video_generation",
            inputs={"prompt": "demo"},
            parameters={"output_dir": str(tmp_path)},
        )
    )
    assert result.status == "simulated"


def test_configured_provider_registry_exposes_model_policy():
    configured = ConfiguredProviderRegistry.from_dict(
        {
            "providers": [
                {
                    "name": "remote-video",
                    "kind": "http",
                    "endpoint": "https://provider.example",
                    "capabilities": [{"name": "video_generation", "commercial_allowed": True}],
                    "model": {
                        "model_id": "t2v",
                        "version": "7",
                        "weights_sha256": "b" * 64,
                        "license_snapshot": {
                            "weights_license": "commercial",
                            "commercial_allowed": True,
                            "allowed_regions": ["US"],
                        },
                    },
                }
            ]
        }
    )
    model = configured.model_registry().resolve(
        provider="remote-video", capability="video_generation", model_id="t2v", version="7"
    )
    assert model is not None
    assert model.commercial_allowed is True
    assert model.allowed_regions == ("US",)

