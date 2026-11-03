"""Network-free Provider admission gate for CI and production configuration.

The Provider smoke command is intentionally opt-in and can contact a staging
gateway.  This module is the opposite boundary: it only parses a registry and
checks the production execution modes.  It never constructs an HTTP adapter,
opens a socket, resolves a host, or submits a billable operation.

The gate is useful in pull requests and deployment admission.  A successful
report proves that the registry is structurally usable for ``api_only`` and
that media storage is configured as ``external``; it does not prove provider
availability or generated-media quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .provider_preflight import (
    PreflightCheck,
    ProviderPreflightReport,
    preflight_provider_registry,
)


@dataclass(frozen=True)
class AcceptanceCheck:
    """One production-boundary assertion made by the admission gate."""

    code: str
    status: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ProviderAcceptanceReport:
    """Combined static Provider preflight and production-boundary report."""

    source: str
    preflight: ProviderPreflightReport
    checks: tuple[AcceptanceCheck, ...]

    @property
    def errors(self) -> tuple[AcceptanceCheck | PreflightCheck, ...]:
        """Return static gate and nested preflight failures for CI callers."""

        return tuple(check for check in self.checks if check.status == "error") + self.preflight.errors

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
            "preflight": self.preflight.as_dict(),
            "summary": {
                "passed": sum(check.status == "pass" for check in self.checks)
                + len(self.preflight.passed),
                "warnings": len(self.preflight.warnings),
                "errors": len(self.errors),
            },
        }

    def render_text(self) -> str:
        lines = [f"Provider acceptance gate: {self.source}"]
        for check in self.checks:
            lines.append(f"[{check.status.upper()}] {check.code}: {check.message}")
        lines.append(self.preflight.render_text())
        lines.append(
            f"Acceptance result: {'PASS' if self.ok else 'FAIL'} "
            f"(errors={len(self.errors)} warnings={len(self.preflight.warnings)})"
        )
        return "\n".join(lines)


def _boundary_checks(
    config: Mapping[str, Any],
    *,
    provider_mode: str,
    asset_storage_mode: str,
) -> tuple[AcceptanceCheck, ...]:
    checks: list[AcceptanceCheck] = []
    normalized_provider_mode = str(provider_mode or "").strip().lower()
    if normalized_provider_mode == "api_only":
        checks.append(
            AcceptanceCheck(
                "provider_mode_api_only",
                "pass",
                "AI execution is delegated to registered HTTP Providers",
            )
        )
    else:
        checks.append(
            AcceptanceCheck(
                "provider_mode_api_only",
                "error",
                "production acceptance requires PROVIDER_MODE=api_only",
                {"configured": normalized_provider_mode or None},
            )
        )

    normalized_storage_mode = str(asset_storage_mode or "").strip().lower()
    if normalized_storage_mode == "external":
        checks.append(
            AcceptanceCheck(
                "asset_storage_external",
                "pass",
                "media bytes remain in external object storage",
            )
        )
    else:
        checks.append(
            AcceptanceCheck(
                "asset_storage_external",
                "error",
                "production acceptance requires ASSET_STORAGE_MODE=external",
                {"configured": normalized_storage_mode or None},
            )
        )

    # This check documents an important property of this code path.  The gate
    # receives parsed JSON only and deliberately never builds a Provider
    # adapter, so it cannot make a network request or run local media tools.
    checks.append(
        AcceptanceCheck(
            "network_free_static_gate",
            "pass",
            "registry admission performs no network or media computation",
        )
    )

    raw_providers = config.get("providers") if isinstance(config, Mapping) else None
    local_kinds = [
        str(item.get("kind"))
        for item in raw_providers or []
        if isinstance(item, Mapping) and item.get("kind") != "http"
    ]
    if local_kinds:
        checks.append(
            AcceptanceCheck(
                "http_provider_only",
                "error",
                "api_only acceptance does not permit local or command Provider adapters",
                {"kinds": local_kinds},
            )
        )
    else:
        checks.append(
            AcceptanceCheck(
                "http_provider_only",
                "pass",
                "all configured Provider entries use kind=http",
            )
        )
    return tuple(checks)


def provider_acceptance(
    config: Mapping[str, Any],
    *,
    source: str = "inline",
    required_capabilities: Sequence[str] | None = None,
    require_secrets: bool = True,
    provider_mode: str = "api_only",
    asset_storage_mode: str = "external",
) -> ProviderAcceptanceReport:
    """Run the network-free production admission gate on parsed JSON."""

    preflight = preflight_provider_registry(
        config,
        source=source,
        required_capabilities=required_capabilities,
        require_secrets=require_secrets,
    )
    return ProviderAcceptanceReport(
        source=source,
        preflight=preflight,
        checks=_boundary_checks(
            config,
            provider_mode=provider_mode,
            asset_storage_mode=asset_storage_mode,
        ),
    )


def provider_acceptance_file(
    path: str | Path,
    *,
    required_capabilities: Sequence[str] | None = None,
    require_secrets: bool = True,
    provider_mode: str = "api_only",
    asset_storage_mode: str = "external",
) -> ProviderAcceptanceReport:
    """Read a JSON registry and run :func:`provider_acceptance`."""

    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read provider registry: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"provider registry is not valid JSON: {file_path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"provider registry must be a JSON object: {file_path}")
    return provider_acceptance(
        payload,
        source=str(file_path),
        required_capabilities=required_capabilities,
        require_secrets=require_secrets,
        provider_mode=provider_mode,
        asset_storage_mode=asset_storage_mode,
    )


__all__ = [
    "AcceptanceCheck",
    "ProviderAcceptanceReport",
    "provider_acceptance",
    "provider_acceptance_file",
]
