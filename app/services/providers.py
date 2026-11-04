"""Provider Gateway contracts.

The control plane depends on these typed contracts rather than importing a
specific model. Hosted commercial APIs implement the production boundary and
can be routed by capability, license, region, cost and quality score. A local
deterministic adapter remains available only as an explicit test mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Literal, Protocol

from ..config import get_settings


Capability = Literal[
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
    "face_transform", "person_replacement",
    "lip_sync",
    "enhancement",
]


@dataclass(frozen=True)
class ProviderCapability:
    name: Capability
    input_types: tuple[str, ...]
    output_types: tuple[str, ...]
    quality_score: float = 0.0
    estimated_cost_per_minute: float | None = None
    cost_currency: str = "USD"
    billing_unit: str = "minute"
    estimated_cost_per_request: float | None = None
    regions: tuple[str, ...] = ()
    commercial_allowed: bool = False
    acceptance_status: str = "unverified"
    acceptance_report: str | None = None
    acceptance_model_version: str | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    price_version: str | None = None


@dataclass(frozen=True)
class ModelSnapshot:
    provider: str
    model_id: str
    version: str
    weights_sha256: str | None = None
    runtime_digest: str | None = None
    license_snapshot: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the immutable model identity used in Artifact manifests.

        Provider responses are untrusted input.  Keeping this conversion in
        one place makes it harder for adapters to accidentally omit a model
        version or persist an opaque SDK object in JSON.
        """
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "version": self.version,
            "weights_sha256": self.weights_sha256,
            "runtime_digest": self.runtime_digest,
            "license_snapshot": dict(self.license_snapshot),
        }


@dataclass(frozen=True)
class ProviderRequest:
    request_id: str
    capability: Capability
    inputs: dict[str, Any]
    parameters: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
    consent_ids: tuple[str, ...] = ()
    region: str | None = None
    # A client may request a concrete adapter.  The router still validates
    # that the adapter advertises the requested capability and is allowed by
    # the commercial policy before dispatching to it.
    provider: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    commercial_use: bool = True


@dataclass(frozen=True)
class ProviderResult:
    artifact_uri: str
    artifact_type: str
    model: ModelSnapshot
    metrics: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    status: str = "succeeded"
    warnings: tuple[str, ...] = ()
    # Provider billing is intentionally opaque and provider-specific.  Keep
    # the normalized usage/cost envelope so the control plane can report and
    # reconcile invoices without hard-coding a vendor SDK.
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCallbackEvent:
    """Normalized webhook event from a hosted model service.

    The API layer may persist this event and wake the same Job.  It is kept
    separate from ``ProviderResult`` because callbacks can arrive before an
    artifact is available, or can report a terminal failure.
    """

    provider: str
    request_id: str
    provider_job_id: str
    status: str
    artifact_uri: str | None = None
    artifact_type: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)


