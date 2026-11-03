import pytest

from app.config import Settings
from app.services.providers import (
    LocalDeterministicProvider,
    ProviderConfigurationError,
    ProviderExecutionError,
    ProviderRequest,
    default_provider_router,
)


def test_settings_default_to_api_only():
    assert Settings().provider_mode == "api_only"
    with pytest.raises(ValueError, match="PROVIDER_MODE"):
        Settings(provider_mode="local")


def test_api_only_does_not_insert_local_deterministic_provider(monkeypatch):
    monkeypatch.delenv("PROVIDER_REGISTRY_JSON", raising=False)
    monkeypatch.delenv("PROVIDER_REGISTRY_PATH", raising=False)
    router = default_provider_router(mode="api_only")
    assert router.providers == []
    with pytest.raises(ProviderExecutionError, match="no commercial provider"):
        router.choose(
            ProviderRequest(
                request_id="api-only-missing-provider",
                capability="video_generation",
                inputs={"prompt": "test"},
            )
        )


def test_api_only_rejects_command_provider_configuration(monkeypatch):
    monkeypatch.setenv(
        "PROVIDER_REGISTRY_JSON",
        '{"providers":[{"name":"local-command","kind":"command",'
        '"command":["echo"],"capabilities":["video_generation"],'
        '"model":{"model_id":"m","version":"1"}}]}',
    )
    with pytest.raises(ProviderConfigurationError, match="only allows HTTP"):
        default_provider_router(mode="api_only")


def test_development_mock_must_be_explicit(monkeypatch):
    monkeypatch.delenv("PROVIDER_REGISTRY_JSON", raising=False)
    monkeypatch.delenv("PROVIDER_REGISTRY_PATH", raising=False)
    router = default_provider_router(mode="development_mock")
    assert any(isinstance(provider, LocalDeterministicProvider) for provider in router.providers)
