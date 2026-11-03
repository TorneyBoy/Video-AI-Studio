"""Configuration driven Provider and model registration.

The registry is deliberately JSON based so deployments can mount a secret
free configuration file without importing vendor SDKs into the control plane.
Commands are argv templates and are never passed through a shell.  HTTP
providers receive the same typed :class:`ProviderRequest` contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from .provider_adapters import CommandProvider, HTTPProvider
from .model_registry import ModelRegistry, ModelVersion
from .providers import (
    ModelSnapshot,
    Provider,
    ProviderCapability,
    ProviderHealth,
    ProviderRouter,
    ProviderRequest,
)


class ProviderRegistryError(ValueError):
    """Raised when operator configuration cannot be made executable safely."""


_ALLOWED_TEMPLATE_KEYS = {
    "output_path",
    "input_path",
    "audio_input_path",
    "request_id",
    "capability",
    "prompt",
    "text",
    "asset_id",
    "source_asset_id",
    "voice_profile_id",
    "avatar_profile_id",
}
_SENSITIVE_KEYS = {"authorization", "api_key", "apikey", "token", "secret", "password", "credential"}
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _sensitive_header_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return any(token in normalized for token in ("authorization", "apikey", "token", "secret", "password", "credential"))


def _validate_endpoint(value: Any, provider: str) -> str:
    """Validate the runtime HTTP endpoint before constructing an adapter.

    ``provider-preflight`` is the deployment admission check, but the worker
    also loads a mounted registry directly.  Keeping the URL boundary strict
    here prevents a stale or bypassed registry from sending credentials to an
    HTTP endpoint or to a URL whose query/fragment changes the signed route.
    """
    if not isinstance(value, str) or not value.strip():
        raise ProviderRegistryError(f"providers.{provider}.endpoint must be a non-empty HTTPS URL")
    endpoint = value.strip()
    try:
        parsed = urlparse(endpoint)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ProviderRegistryError(f"providers.{provider}.endpoint is not a valid URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc or not hostname:
        raise ProviderRegistryError(f"providers.{provider}.endpoint must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderRegistryError(
            f"providers.{provider}.endpoint must not contain credentials, query parameters, or fragments"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in endpoint):
        raise ProviderRegistryError(f"providers.{provider}.endpoint must not contain control characters")
    return endpoint


def _validate_header_name(value: Any, *, provider: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or not _HEADER_NAME.fullmatch(value.strip()):
        raise ProviderRegistryError(f"providers.{provider}.{field} contains an invalid header name")
    return value.strip()


def _validate_header_value(value: Any, *, provider: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderRegistryError(f"providers.{provider}.{field} must be a non-empty string")
    if any((ord(character) < 0x20 and character != "\t") or ord(character) == 0x7F for character in value):
        raise ProviderRegistryError(f"providers.{provider}.{field} must not contain control characters")
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if any(part in str(key).lower() for part in _SENSITIVE_KEYS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _string(value: Any, field: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ProviderRegistryError(f"{field} must be a non-empty string")
    return value.strip()


def _headers(data: Any, env_data: Any, provider: str, *, resolve_secrets: bool = True) -> dict[str, str]:
    """Resolve non-secret headers and secret-manager environment references.

    Registry files are commonly mounted as ConfigMaps or committed as an
    example.  Keeping credentials out of that JSON is therefore the default
    production path: ``headers_env`` maps a header name to an environment
    variable populated by the deployment secret manager.  Values are resolved
    once when the registry is loaded and are never returned by ``as_dict``.
    """
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ProviderRegistryError(f"providers.{provider}.headers must be an object")
    headers: dict[str, str] = {}
    for key, value in data.items():
        header = _validate_header_name(key, provider=provider, field="headers")
        if _sensitive_header_name(header):
            raise ProviderRegistryError(
                f"providers.{provider}.headers must not contain secret-bearing header {header!r}; use headers_env"
            )
        headers[header] = _validate_header_value(value, provider=provider, field=f"headers[{header}]")
    if env_data is None:
        env_data = {}
    if not isinstance(env_data, Mapping):
        raise ProviderRegistryError(f"providers.{provider}.headers_env must be an object")
    for header, env_name in env_data.items():
        normalized_header = _validate_header_name(header, provider=provider, field="headers_env")
        if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name.strip()):
            raise ProviderRegistryError(
                f"providers.{provider}.headers_env values must be uppercase environment names"
            )
        if not resolve_secrets:
            continue
        value = os.getenv(env_name.strip())
        if not value or not value.strip():
            raise ProviderRegistryError(
                f"providers.{provider}.headers_env requires environment variable: {env_name.strip()}"
            )
        headers[normalized_header] = _validate_header_value(
            value,
            provider=provider,
            field=f"headers_env[{normalized_header}]",
        )
    return headers


def _model_snapshot(data: Mapping[str, Any], provider: str) -> ModelSnapshot:
    if not isinstance(data, Mapping):
        raise ProviderRegistryError(f"providers.{provider}.model must be an object")
    license_snapshot = data.get("license_snapshot") or {}
    if not isinstance(license_snapshot, Mapping):
        raise ProviderRegistryError(f"providers.{provider}.model.license_snapshot must be an object")
    return ModelSnapshot(
        provider=_string(data.get("provider"), "model.provider", default=provider),
        model_id=_string(data.get("model_id"), "model.model_id", default="configured"),
        version=_string(data.get("version"), "model.version", default="unknown"),
        weights_sha256=data.get("weights_sha256"),
        runtime_digest=data.get("runtime_digest"),
        license_snapshot=_redact(license_snapshot),
    )


def _capabilities(data: Any, provider: str) -> tuple[ProviderCapability, ...]:
    if not isinstance(data, list) or not data:
        raise ProviderRegistryError(f"providers.{provider}.capabilities must be a non-empty array")
    result: list[ProviderCapability] = []
    for item in data:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, Mapping):
            raise ProviderRegistryError(f"providers.{provider}.capabilities entries must be objects or strings")
        name = _string(item.get("name"), "capability.name")
        # Capability is a Literal at runtime; config validation still rejects
        # unknown names so a typo cannot create an unroutable job.
        known = {
            "media_analysis", "shot_analysis", "transcription", "translation",
            "voice_tts", "voice_clone", "voice_conversion", "video_generation",
            "video_editing", "motion_transfer", "face_transform", "lip_sync", "enhancement",
        }
        if name not in known:
            raise ProviderRegistryError(f"unknown provider capability: {name}")
        input_types = tuple(str(value) for value in (item.get("input_types") or ["json"]))
        output_types = tuple(str(value) for value in (item.get("output_types") or ["json"]))
        cost = item.get("cost") if isinstance(item.get("cost"), Mapping) else {}
        estimated_per_minute = item.get("estimated_cost_per_minute", cost.get("per_minute"))
        estimated_per_request = item.get("estimated_cost_per_request", cost.get("per_request"))
        currency = str(item.get("cost_currency", cost.get("currency", "USD"))).upper()
        billing_unit = str(item.get("billing_unit", cost.get("billing_unit", "minute")))
        for label, value in (("per minute", estimated_per_minute), ("per request", estimated_per_request)):
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ProviderRegistryError(
                    f"providers.{provider}.capabilities cost {label} must be numeric"
                ) from exc
            if not math.isfinite(number) or number < 0:
                raise ProviderRegistryError(
                    f"providers.{provider}.capabilities cost {label} must be finite and non-negative"
                )
        if not currency or len(currency) > 12:
            raise ProviderRegistryError(f"providers.{provider}.capabilities cost_currency is invalid")
        if not billing_unit:
            raise ProviderRegistryError(f"providers.{provider}.capabilities billing_unit is invalid")
        acceptance = item.get("acceptance") or {}
        limits = item.get("limits") or {}
        if not isinstance(acceptance, Mapping) or not isinstance(limits, Mapping):
            raise ProviderRegistryError("capability acceptance and limits must be objects")
        if acceptance.get("status", "unverified") not in {"unverified", "testing", "verified", "suspended"}:
            raise ProviderRegistryError("unsupported capability acceptance status")
        for value in (acceptance.get("report_ref"), acceptance.get("model_version"), item.get("price_version")):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ProviderRegistryError("capability evidence and price versions must be non-empty strings")
        result.append(
            ProviderCapability(
                name=name,  # type: ignore[arg-type]
                input_types=input_types,
                output_types=output_types,
                quality_score=float(item.get("quality_score", 0.0)),
                acceptance_status=str(acceptance.get("status", "unverified")),
                acceptance_report=acceptance.get("report_ref"),
                acceptance_model_version=acceptance.get("model_version"),
                limits=dict(limits),
                price_version=item.get("price_version"),
                estimated_cost_per_minute=float(estimated_per_minute) if estimated_per_minute is not None else None,
                cost_currency=currency,
                billing_unit=billing_unit,
                estimated_cost_per_request=float(estimated_per_request) if estimated_per_request is not None else None,
                regions=tuple(str(value) for value in (item.get("regions") or [])),
                commercial_allowed=bool(item.get("commercial_allowed", False)),
            )
        )
    return tuple(result)


def _request_value(request: ProviderRequest, key: str, output_path: Path) -> str:
    if key == "output_path":
        return str(output_path)
    if key == "input_path":
        value = request.inputs.get("input_path") or request.inputs.get("source_uri") or request.inputs.get("asset_uri")
    else:
        value = request.inputs.get(key)
    if value is None:
        value = request.parameters.get(key)
    if value is None:
        if request.request_id == "health-check":
            # Health checks only inspect argv[0]; unresolved media inputs do
            # not need to be valid paths and must never run the command.
            return ""
        raise ProviderRegistryError(f"command template requires request value: {key}")
    return str(value)


def command_builder_from_template(template: tuple[str, ...]):
    if not template or any(not isinstance(part, str) or not part for part in template):
        raise ProviderRegistryError("command must be a non-empty argv array")

    def build(request: ProviderRequest, output_path: Path) -> list[str]:
        values = {
            key: _request_value(request, key, output_path)
            for key in _ALLOWED_TEMPLATE_KEYS
            if "{" + key + "}" in " ".join(template)
        }
        rendered: list[str] = []
        for part in template:
            try:
                rendered.append(part.format_map(values))
            except KeyError as exc:
                raise ProviderRegistryError(f"unknown command template key: {exc.args[0]}") from exc
        return rendered

    return build


@dataclass(frozen=True)
class ProviderRegistration:
    name: str
    kind: str
    model: ModelSnapshot
    capabilities: tuple[ProviderCapability, ...]
    timeout_seconds: float
    enabled: bool = True
    poll_path: str | None = None
    poll_interval_seconds: float = 2.0
    poll_timeout_seconds: float | None = None
    max_poll_attempts: int = 30
    callback_configured: bool = False


class ConfiguredProviderRegistry:
    """Build and inspect adapters from a mounted JSON registration file."""

    def __init__(self, providers: list[Provider], registrations: list[ProviderRegistration]):
        self.providers = providers
        self.registrations = registrations

    @classmethod
    def from_dict(cls, config: Mapping[str, Any]) -> "ConfiguredProviderRegistry":
        raw_providers = config.get("providers") if isinstance(config, Mapping) else None
        if not isinstance(raw_providers, list):
            raise ProviderRegistryError("provider registry must contain a providers array")
        providers: list[Provider] = []
        registrations: list[ProviderRegistration] = []
        names: set[str] = set()
        for raw in raw_providers:
            if not isinstance(raw, Mapping):
                raise ProviderRegistryError("provider entries must be objects")
            name = _string(raw.get("name"), "provider.name")
            if name in names:
                raise ProviderRegistryError(f"duplicate provider name: {name}")
            names.add(name)
            kind = _string(raw.get("kind"), f"providers.{name}.kind")
            capabilities = _capabilities(raw.get("capabilities"), name)
            model = _model_snapshot(raw.get("model") or {}, name)
            timeout = float(raw.get("timeout_seconds", 120.0))
            if timeout <= 0:
                raise ProviderRegistryError(f"providers.{name}.timeout_seconds must be positive")
            enabled = bool(raw.get("enabled", True))
            if kind == "command":
                command = raw.get("command")
                if not isinstance(command, list):
                    raise ProviderRegistryError(f"providers.{name}.command must be an argv array")
                provider: Provider = CommandProvider(
                    name=name,
                    capabilities=capabilities,
                    command_builder=command_builder_from_template(tuple(str(part) for part in command)),
                    model=model,
                    artifact_type=str(raw.get("artifact_type") or "provider.artifact"),
                    timeout_seconds=timeout,
                )
            elif kind == "http":
                endpoint = _validate_endpoint(raw.get("endpoint"), name)
                headers = _headers(raw.get("headers"), raw.get("headers_env"), name, resolve_secrets=enabled)
                poll_path = raw.get("poll_path")
                if poll_path is not None and (not isinstance(poll_path, str) or not poll_path.strip()):
                    raise ProviderRegistryError(f"providers.{name}.poll_path must be a non-empty string")
                poll_interval = float(raw.get("poll_interval_seconds", 2.0))
                poll_timeout = raw.get("poll_timeout_seconds")
                poll_timeout_value = float(poll_timeout) if poll_timeout is not None else None
                max_poll_attempts = int(raw.get("max_poll_attempts", 30))
                if poll_interval < 0 or (poll_timeout_value is not None and poll_timeout_value <= 0):
                    raise ProviderRegistryError(f"providers.{name} polling intervals must be valid positive values")
                if max_poll_attempts < 1:
                    raise ProviderRegistryError(f"providers.{name}.max_poll_attempts must be at least one")
                callback_secret_env = raw.get("callback_secret_env")
                if callback_secret_env is not None and (not isinstance(callback_secret_env, str) or not callback_secret_env.strip()):
                    raise ProviderRegistryError(f"providers.{name}.callback_secret_env must be a non-empty string")
                if callback_secret_env is not None and not _ENV_NAME.fullmatch(callback_secret_env.strip()):
                    raise ProviderRegistryError(
                        f"providers.{name}.callback_secret_env must be an uppercase environment name"
                    )
                callback_secret = os.getenv(callback_secret_env) if callback_secret_env and enabled else None
                provider = HTTPProvider(
                    name=name,
                    base_url=endpoint,
                    capabilities=capabilities,
                    path=str(raw.get("path") or "/v1/generate"),
                    headers=dict(headers),
                    timeout_seconds=timeout,
                    health_path=str(raw.get("health_path") or "/health"),
                    model=model,
                    poll_path=poll_path,
                    poll_interval_seconds=poll_interval,
                    poll_timeout_seconds=poll_timeout_value,
                    max_poll_attempts=max_poll_attempts,
                    callback_secret=callback_secret,
                )
            else:
                raise ProviderRegistryError(f"unsupported provider kind: {kind}")
            provider.enabled = enabled
            providers.append(provider)
            registrations.append(
                ProviderRegistration(
                    name=name,
                    kind=kind,
                    model=model,
                    capabilities=capabilities,
                    timeout_seconds=timeout,
                    enabled=enabled,
                    poll_path=getattr(provider, "poll_path", None),
                    poll_interval_seconds=float(getattr(provider, "poll_interval_seconds", 2.0)),
                    poll_timeout_seconds=getattr(provider, "poll_timeout_seconds", None),
                    max_poll_attempts=int(getattr(provider, "max_poll_attempts", 30)),
                    callback_configured=bool(getattr(provider, "callback_secret", None)),
                )
            )
        return cls(providers, registrations)

    @classmethod
    def from_file(cls, path: str | Path) -> "ConfiguredProviderRegistry":
        file_path = Path(path)
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProviderRegistryError(f"unable to read provider registry: {file_path}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderRegistryError(f"provider registry is not valid JSON: {file_path}") from exc
        return cls.from_dict(payload)

    @classmethod
    def from_env(cls) -> "ConfiguredProviderRegistry":
        raw = os.getenv("PROVIDER_REGISTRY_JSON")
        path = os.getenv("PROVIDER_REGISTRY_PATH")
        if raw:
            try:
                return cls.from_dict(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ProviderRegistryError("PROVIDER_REGISTRY_JSON is not valid JSON") from exc
        if path:
            return cls.from_file(path)
        return cls([], [])

    def router(self, *, health_check: bool = False, probe: bool = False) -> ProviderRouter:
        if health_check:
            self.health(probe=probe)
        return ProviderRouter(list(self.providers), model_registry=self.model_registry())

    def model_registry(self) -> ModelRegistry:
        """Build the policy registry represented by this provider config."""
        records: list[ModelVersion] = []
        for registration in self.registrations:
            license_data = registration.model.license_snapshot
            integrity_mode = str(license_data.get("integrity_mode") or "weights")
            if integrity_mode == "provider_version" and registration.kind != "http":
                raise ProviderRegistryError("provider_version integrity evidence is only valid for HTTP providers")
            commercial_allowed = bool(license_data.get("commercial_allowed", False))
            status = str(license_data.get("status") or ("approved" if commercial_allowed else "draft"))
            if status not in {"draft", "approved", "suspended", "retired"}:
                raise ProviderRegistryError(f"unsupported model status: {status}")
            records.append(
                ModelVersion(
                    provider=registration.model.provider,
                    model_id=registration.model.model_id,
                    version=registration.model.version,
                    capabilities=tuple(cap.name for cap in registration.capabilities),
                    repo_url=license_data.get("repo_url"),
                    commit_sha=license_data.get("commit_sha"),
                    code_license=license_data.get("code_license"),
                    weights_license=license_data.get("weights_license"),
                    dataset_terms=license_data.get("dataset_terms"),
                    third_party_licenses=tuple(license_data.get("third_party_licenses") or ()),
                    commercial_allowed=commercial_allowed,
                    allowed_regions=tuple(license_data.get("allowed_regions") or ()),
                    content_restrictions=tuple(license_data.get("content_restrictions") or ()),
                    runtime_digest=registration.model.runtime_digest,
                    weights_sha256=registration.model.weights_sha256,
                    integrity_mode=integrity_mode,
                    integrity_reference=license_data.get("integrity_reference"),
                    status=status,  # type: ignore[arg-type]
                )
            )
        return ModelRegistry(records)

    def health(self, *, probe: bool = False) -> list[ProviderHealth]:
        """Return provider health, optionally without contacting the network.

        ``probe=False`` is the safe default for operational dashboards and
        liveness surfaces; ``probe=True`` is reserved for an explicit
        staging/operator check.
        """
        results: list[ProviderHealth] = []
        for provider in self.providers:
            if not getattr(provider, "enabled", True):
                results.append(
                    ProviderHealth(
                        provider=getattr(provider, "name", provider.__class__.__name__),
                        available=False,
                        status="disabled",
                        reason="operator_disabled",
                        model=getattr(provider, "model", None),
                    )
                )
                continue
            check = getattr(provider, "health_check", None)
            if callable(check):
                try:
                    result = check(probe=probe)
                except TypeError:
                    result = check()
            else:
                result = ProviderHealth(
                    provider=getattr(provider, "name", provider.__class__.__name__),
                    available=False,
                    status="unknown",
                    reason="adapter does not expose health_check",
                )
            results.append(result)
        return results

    def as_dict(self, *, health_check: bool = False, probe: bool = False) -> dict[str, Any]:
        status_by_name = {item.provider: item.as_dict() for item in self.health(probe=probe)} if health_check else {}
        return {
            "providers": [
                {
                    "name": registration.name,
                    "kind": registration.kind,
                    "enabled": registration.enabled,
                    "timeout_seconds": registration.timeout_seconds,
                    "poll_path": registration.poll_path,
                    "poll_interval_seconds": registration.poll_interval_seconds,
                    "poll_timeout_seconds": registration.poll_timeout_seconds,
                    "max_poll_attempts": registration.max_poll_attempts,
                    "callback_configured": registration.callback_configured,
                    "capabilities": [
                        {
                            "name": cap.name,
                            "quality_score": cap.quality_score,
                            "estimated_cost_per_minute": cap.estimated_cost_per_minute,
                            "estimated_cost_per_request": cap.estimated_cost_per_request,
                            "cost_currency": cap.cost_currency,
                            "billing_unit": cap.billing_unit,
                        }
                        for cap in registration.capabilities
                    ],
                    "model": registration.model.as_dict(),
                    **({"health": status_by_name[registration.name]} if registration.name in status_by_name else {}),
                }
                for registration in self.registrations
            ]
        }