class ProviderExecutionError(LookupError):
    """A safe-to-persist provider failure with retry guidance.

    Provider SDK exceptions tend to contain implementation details that are
    not useful to API clients.  Adapters should raise this error (or let the
    worker wrap an unexpected exception) so the job and step can persist a
    stable audit record.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_execution_failed",
        provider: str | None = None,
        capability: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.capability = capability
        self.retryable = retryable
        self.details = details or {}
        # Internal evidence only; never included in the public error details.
        self.rejected_result: ProviderResult | None = None


class ProviderExecutionPending(ProviderExecutionError):
    """A hosted provider accepted work that must be polled later.

    The stable ``request_id`` is used as the idempotency key on a retry.  The
    worker persists ``details.provider_job_id`` and passes it back to the
    adapter, so a retry resumes polling instead of submitting duplicate work.
    """

    def __init__(
        self,
        message: str = "provider job is still processing",
        *,
        provider: str | None = None,
        capability: str | None = None,
        provider_job_id: str | None = None,
        poll_after_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        if provider_job_id:
            merged.setdefault("provider_job_id", provider_job_id)
        if poll_after_seconds is not None:
            merged.setdefault("poll_after_seconds", poll_after_seconds)
        super().__init__(
            message,
            code="provider_async_pending",
            provider=provider,
            capability=capability,
            retryable=True,
            details=merged,
        )


class ProviderConfigurationError(ValueError):
    """Raised when the selected compute policy cannot be satisfied safely."""


class Provider(Protocol):
    """Minimal provider contract implemented by every model adapter."""

    @property
    def capabilities(self) -> tuple[ProviderCapability, ...]: ...

    def execute(self, request: ProviderRequest) -> ProviderResult: ...

    def health_check(self, *, probe: bool = False) -> "ProviderHealth": ...


@dataclass(frozen=True)
class ProviderHealth:
    """Safe, low-cardinality availability result for an adapter.

    ``probe`` is intentionally reported separately from ``status``. A
    configuration-only health read must remain network-free, while an
    operator can request an explicit endpoint probe during staging.
    ``latency_ms`` is rounded and bounded to avoid turning this into a
    high-cardinality tracing surface.
    """

    provider: str
    available: bool
    status: str
    reason: str | None = None
    model: ModelSnapshot | None = None
    probed: bool = False
    latency_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "status": self.status,
            "reason": self.reason,
            "model": self.model.as_dict() if self.model else None,
            "probed": self.probed,
            "latency_ms": self.latency_ms,
        }


class ProviderRouter:
    """Deterministic capability router used by the local and GPU worker planes."""

    def __init__(
        self,
        providers: list[Provider] | None = None,
        *,
        model_registry: object | None = None,
        commercial_use: bool = True,
    ):
        self.providers = providers or []
        self.model_registry = model_registry
        self.commercial_use = commercial_use

    def register(self, provider: Provider) -> None:
        self.providers.append(provider)

    def health(self, *, probe: bool = False) -> list[ProviderHealth]:
        """Return safe availability results for all registered adapters.

        ``probe=False`` is the safe default for worker and control-plane reads.
        Staging operators must pass ``probe=True`` to contact a third-party
        endpoint explicitly.
        """
        results: list[ProviderHealth] = []
        for provider in self.providers:
            name = getattr(provider, "name", provider.__class__.__name__)
            if not getattr(provider, "enabled", True):
                results.append(ProviderHealth(name, False, "disabled", "operator_disabled", getattr(provider, "model", None)))
                continue
            check = getattr(provider, "health_check", None)
            if callable(check):
                try:
                    results.append(check(probe=probe))
                except TypeError:
                    # Compatibility for injected contract-test providers that
                    # still implement the pre-probe health_check() signature.
                    results.append(check())
            else:
                results.append(ProviderHealth(name, False, "unknown", "adapter_does_not_support_health"))
        return results

    def choose(self, request: ProviderRequest) -> Provider:
        candidates = []
        policy_errors: list[ProviderExecutionError] = []
        for provider in self.providers:
            provider_name = getattr(provider, "name", provider.__class__.__name__)
            if not getattr(provider, "enabled", True):
                continue
            # A registry may perform health checks out of band and mark a
            # provider unavailable.  Do not silently dispatch to a known bad
            # endpoint; callers receive the stable provider_unavailable code.
            if getattr(provider, "available", True) is False:
                continue
            if request.provider and provider_name != request.provider:
                continue
            for capability in provider.capabilities:
                if capability.name != request.capability:
                    continue
                if request.region and capability.regions and request.region not in capability.regions:
                    continue
                if request.commercial_use and not capability.commercial_allowed:
                    continue
                registered_model = None
                if self.model_registry is not None:
                    strict_registry = bool(getattr(self.model_registry, "strict", True))
                    model_snapshot = getattr(provider, "model", None) or getattr(provider, "model_snapshot", None)
                    model_id = request.model_id or getattr(model_snapshot, "model_id", None)
                    model_version = request.model_version or getattr(model_snapshot, "version", None)
                    registered_model = self.model_registry.resolve(
                        provider=provider_name,
                        capability=request.capability,
                        model_id=model_id,
                        version=model_version,
                    )
                    if registered_model is None and strict_registry:
                        policy_errors.append(
                            ProviderExecutionError(
                                "provider model is not registered for this capability",
                                code="model_unregistered",
                                provider=provider_name,
                                capability=request.capability,
                                retryable=False,
                                details={"model_id": model_id, "version": model_version},
                            )
                        )
                        continue
                    if registered_model is not None:
                        try:
                            self.model_registry.validate_request(
                                registered_model,
                                request,
                                commercial_use=request.commercial_use and self.commercial_use,
                            )
                        except ProviderExecutionError as exc:
                            policy_errors.append(exc)
                            continue
                candidates.append((capability.quality_score, provider, registered_model))
        if not candidates:
            if policy_errors:
                raise policy_errors[0]
            if request.provider:
                raise ProviderExecutionError(
                    f"provider '{request.provider}' is unavailable for capability={request.capability}",
                    code="provider_unavailable",
                    provider=request.provider,
                    capability=request.capability,
                    retryable=False,
                )
            raise ProviderExecutionError(
                f"no commercial provider for capability={request.capability}",
                code="capability_unavailable",
                capability=request.capability,
                retryable=False,
            )
        return max(candidates, key=lambda item: item[0])[1]

    def execute(self, request: ProviderRequest) -> ProviderResult:
        provider = self.choose(request)
        result = provider.execute(request)
        try:
            if self.model_registry is not None:
                model_snapshot = getattr(provider, "model", None) or getattr(provider, "model_snapshot", None)
                model_id = request.model_id or getattr(model_snapshot, "model_id", None) or result.model.model_id
                model_version = request.model_version or getattr(model_snapshot, "version", None) or result.model.version
                expected = self.model_registry.resolve(
                    provider=getattr(provider, "name", provider.__class__.__name__),
                    capability=request.capability,
                    model_id=model_id,
                    version=model_version,
                )
                if expected is None and bool(getattr(self.model_registry, "strict", True)):
                    raise ProviderExecutionError(
                        "provider model is not registered for this capability",
                        code="model_unregistered",
                        provider=getattr(provider, "name", provider.__class__.__name__),
                        capability=request.capability,
                        retryable=False,
                    )
                if expected is not None:
                    self.model_registry.validate_snapshot(expected, result.model, request)
        except ProviderExecutionError as exc:
            # A rejected output can still have incurred a charge. Keep its
            # known remote identity and billing evidence without accepting it
            # for delivery or placing artifact data in public error messages.
            exc.rejected_result = result
            remote_id = result.provenance.get("provider_job_id")
            if remote_id:
                exc.details = {**exc.details, "provider_job_id": str(remote_id)}
            raise
        return result


_CAPABILITY_ARTIFACT_TYPES: dict[str, str] = {
    "voice_tts": "audio.render_manifest",
    "voice_clone": "audio.clone_manifest",
    "voice_conversion": "audio.conversion_manifest",
    "video_generation": "video.generation_manifest",
    "motion_transfer": "video.motion_manifest",
    "face_transform": "video.face_manifest",
    "lip_sync": "video.lip_sync_manifest",
    "enhancement": "video.enhancement_manifest",
}


class LocalDeterministicProvider:
    """A transparent local adapter used until GPU/commercial adapters exist.

    It does not claim to synthesize a finished production asset.  Instead it
    emits a provider result manifest, which makes every specialized job
    executable and auditable in a CPU-only development environment.  A real
    adapter can later replace this class without changing the Job/Artifact
    contract.
    """

    name = "local-deterministic"

    def __init__(self) -> None:
        self.enabled = True
        self.available = True
        self.model = ModelSnapshot(
            provider=self.name,
            model_id="deterministic-manifest",
            version="1",
            weights_sha256=hashlib.sha256(b"video-ai-studio/local-deterministic-manifest/v1").hexdigest(),
            runtime_digest="python-standard-library",
            license_snapshot={
                "mode": "development",
                "code_license": "internal-development",
                "weights_license": "no-weights",
                "commercial_allowed": True,
                "status": "approved",
            },
        )
        capabilities: list[ProviderCapability] = []
        for capability in _CAPABILITY_ARTIFACT_TYPES:
            capabilities.append(
                ProviderCapability(
                    capability, ("json",), ("json",), quality_score=0.1, commercial_allowed=True
                )
            )
        self.capabilities = tuple(capabilities)

    def execute(self, request: ProviderRequest) -> ProviderResult:
        if request.capability not in _CAPABILITY_ARTIFACT_TYPES:
            raise ProviderExecutionError(
                f"unsupported capability={request.capability}",
                code="unsupported_capability",
                provider=self.name,
                capability=request.capability,
            )
        self._validate(request)
        output_dir = request.parameters.get("output_dir")
        if not output_dir:
            raise ProviderExecutionError(
                "provider output_dir is required",
                code="missing_output_directory",
                provider=self.name,
                capability=request.capability,
            )
        directory = Path(str(output_dir))
        if os.name == "nt" and not str(directory).startswith("\\\\?\\"):
            # Deep test/development workspaces may exceed Win32 MAX_PATH.
            directory = Path("\\\\?\\" + str(directory.resolve()))
        directory.mkdir(parents=True, exist_ok=True)
        safe_request_id = re.sub(r"[^A-Za-z0-9_.-]", "_", request.request_id)
        output_path = directory / f"{safe_request_id}.json"
        model = self.model
        body = {
            "schema_version": "provider-result/1.0",
            "request_id": request.request_id,
            "capability": request.capability,
            "provider": self.name,
            "status": "simulated",
            "inputs": request.inputs,
            "parameters": {key: value for key, value in request.parameters.items() if key != "output_dir"},
            "model": {
                "provider": model.provider,
                "model_id": model.model_id,
                "version": model.version,
                "runtime_digest": model.runtime_digest,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            output_path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        except OSError as exc:
            raise ProviderExecutionError(
                f"unable to write provider output: {exc}",
                code="provider_output_write_failed",
                provider=self.name,
                capability=request.capability,
                retryable=True,
            ) from exc
        return ProviderResult(
            artifact_uri=str(output_path),
            artifact_type=_CAPABILITY_ARTIFACT_TYPES[request.capability],
            model=model,
            metrics={"simulated": True},
            provenance={"request_id": request.request_id, "consent_ids": list(request.consent_ids)},
            status="simulated",
            warnings=("local deterministic adapter emitted a manifest; no generative model was run",),
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            available=True,
            status="simulated",
            reason="development adapter emits manifests and does not run a model",
            model=self.model,
        )

    @staticmethod
    def _validate(request: ProviderRequest) -> None:
        required: dict[str, tuple[str, ...]] = {
            "voice_tts": ("text",),
            "voice_clone": ("text", "voice_profile_id"),
            "voice_conversion": ("text", "voice_profile_id"),
            "video_generation": ("prompt",),
            "motion_transfer": ("source_asset_id", "avatar_profile_id"),
            "face_transform": ("source_asset_id", "avatar_profile_id"),
            "lip_sync": ("source_asset_id", "avatar_profile_id"),
            "enhancement": ("asset_id",),
        }.get(request.capability, ())
        missing = [key for key in required if not request.inputs.get(key)]
        if missing:
            raise ProviderExecutionError(
                f"missing provider inputs: {', '.join(missing)}",
                code="invalid_provider_input",
                provider=LocalDeterministicProvider.name,
                capability=request.capability,
                retryable=False,
                details={"missing": missing},
            )
        if request.capability == "enhancement" and not request.inputs.get("rights_confirmed"):
            raise ProviderExecutionError(
                "rights confirmation is required for enhancement",
                code="rights_not_confirmed",
                provider=LocalDeterministicProvider.name,
                capability=request.capability,
                retryable=False,
            )


def default_provider_router(
    *,
    include_configured: bool = True,
    health_check: bool = False,
    mode: str | None = None,
) -> ProviderRouter:
    """Build the worker router under an explicit provider compute policy.

    ``api_only`` is the production-safe default: only configured HTTP
    adapters are eligible and no local deterministic or command provider is
    silently inserted. ``development_mock`` is an explicit opt-in for local
    contract tests; its deterministic adapter emits JSON manifests only.
    """
    effective_mode = str(mode or get_settings().provider_mode).strip().lower()
    if effective_mode not in {"api_only", "development_mock"}:
        raise ProviderConfigurationError(
            "provider mode must be api_only or development_mock"
        )

    providers: list[Provider] = []
    configured = None
    if include_configured:
        # Lazy import avoids a module cycle: the registry imports the adapter
        # implementations, while this module owns the base contracts.
        from .provider_registry import ConfiguredProviderRegistry

        configured = ConfiguredProviderRegistry.from_env()
        if effective_mode == "api_only":
            local_registrations = [
                registration.name
                for registration in configured.registrations
                if registration.kind != "http"
            ]
            if local_registrations:
                names = ", ".join(sorted(local_registrations))
                raise ProviderConfigurationError(
                    "api_only mode only allows HTTP Provider adapters; "
                    f"local/command providers are configured: {names}"
                )
        if health_check:
            configured.health()
        providers.extend(configured.providers)
    if effective_mode == "development_mock":
        # Keep this insertion after configured providers so a real adapter
        # with a better quality score wins when both are available.
        providers.insert(0, LocalDeterministicProvider())
    from .model_registry import ModelRegistry, development_registry

    model_registry = development_registry()
    if configured is not None:
        configured_models = configured.model_registry().all()
        configured_keys = {item.key for item in configured_models}
        local_models = [item for item in model_registry.all() if item.key not in configured_keys]
        # A mounted config may intentionally override a local registration.
        model_registry = ModelRegistry([*local_models, *configured_models])
    return ProviderRouter(providers, model_registry=model_registry)
