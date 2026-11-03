import json
from pathlib import Path

import pytest

from app.services.provider_registry import ConfiguredProviderRegistry, ProviderRegistryError


TEMPLATE_PATH = Path(__file__).parents[1] / "config" / "providers.example.json"


def test_disabled_provider_does_not_require_or_load_secret_environment(monkeypatch):
    payload = {"providers": [{"name": "future-provider", "kind": "http", "enabled": False,
        "endpoint": "https://provider.example", "headers_env": {"Authorization": "FUTURE_PROVIDER_KEY"},
        "callback_secret_env": "FUTURE_PROVIDER_CALLBACK", "capabilities": ["media_analysis"]}]}
    monkeypatch.delenv("FUTURE_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("FUTURE_PROVIDER_CALLBACK", raising=False)
    disabled = ConfiguredProviderRegistry.from_dict(payload)
    assert disabled.providers[0].headers == {} and not disabled.registrations[0].enabled
    monkeypatch.setenv("FUTURE_PROVIDER_KEY", "must-not-be-loaded")
    monkeypatch.setenv("FUTURE_PROVIDER_CALLBACK", "must-not-be-loaded-either")
    assert ConfiguredProviderRegistry.from_dict(payload).providers[0].headers == {}
    payload["providers"][0]["enabled"] = True
    monkeypatch.delenv("FUTURE_PROVIDER_KEY")
    with pytest.raises(ProviderRegistryError, match="FUTURE_PROVIDER_KEY"):
        ConfiguredProviderRegistry.from_dict(payload)


def test_disabled_provider_still_validates_secret_reference_shape():
    with pytest.raises(ProviderRegistryError, match="uppercase environment"):
        ConfiguredProviderRegistry.from_dict({"providers": [{"name": "disabled", "kind": "http", "enabled": False,
            "endpoint": "https://provider.example", "headers_env": {"Authorization": "invalid-secret-ref"},
            "capabilities": ["media_analysis"]}]})


_SECRET_ENV = {
    "VIDEO_EDITING_API_KEY": "video-editing-key",
    "VIDEO_EDITING_CALLBACK_SECRET": "video-editing-callback",
    "VIDEO_GENERATION_API_KEY": "video-generation-key",
    "VIDEO_GENERATION_CALLBACK_SECRET": "video-generation-callback",
    "VOICE_API_KEY": "voice-key",
    "VOICE_CALLBACK_SECRET": "voice-callback",
    "AVATAR_API_KEY": "avatar-key",
    "AVATAR_CALLBACK_SECRET": "avatar-callback",
    "ENHANCEMENT_API_KEY": "enhancement-key",
    "ENHANCEMENT_CALLBACK_SECRET": "enhancement-callback",
}


def test_hosted_provider_template_is_valid_and_covers_external_capabilities(monkeypatch):
    for name, value in _SECRET_ENV.items():
        monkeypatch.setenv(name, value)

    registry = ConfiguredProviderRegistry.from_file(TEMPLATE_PATH)
    capabilities = {
        capability.name
        for registration in registry.registrations
        for capability in registration.capabilities
    }

    assert {
        "video_editing",
        "media_analysis",
        "video_generation",
        "voice_tts",
        "voice_clone",
        "voice_conversion",
        "motion_transfer",
        "face_transform",
        "lip_sync",
        "enhancement",
    } <= capabilities
    assert len(registry.providers) == 5
    assert all(registration.kind == "http" for registration in registry.registrations)
    assert all(registration.callback_configured for registration in registry.registrations)
    assert all(registration.poll_path for registration in registry.registrations)

    exported = registry.as_dict()
    assert exported["providers"][0]["capabilities"][0]["cost_currency"] == "USD"
    # Secret values are used by the adapter but never appear in registry introspection.
    assert "video-editing-key" not in json.dumps(exported)


def test_headers_env_fails_fast_when_secret_manager_value_is_missing(monkeypatch):
    monkeypatch.delenv("VIDEO_EDITING_API_KEY", raising=False)
    monkeypatch.setenv("VIDEO_EDITING_CALLBACK_SECRET", "callback")

    with pytest.raises(ProviderRegistryError, match="VIDEO_EDITING_API_KEY"):
        ConfiguredProviderRegistry.from_dict(
            {
                "providers": [
                    {
                        "name": "video",
                        "kind": "http",
                        "endpoint": "https://gateway.example.com",
                        "headers_env": {"Authorization": "VIDEO_EDITING_API_KEY"},
                        "capabilities": ["video_editing"],
                        "model": {},
                    }
                ]
            }
        )


def test_headers_env_rejects_non_environment_name():
    with pytest.raises(ProviderRegistryError, match="uppercase environment names"):
        ConfiguredProviderRegistry.from_dict(
            {
                "providers": [
                    {
                        "name": "video",
                        "kind": "http",
                        "endpoint": "https://gateway.example.com",
                        "headers_env": {"Authorization": "lowercase-secret"},
                        "capabilities": ["video_editing"],
                        "model": {},
                    }
                ]
            }
        )


def test_static_sensitive_headers_are_rejected_in_registry():
    with pytest.raises(ProviderRegistryError, match="headers_env"):
        ConfiguredProviderRegistry.from_dict(
            {
                "providers": [
                    {
                        "name": "video",
                        "kind": "http",
                        "endpoint": "https://gateway.example.com",
                        "headers": {"Authorization": "Bearer hard-coded-secret"},
                        "capabilities": ["video_editing"],
                        "model": {},
                    }
                ]
            }
        )


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), "not-a-number"])
def test_runtime_registry_rejects_non_finite_or_invalid_cost(cost):
    with pytest.raises(ProviderRegistryError, match="cost"):
        ConfiguredProviderRegistry.from_dict(
            {
                "providers": [
                    {
                        "name": "video",
                        "kind": "http",
                        "endpoint": "https://gateway.example.com",
                        "capabilities": [{"name": "video_editing", "cost": {"per_minute": cost}}],
                        "model": {},
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://gateway.example.com",
        "https://user:password@gateway.example.com",
        "https://gateway.example.com?api_key=secret",
        "https://gateway.example.com/path#fragment",
    ],
)
def test_runtime_registry_rejects_unsafe_provider_endpoints(endpoint):
    with pytest.raises(ProviderRegistryError, match="endpoint"):
        ConfiguredProviderRegistry.from_dict(
            {
                "providers": [
                    {
                        "name": "video",
                        "kind": "http",
                        "endpoint": endpoint,
                        "capabilities": ["video_editing"],
                        "model": {},
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("headers", "env_value", "message"),
    [
        ({"": "value"}, None, "invalid header name"),
        ({"X-Trace": ""}, None, "non-empty string"),
        ({"X-Trace": "value\r\ninjected"}, None, "control characters"),
        (None, "value\nwith-break", "control characters"),
    ],
)
def test_runtime_registry_rejects_unsafe_header_names_and_values(headers, env_value, message, monkeypatch):
    if env_value is not None:
        monkeypatch.setenv("VIDEO_HEADER_VALUE", env_value)
    config = {
        "providers": [
            {
                "name": "video",
                "kind": "http",
                "endpoint": "https://gateway.example.com",
                "headers": headers or {},
                "headers_env": {"X-Trace": "VIDEO_HEADER_VALUE"} if env_value is not None else {},
                "capabilities": ["video_editing"],
                "model": {},
            }
        ]
    }
    with pytest.raises(ProviderRegistryError, match=message):
        ConfiguredProviderRegistry.from_dict(config)
