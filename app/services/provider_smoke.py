"""Opt-in staging smoke checks for the hosted Provider Gateway.

This module deliberately does not hide network or billing side effects.  A
normal invocation validates the mounted registry, selected capability/model,
and remote object references without contacting a provider.  Callers must set
``execute=True`` to submit one request to the configured HTTP gateway.  That
flag is intended for a staging endpoint with a non-billable test operation;
this control plane cannot infer whether an operator's provider account bills a
request.

The result reports contract execution state only.  A successful response
proves that the configured gateway returned a contract-valid artifact; it does
not claim model quality, visual fidelity, voice similarity, or any other
quality outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .provider_adapters import HTTPProvider, HttpTransport
from .provider_registry import ConfiguredProviderRegistry, ProviderRegistryError
from .providers import (
    ProviderExecutionError,
    ProviderExecutionPending,
    ProviderRequest,
    ProviderResult,
)
from .remote_uri import RemoteURIError, validate_remote_uri


SmokeStatus = Literal["pass", "fail", "pending"]


@dataclass(frozen=True)
class SmokeCheck:
    """One bounded validation result with no provider response body."""

    name: str
    status: Literal["pass", "fail"]
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class ProviderSmokeResult:
    """Safe machine-readable result for CI/staging acceptance checks."""

    status: SmokeStatus
    executed: bool
    provider: str
    capability: str
    model_id: str | None
    model_version: str | None
    request_id: str
    checks: tuple[SmokeCheck, ...] = ()
    artifact_uri: str | None = None
    provider_job_id: str | None = None
    poll_after_seconds: float | None = None
    error_code: str | None = None
    error: str | None = None
    # Explicitly avoid implying anything about generated media quality.
    quality_evidence: str = "not_evaluated"
    quality_claim: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "executed": self.executed,
            "provider": self.provider,
            "capability": self.capability,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "request_id": self.request_id,
            "checks": [check.as_dict() for check in self.checks],
            "artifact_uri": self.artifact_uri,
            "provider_job_id": self.provider_job_id,
            "poll_after_seconds": self.poll_after_seconds,
            "error_code": self.error_code,
            "error": self.error,
            "quality_evidence": self.quality_evidence,
            "quality_claim": self.quality_claim,
        }

    def render_text(self) -> str:
        action = "executed" if self.executed else "dry-run"
        lines = [
            f"Provider smoke: {self.status.upper()} ({action})",
            f"provider={self.provider} capability={self.capability} "
            f"model={self.model_id or '-'}@{self.model_version or '-'} request_id={self.request_id}",
        ]
        for check in self.checks:
            detail = f" ({check.detail})" if check.detail else ""
            lines.append(f"  [{check.status.upper()}] {check.name}{detail}")
        if self.provider_job_id:
            lines.append(f"provider_job_id={self.provider_job_id}")
        if self.poll_after_seconds is not None:
            lines.append(f"poll_after_seconds={self.poll_after_seconds:g}")
        if self.error_code:
            lines.append(f"error={self.error_code}: {self.error or 'provider execution failed'}")
        lines.append("quality_evidence=not_evaluated quality_claim=false")
        return "\n".join(lines)


def _failure_result(
    *,
    provider: str,
    capability: str,
    model_id: str | None,
    model_version: str | None,
    request_id: str,
    checks: list[SmokeCheck],
    executed: bool,
    error: Exception,
    status: SmokeStatus = "fail",
) -> ProviderSmokeResult:
    provider_job_id = None
    poll_after_seconds = None
    error_code = None
    message = str(error) or error.__class__.__name__
    if isinstance(error, ProviderExecutionError):
        error_code = error.code
        if isinstance(error.details, dict):
            raw_job_id = error.details.get("provider_job_id")
            provider_job_id = str(raw_job_id) if raw_job_id else None
            raw_poll_after = error.details.get("poll_after_seconds")
            if isinstance(raw_poll_after, (int, float)) and raw_poll_after >= 0:
                poll_after_seconds = float(raw_poll_after)
    elif isinstance(error, ProviderRegistryError):
        error_code = "provider_registry_error"
    elif isinstance(error, ValueError):
        error_code = "provider_validation_error"
    return ProviderSmokeResult(
        status=status,
        executed=executed,
        provider=provider,
        capability=capability,
        model_id=model_id,
        model_version=model_version,
        request_id=request_id,
        checks=tuple(checks),
        provider_job_id=provider_job_id,
        poll_after_seconds=poll_after_seconds,
        error_code=error_code,
        error=message,
    )


def _validate_remote_inputs(inputs: dict[str, Any]) -> list[SmokeCheck]:
    checks: list[SmokeCheck] = []
    uri_fields = [key for key in inputs if "uri" in str(key).lower()]
    if not uri_fields:
        return [SmokeCheck("remote_input_refs", "fail", "at least one URI input is required")]
    for uri_field in uri_fields:
        value = inputs[uri_field]
        values = value if isinstance(value, (list, tuple)) else [value]
        if not values:
            checks.append(SmokeCheck("remote_input_refs", "fail", f"{uri_field} is empty"))
            continue
        for item in values:
            try:
                validate_remote_uri(str(item), field=str(uri_field))
            except RemoteURIError as exc:
                checks.append(SmokeCheck("remote_input_refs", "fail", str(exc)))
                break
        else:
            checks.append(SmokeCheck("remote_input_refs", "pass", f"{uri_field} validated"))
    return checks


def run_provider_smoke(
    registry: ConfiguredProviderRegistry,
    *,
    provider_name: str,
    capability: str,
    request_id: str,
    inputs: dict[str, Any],
    parameters: dict[str, Any] | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    tenant_id: str | None = None,
    region: str | None = None,
    execute: bool = False,
    provider_job_id: str | None = None,
    transport: HttpTransport | None = None,
) -> ProviderSmokeResult:
    """Run a dry-run or one explicitly requested staging gateway operation.

    ``provider_job_id`` resumes a previously accepted asynchronous operation;
    the existing ``HTTPProvider`` then polls without sending a second POST.
    ``transport`` is only for in-process contract tests and should never be
    passed by production callers.
    """

    checks: list[SmokeCheck] = []
    model_snapshot = None
    provider: Any = None
    request_model_id = model_id
    request_model_version = model_version
    try:
        if not request_id or not request_id.strip():
            raise ProviderRegistryError("request_id must be a stable non-empty string")
        if not isinstance(inputs, dict):
            raise ProviderRegistryError("inputs must be an object")
        provider = next((item for item in registry.providers if getattr(item, "name", None) == provider_name), None)
        registration = next((item for item in registry.registrations if item.name == provider_name), None)
        if provider is None or registration is None:
            raise ProviderRegistryError(f"provider is not configured: {provider_name}")
        checks.append(SmokeCheck("provider_configured", "pass", provider_name))
        if not isinstance(provider, HTTPProvider):
            raise ProviderRegistryError("provider smoke requires an HTTP Provider Gateway")
        capability_record = next((item for item in registration.capabilities if item.name == capability), None)
        if capability_record is None:
            raise ProviderRegistryError(f"capability is not configured for provider: {capability}")
        checks.append(SmokeCheck("capability_configured", "pass", capability))
        model_snapshot = getattr(provider, "model", None)
        if model_snapshot is None:
            raise ProviderRegistryError("provider model identity is not configured")
        request_model_id = request_model_id or model_snapshot.model_id
        request_model_version = request_model_version or model_snapshot.version
        if request_model_id != model_snapshot.model_id or request_model_version != model_snapshot.version:
            raise ProviderRegistryError("requested model does not match configured provider model")
        checks.append(SmokeCheck("model_configured", "pass", f"{request_model_id}@{request_model_version}"))
        checks.extend(_validate_remote_inputs(inputs))
        if any(check.status == "fail" for check in checks):
            raise ProviderRegistryError("remote input references failed validation")
        checks.append(SmokeCheck("request_identity", "pass", "stable request_id supplied"))

        request_parameters = dict(parameters or {})
        if provider_job_id:
            if not str(provider_job_id).strip():
                raise ProviderRegistryError("provider_job_id must be non-empty when supplied")
            request_parameters["provider_job_id"] = str(provider_job_id)
            checks.append(SmokeCheck("resume_task", "pass", "polls existing provider job"))
        request = ProviderRequest(
            request_id=request_id,
            capability=capability,  # type: ignore[arg-type]
            inputs=dict(inputs),
            parameters=request_parameters,
            tenant_id=tenant_id,
            region=region,
            provider=provider_name,
            model_id=request_model_id,
            model_version=request_model_version,
            commercial_use=True,
        )
        # Routing/policy validation does not contact the gateway.
        registry.router().choose(request)
        checks.append(SmokeCheck("provider_policy", "pass", "capability/model policy accepted"))
    except (ProviderRegistryError, ProviderExecutionError, ValueError) as exc:
        return _failure_result(
            provider=provider_name,
            capability=capability,
            model_id=request_model_id,
            model_version=request_model_version,
            request_id=request_id,
            checks=checks,
            executed=False,
            error=exc,
        )

    if not execute:
        return ProviderSmokeResult(
            status="pass",
            executed=False,
            provider=provider_name,
            capability=capability,
            model_id=request_model_id,
            model_version=request_model_version,
            request_id=request_id,
            checks=tuple(checks),
        )

    original_transport = provider._transport
    if transport is not None:
        provider._transport = transport
    try:
        try:
            result: ProviderResult = provider.execute(request)
        except ProviderExecutionPending as exc:
            return _failure_result(
                provider=provider_name,
                capability=capability,
                model_id=request_model_id,
                model_version=request_model_version,
                request_id=request_id,
                checks=checks,
                executed=True,
                error=exc,
                status="pending",
            )
        except ProviderExecutionError as exc:
            return _failure_result(
                provider=provider_name,
                capability=capability,
                model_id=request_model_id,
                model_version=request_model_version,
                request_id=request_id,
                checks=checks,
                executed=True,
                error=exc,
            )
        except Exception as exc:  # noqa: BLE001 - smoke result must be machine-readable
            return _failure_result(
                provider=provider_name,
                capability=capability,
                model_id=request_model_id,
                model_version=request_model_version,
                request_id=request_id,
                checks=checks,
                executed=True,
                error=ProviderExecutionError(
                    "provider smoke execution failed",
                    code="provider_smoke_execution_error",
                    provider=provider_name,
                    capability=capability,
                    retryable=False,
                    details={"error_type": exc.__class__.__name__},
                ),
            )
        return ProviderSmokeResult(
            status="pass",
            executed=True,
            provider=provider_name,
            capability=capability,
            model_id=request_model_id,
            model_version=request_model_version,
            request_id=request_id,
            checks=tuple(checks),
            artifact_uri=result.artifact_uri,
            provider_job_id=(result.provenance or {}).get("provider_job_id"),
        )
    finally:
        provider._transport = original_transport


__all__ = ["ProviderSmokeResult", "SmokeCheck", "run_provider_smoke"]
