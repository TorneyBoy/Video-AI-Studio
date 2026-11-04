"""Client-safe capability catalogue, with independently verified rollout."""

from __future__ import annotations

import hashlib
import json
import math

from ..config import get_settings
from .provider_registry import ConfiguredProviderRegistry, ProviderRegistryError
from .providers import ProviderExecutionError, ProviderRequest
from .model_requests import public_contract, public_routing


PUBLIC_LIMITS = {
    "max_input_bytes", "max_duration_seconds", "max_output_duration_seconds",
    "max_inputs", "max_concurrent", "aspect_ratios", "resolutions", "formats", "features", "feature_combinations",
    "generation_resolutions", "allowed_durations", "generation_modes", "min_reference_inputs",
    "max_reference_inputs", "max_prompt_chars", "max_negative_prompt_chars", "output_fps_by_mode",
    "request_contract", "model_routing",
}

INTEGER_LIMITS = {"min_reference_inputs", "max_reference_inputs", "max_prompt_chars", "max_negative_prompt_chars"}


def _public_limits(values: dict) -> dict:
    limits = {}
    for key, value in values.items():
        if key not in PUBLIC_LIMITS:
            continue
        if key in {"request_contract", "model_routing"}:
            public = (public_contract if key == "request_contract" else public_routing)(value)
            if public is not None:
                limits[key] = public
        elif key == "output_fps_by_mode":
            if not isinstance(value, dict):
                continue
            limits[key] = {mode: fps for mode, fps in list(value.items())[:64]
                           if isinstance(mode, str) and 0 < len(mode) <= 100 and mode == mode.strip()
                           and all(32 <= ord(character) < 127 for character in mode)
                           and isinstance(fps, (int, float)) and not isinstance(fps, bool)
                           and math.isfinite(fps) and 1 <= fps <= 120}
        elif key in INTEGER_LIMITS:
            if type(value) is int and value >= 0:
                limits[key] = value
        elif key.startswith("max_"):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                limits[key] = value
        elif isinstance(value, list):
            if key == "allowed_durations":
                limits[key] = sorted({item for item in value[:64]
                                      if isinstance(item, (int, float)) and not isinstance(item, bool)
                                      and math.isfinite(item) and item > 0})
            elif key in {"generation_resolutions", "generation_modes"}:
                limits[key] = sorted({item for item in value[:64]
                                      if isinstance(item, str) and 0 < len(item) <= 100
                                      and item == item.strip() and all(ord(character) >= 32 and ord(character) != 127 for character in item)})
            elif key == "feature_combinations":
                # Each complete row describes one accepted combination. Never
                # flatten rows: independent acceptance does not prove mixing.
                limits[key] = [sorted(set(row)) for row in value[:64] if isinstance(row, list)
                               and 0 < len(row) <= 64
                               and all(isinstance(item, str) and 0 < len(item) <= 100 for item in row)]
            elif key == "resolutions":
                limits[key] = [{field: item[field] for field in ("width", "height")}
                               for item in value if isinstance(item, dict)
                               and all(isinstance(item.get(field), int) and item[field] > 0 for field in ("width", "height"))]
            else:
                limits[key] = [item for item in value if isinstance(item, str) and len(item) <= 100]
    return limits


def capability_catalog(registry: ConfiguredProviderRegistry | None = None) -> dict:
    """Read configuration without health probes or secret-bearing output."""
    registry = registry if registry is not None else ConfiguredProviderRegistry.from_env()
    models = registry.model_registry()
    entries = []
    for registration in registry.registrations:
        for capability in registration.capabilities:
            accepted = (capability.acceptance_status == "verified" and bool(capability.acceptance_report)
                        and capability.acceptance_model_version == registration.model.version)
            priced = (bool(capability.price_version)
                      and (capability.estimated_cost_per_minute is not None or capability.estimated_cost_per_request is not None)
                      and len(capability.cost_currency) == 3 and capability.cost_currency.isalpha())
            status = "available"
            if not registration.enabled:
                status = "disabled"
            elif get_settings().provider_mode == "api_only" and registration.kind != "http":
                status = "unsupported_execution_mode"
            elif not capability.commercial_allowed:
                status = "policy_unavailable"
            elif not accepted:
                status = "awaiting_acceptance"
            elif not priced:
                status = "awaiting_price"
            else:
                model = models.get(registration.name, registration.model.model_id, registration.model.version)
                try:
                    models.validate_request(model, ProviderRequest(
                        request_id="catalog-policy", capability=capability.name, inputs={},
                        provider=registration.name, commercial_use=True))
                except ProviderExecutionError:
                    status = "policy_unavailable"
            limits = _public_limits(capability.limits)
            if any(key in capability.limits and key not in limits for key in ("request_contract", "model_routing")):
                # Invalid model contracts cannot fall through to the old Wan
                # parameter rules after their public projection is removed.
                status = "invalid_model_contract"
            entries.append({
                "id": f"{registration.name}:{capability.name}:{registration.model.version}",
                "capability": capability.name, "provider": registration.name,
                "model_id": registration.model.model_id, "model_version": registration.model.version,
                "status": status, "available": status == "available",
                "input_types": list(capability.input_types), "output_types": list(capability.output_types),
                "limits": limits, "regions": list(capability.regions),
                "price": {"version": capability.price_version, "currency": capability.cost_currency,
                          "billing_unit": capability.billing_unit,
                          "per_minute": capability.estimated_cost_per_minute,
                          "per_request": capability.estimated_cost_per_request},
            })
    revision = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"revision": revision, "capabilities": entries}


def require_available_capability(name: str, provider: str | None = None,
                                 model_id: str | None = None, model_version: str | None = None) -> dict:
    try:
        entries = capability_catalog()["capabilities"]
    except (ProviderRegistryError, ValueError, OSError) as exc:
        raise ProviderExecutionError("capability configuration is unavailable", code="capability_configuration_unavailable", retryable=False) from exc
    choices = [item for item in entries if item["available"] and item["capability"] == name
               and (not provider or item["provider"] == provider)
               and (not model_id or item["model_id"] == model_id)
               and (not model_version or item["model_version"] == model_version)]
    if not choices:
        raise ProviderExecutionError("capability is not open for user execution", code="capability_not_available", capability=name, retryable=False)
    return sorted(choices, key=lambda item: item["id"])[0]
