"""Static, network-free validation for hosted Provider registrations.

The preflight command intentionally stops at the control-plane boundary.  It
parses the mounted registry and checks secret *references* and policy metadata,
but it never calls a Provider endpoint and never prints an environment value.
This makes it suitable for CI, deployment admission checks, and a safe first
step before a separately authorized smoke test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


KNOWN_CAPABILITIES = frozenset(
    {
        "media_analysis",
        "shot_analysis",
        "transcription",
        "translation",
        "voice_tts",
        "voice_clone",
        "voice_conversion",
        "video_generation",
        "video_editing",
        "motion_transfer",
        "face_transform",
        "lip_sync",
        "enhancement",
    }
)

# The production hosted surface currently promises these capabilities.  The
# command accepts a narrower set for a staged rollout via required_capabilities.
HOSTED_CAPABILITIES = frozenset(
    {
        "media_analysis",
        "video_editing",
        "video_generation",
        "voice_tts",
        "voice_clone",
        "voice_conversion",
        "motion_transfer",
        "face_transform",
        "lip_sync",
        "enhancement",
    }
)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_UNKNOWN_MODEL_VALUES = frozenset({"unknown", "configured", "latest", "unspecified", ""})


def _sensitive_header_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return any(
        token in normalized
        for token in ("authorization", "apikey", "token", "secret", "password", "credential")
    )


def _valid_header_name(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEADER_NAME.fullmatch(value.strip()))


def _safe_header_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(
        (ord(character) < 0x20 and character != "\t") or ord(character) == 0x7F
        for character in value
    )


@dataclass(frozen=True)
class PreflightCheck:
    """One safe, machine-readable result from a registry check."""

    code: str
    status: str
    message: str
    provider: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        # Details are assembled only from configuration names/statuses.  Never
        # add an environment value here: this object is safe to print as JSON.
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "provider": self.provider,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ProviderPreflightReport:
    """Summary returned by :func:`preflight_provider_registry`."""

    source: str
    checks: tuple[PreflightCheck, ...]
    required_capabilities: tuple[str, ...]
    provider_count: int

    @property
    def errors(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.status == "error")

    @property
    def warnings(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.status == "warning")

    @property
    def passed(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.status == "pass")

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "provider_count": self.provider_count,
            "required_capabilities": list(self.required_capabilities),
            "summary": {
                "passed": len(self.passed),
                "warnings": len(self.warnings),
                "errors": len(self.errors),
            },
            "checks": [check.as_dict() for check in self.checks],
        }

    def render_text(self) -> str:
        lines = [
            f"Provider registry preflight: {self.source}",
            f"Providers: {self.provider_count}; required capabilities: {len(self.required_capabilities)}",
        ]
        for check in self.checks:
            scope = f" provider={check.provider}" if check.provider else ""
            lines.append(f"[{check.status.upper()}] {check.code}{scope}: {check.message}")
        lines.append(
            "Summary: "
            f"passed={len(self.passed)} warnings={len(self.warnings)} errors={len(self.errors)} "
            f"result={'PASS' if self.ok else 'FAIL'}"
        )
        return "\n".join(lines)


def _add(
    checks: list[PreflightCheck],
    code: str,
    status: str,
    message: str,
    *,
    provider: str | None = None,
    **details: Any,
) -> None:
    checks.append(PreflightCheck(code, status, message, provider, details))


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _env_reference(
    checks: list[PreflightCheck],
    *,
    provider: str,
    field: str,
    env_name: Any,
    require_value: bool,
) -> bool:
    if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name.strip()):
        _add(
            checks,
            "invalid_secret_env_name",
            "error",
            f"{field} must be an uppercase environment variable name",
            provider=provider,
        )
        return False
    normalized = env_name.strip()
    configured = bool(os.getenv(normalized))
    if not configured and require_value:
        _add(
            checks,
            "missing_secret_env",
            "error",
            f"{field} references an unset environment variable",
            provider=provider,
            field=field,
            env_name=normalized,
            configured=False,
        )
    elif not configured:
        _add(
            checks,
            "missing_secret_env",
            "warning",
            f"{field} references an unset environment variable",
            provider=provider,
            field=field,
            env_name=normalized,
            configured=False,
        )
    else:
        _add(
            checks,
            "secret_env_configured",
            "pass",
            f"{field} references a configured environment variable",
            provider=provider,
            field=field,
            env_name=normalized,
            configured=True,
        )
    return True


def _check_cost(
    checks: list[PreflightCheck],
    *,
    provider: str,
    capability: str,
    item: Mapping[str, Any],
) -> bool:
    raw_cost = item.get("cost")
    cost = raw_cost if isinstance(raw_cost, Mapping) else {}
    per_minute = item.get("estimated_cost_per_minute", cost.get("per_minute"))
    per_request = item.get("estimated_cost_per_request", cost.get("per_request"))
    configured_units: list[str] = []
    for name, value in (("per_minute", per_minute), ("per_request", per_request)):
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            _add(
                checks,
                "invalid_cost_value",
                "error",
                f"{capability} cost {name} must be numeric",
                provider=provider,
                capability=capability,
                unit=name,
            )
            return False
        if not math.isfinite(number) or number < 0:
            _add(
                checks,
                "invalid_cost_value",
                "error",
                f"{capability} cost {name} must be finite and non-negative",
                provider=provider,
                capability=capability,
                unit=name,
            )
            return False
        configured_units.append(name)
    if not configured_units:
        _add(
            checks,
            "missing_cost_config",
            "error",
            f"{capability} must define cost.per_minute or cost.per_request",
            provider=provider,
            capability=capability,
        )
        return False
    currency = item.get("cost_currency", cost.get("currency", "USD"))
    billing_unit = item.get("billing_unit", cost.get("billing_unit"))
    if not _non_empty_string(currency) or len(str(currency).strip()) > 12:
        _add(
            checks,
            "invalid_cost_currency",
            "error",
            f"{capability} cost currency must be a short non-empty code",
            provider=provider,
            capability=capability,
        )
        return False
    if not _non_empty_string(billing_unit):
        _add(
            checks,
            "missing_billing_unit",
            "error",
            f"{capability} cost must define billing_unit",
            provider=provider,
            capability=capability,
        )
        return False
    _add(
        checks,
        "cost_configured",
        "pass",
        f"{capability} has a non-negative cost estimate",
        provider=provider,
        capability=capability,
        units=configured_units,
        currency=str(currency).strip().upper(),
        billing_unit=str(billing_unit).strip(),
    )
    return True


def preflight_provider_registry(
    config: Mapping[str, Any],
    *,
    source: str = "inline",
    required_capabilities: Sequence[str] | None = None,
    require_secrets: bool = True,
) -> ProviderPreflightReport:
    """Validate a hosted registry without network calls or secret disclosure.

    ``require_secrets=False`` is intended for pull-request checks against a
    template. Production admission should retain the default ``True`` so an
    unset secret manager reference fails before workers start.
    """

    required = tuple(sorted(set(required_capabilities or HOSTED_CAPABILITIES)))
    checks: list[PreflightCheck] = []
    for capability in required:
        if capability not in KNOWN_CAPABILITIES:
            _add(
                checks,
                "required_capability_unknown",
                "error",
                "required capability is not supported by the Provider contract",
                capability=capability,
            )
    raw_providers = config.get("providers") if isinstance(config, Mapping) else None
    if not isinstance(raw_providers, list):
        _add(checks, "providers_array_missing", "error", "registry must contain a providers array")
        return ProviderPreflightReport(source, tuple(checks), required, 0)

    names: set[str] = set()
    covered: dict[str, set[str]] = {capability: set() for capability in required}
    for index, raw in enumerate(raw_providers):
        if not isinstance(raw, Mapping):
            _add(checks, "provider_entry_invalid", "error", f"provider entry {index} must be an object")
            continue
        name = str(raw.get("name") or f"provider[{index}]").strip()
        if not _non_empty_string(raw.get("name")):
            _add(checks, "provider_name_missing", "error", "provider name must be non-empty", provider=name)
        elif name in names:
            _add(checks, "duplicate_provider_name", "error", "provider name is duplicated", provider=name)
        names.add(name)

        kind = raw.get("kind")
        if kind != "http":
            _add(
                checks,
                "provider_kind_not_http",
                "error",
                "api_only production registrations must use kind=http",
                provider=name,
                kind=kind,
            )

        endpoint = raw.get("endpoint")
        try:
            parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
            hostname = parsed.hostname if parsed is not None else None
        except ValueError:
            parsed = None
            hostname = None
        if parsed is None or parsed.scheme.lower() != "https" or not parsed.netloc:
            _add(
                checks,
                "endpoint_not_https",
                "error",
                "provider endpoint must be an absolute HTTPS URL",
                provider=name,
            )
        elif (
            not hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in endpoint)
        ):
            _add(
                checks,
                "endpoint_contains_credentials_or_query",
                "error",
                "provider endpoint must not contain credentials, query parameters, fragments, or control characters",
                provider=name,
            )
        else:
            _add(
                checks,
                "endpoint_https",
                "pass",
                "provider endpoint uses HTTPS",
                provider=name,
                host=hostname,
            )

        headers_env = raw.get("headers_env")
        if not isinstance(headers_env, Mapping) or not headers_env:
            _add(
                checks,
                "headers_env_missing",
                "error",
                "HTTP Provider must reference credentials through headers_env",
                provider=name,
            )
        else:
            for header, env_name in headers_env.items():
                if not _valid_header_name(header):
                    _add(
                        checks,
                        "invalid_header_name",
                        "error",
                        "headers_env header names must be valid HTTP field names",
                        provider=name,
                    )
                    continue
                valid_env = _env_reference(
                    checks,
                    provider=name,
                    field=f"headers_env[{header}]",
                    env_name=env_name,
                    require_value=require_secrets,
                )
                if valid_env and require_secrets and not _safe_header_value(os.getenv(str(env_name).strip())):
                    _add(
                        checks,
                        "invalid_header_value",
                        "error",
                        "headers_env value must be non-empty and must not contain control characters",
                        provider=name,
                        header=header,
                    )

        static_headers = raw.get("headers")
        if static_headers is not None and not isinstance(static_headers, Mapping):
            _add(
                checks,
                "headers_invalid",
                "error",
                "headers must be an object",
                provider=name,
            )
        elif isinstance(static_headers, Mapping):
            for header, value in static_headers.items():
                if not _valid_header_name(header):
                    _add(
                        checks,
                        "invalid_header_name",
                        "error",
                        "headers header names must be valid HTTP field names",
                        provider=name,
                    )
                    continue
                if not _safe_header_value(value):
                    _add(
                        checks,
                        "invalid_header_value",
                        "error",
                        "headers values must be non-empty and must not contain control characters",
                        provider=name,
                        header=header,
                    )
                if _sensitive_header_name(header):
                    _add(
                        checks,
                        "static_secret_header",
                        "error",
                        "secret-bearing headers must use headers_env, never registry JSON values",
                        provider=name,
                        header=header,
                    )

        callback_env = raw.get("callback_secret_env")
        if callback_env is None:
            _add(
                checks,
                "callback_secret_env_missing",
                "error",
                "async HTTP Provider must define callback_secret_env",
                provider=name,
            )
        else:
            _env_reference(
                checks,
                provider=name,
                field="callback_secret_env",
                env_name=callback_env,
                require_value=require_secrets,
            )

        model = raw.get("model")
        if not isinstance(model, Mapping):
            _add(checks, "model_missing", "error", "provider model metadata is required", provider=name)
        else:
            model_id = model.get("model_id")
            version = model.get("version")
            if not _non_empty_string(model_id) or str(model_id).strip().lower() in _UNKNOWN_MODEL_VALUES:
                _add(checks, "model_id_missing", "error", "model.model_id must identify a concrete model", provider=name)
            if not _non_empty_string(version) or str(version).strip().lower() in _UNKNOWN_MODEL_VALUES:
                _add(
                    checks,
                    "model_version_missing",
                    "error",
                    "model.version must identify a pinned release",
                    provider=name,
                )
            license_snapshot = model.get("license_snapshot")
            if not isinstance(license_snapshot, Mapping):
                _add(checks, "license_snapshot_missing", "error", "model.license_snapshot is required", provider=name)
            else:
                if license_snapshot.get("commercial_allowed") is not True:
                    _add(
                        checks,
                        "commercial_license_denied",
                        "error",
                        "model license_snapshot must explicitly allow commercial use",
                        provider=name,
                    )
                elif "status" in license_snapshot and license_snapshot.get("status") != "approved":
                    _add(
                        checks,
                        "model_not_approved",
                        "error",
                        "model license_snapshot.status must be approved for production",
                        provider=name,
                        license_status=license_snapshot.get("status"),
                    )
                elif not _non_empty_string(license_snapshot.get("code_license")) or not _non_empty_string(
                    license_snapshot.get("weights_license")
                ):
                    _add(
                        checks,
                        "license_metadata_incomplete",
                        "error",
                        "commercial models must record code_license and weights_license",
                        provider=name,
                    )
                else:
                    _add(
                        checks,
                        "commercial_license_recorded",
                        "pass",
                        "model version has explicit commercial license metadata",
                        provider=name,
                        model_id=str(model_id).strip(),
                        version=str(version).strip(),
                    )

        capabilities = raw.get("capabilities")
        valid_capabilities: set[str] = set()
        if not isinstance(capabilities, list) or not capabilities:
            _add(checks, "capabilities_missing", "error", "provider capabilities must be a non-empty array", provider=name)
        else:
            for entry in capabilities:
                item = {"name": entry} if isinstance(entry, str) else entry
                if not isinstance(item, Mapping):
                    _add(checks, "capability_entry_invalid", "error", "capability entries must be objects or strings", provider=name)
                    continue
                capability = item.get("name")
                if not _non_empty_string(capability) or str(capability).strip() not in KNOWN_CAPABILITIES:
                    _add(
                        checks,
                        "unknown_capability",
                        "error",
                        "capability name is not supported by the Provider contract",
                        provider=name,
                        capability=capability,
                    )
                    continue
                capability = str(capability).strip()
                if item.get("commercial_allowed") is not True:
                    _add(
                        checks,
                        "capability_commercial_denied",
                        "error",
                        f"{capability} must be commercially allowed",
                        provider=name,
                        capability=capability,
                    )
                cost_ok = _check_cost(checks, provider=name, capability=capability, item=item)
                if item.get("commercial_allowed") is True and cost_ok and bool(raw.get("enabled", True)):
                    valid_capabilities.add(capability)
                _add(
                    checks,
                    "capability_registered",
                    "pass",
                    f"{capability} is registered",
                    provider=name,
                    capability=capability,
                )
        for capability in valid_capabilities & set(required):
            covered[capability].add(name)

        poll_path = raw.get("poll_path")
        if not _non_empty_string(poll_path):
            _add(
                checks,
                "poll_path_missing",
                "error",
                "hosted asynchronous Provider must define poll_path",
                provider=name,
            )
        else:
            _add(checks, "polling_configured", "pass", "bounded polling path is configured", provider=name)

    for capability, providers in covered.items():
        if not providers:
            _add(
                checks,
                "capability_uncovered",
                "error",
                f"no enabled commercial Provider covers {capability}",
                capability=capability,
            )
        else:
            _add(
                checks,
                "capability_covered",
                "pass",
                f"{capability} has an enabled commercial Provider",
                capability=capability,
                providers=sorted(providers),
            )
    return ProviderPreflightReport(source, tuple(checks), required, len(raw_providers))


def preflight_provider_registry_file(
    path: str | Path,
    *,
    required_capabilities: Sequence[str] | None = None,
    require_secrets: bool = True,
) -> ProviderPreflightReport:
    """Read a JSON registry and return a safe preflight report."""

    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read provider registry: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"provider registry is not valid JSON: {file_path}") from exc
    return preflight_provider_registry(
        payload,
        source=str(file_path),
        required_capabilities=required_capabilities,
        require_secrets=require_secrets,
    )
