"""Apply signed asynchronous results from commercial AI providers.

The control plane does not download model output and does not run inference.
Providers upload results to their own/object storage and send a signed callback
containing an immutable URL. This module validates the callback, updates the
durable Job state, and records an Artifact pointer.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from timeline_engine import RenderManifest, Timeline

from .. import models
from ..config import get_settings
from .budgets import settle_provider_cost
from .jobs import transition_job
from .completion import finish_job, lock_job, store_artifact
from .remote_uri import RemoteURIError, validate_remote_uri
from .analysis_contract import validate_hosted_analysis
from .media_quality import validate_render_quality
from .providers import ProviderExecutionError


CALLBACK_STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
_SENSITIVE_KEYS = frozenset({"authorization", "api_key", "apikey", "token", "secret", "password", "credential"})


def _redact_untrusted(value: Any) -> Any:
    """Remove credential-shaped fields before callback data is persisted.

    Provider callbacks are signed, but they are still an external integration
    boundary. Vendors occasionally echo request headers or opaque metadata in
    a result envelope. Persisting those values would expose credentials from
    the normal tenant artifact/job APIs and leave them in database backups.
    """

    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if any(token in str(key).lower() for token in _SENSITIVE_KEYS)
            else _redact_untrusted(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_untrusted(item) for item in value]
    return value


class ProviderCallbackError(ValueError):
    """Safe validation error for an untrusted provider callback."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def verify_signature(
    raw_body: bytes,
    signature: str | None,
    secret: str | None,
    *,
    timestamp: str | None = None,
    max_age_seconds: int = 300,
) -> bool:
    """Verify a raw-body HMAC, optionally bound to a short-lived timestamp.

    Providers that send ``X-Provider-Timestamp`` sign
    ``<timestamp>.<raw-body>``. The raw-body form remains supported for
    providers without timestamped signatures; event-id idempotency still
    prevents duplicate completion effects.
    """

    if not secret or not signature:
        return False
    value = signature.strip()
    if value.lower().startswith("sha256="):
        value = value.split("=", 1)[1]
    signed = raw_body
    if timestamp is not None:
        try:
            issued = int(timestamp)
        except ValueError:
            return False
        if max_age_seconds > 0 and abs(int(datetime.now(timezone.utc).timestamp()) - issued) > max_age_seconds:
            return False
        signed = timestamp.encode("ascii") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(value.lower(), expected)


def _remote_uri(value: Any) -> str:
    try:
        return validate_remote_uri(value, field="artifact_uri")
    except RemoteURIError as exc:
        raise ProviderCallbackError(str(exc)) from exc


def _callback_event(payload: dict[str, Any]) -> dict[str, str]:
    event_id = str(payload.get("event_id") or payload.get("id") or "").strip()
    state = str(payload.get("status") or "").strip().lower()
    if not event_id:
        raise ProviderCallbackError("event_id is required")
    if state not in CALLBACK_STATES:
        raise ProviderCallbackError(f"unsupported callback status: {state or '<empty>'}")
    return {"event_id": event_id, "status": state}


def apply_callback(
    db: Session,
    *,
    job: models.Job,
    payload: dict[str, Any],
    provider: str | None = None,
) -> tuple[models.Job, bool]:
    """Apply an immutable event, including late charge evidence, atomically."""
    from .recovery_guard import require_active_database
    require_active_database(db)
    lock_job(db, job)
    event = _callback_event(payload)
    if len(event["event_id"]) > 255:
        raise ProviderCallbackError("event_id is too long")
    state = event["status"]
    request_id = str(payload.get("request_id") or "").strip()
    if request_id and request_id != job.id:
        raise ProviderCallbackError("callback request_id does not match the target job", status_code=409)
    operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job.id))
    if get_settings().app_environment == "production" and operation is None:
        raise ProviderCallbackError("callback has no submitted provider operation", status_code=409)
    requested_provider = (operation.provider_key if operation else None) or ((job.payload or {}).get("provider_request") or {}).get("provider")
    callback_provider = str(payload.get("provider") or provider or requested_provider or "legacy").strip()
    if requested_provider and callback_provider != requested_provider:
        raise ProviderCallbackError("callback provider does not match the job", status_code=409)
    if len(callback_provider) > 160:
        raise ProviderCallbackError("provider identity is too long")
    expected_id = (operation.external_job_id if operation else None) or (job.payload or {}).get("provider_job_id")
    external_id = str(payload.get("external_job_id") or "").strip()
    if expected_id and external_id and str(expected_id) != external_id:
        raise ProviderCallbackError("callback external_job_id does not match the pending task", status_code=409)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    existing_event = db.scalar(select(models.ProviderEvent).where(
        models.ProviderEvent.provider_key == callback_provider,
        models.ProviderEvent.event_id == event["event_id"],
    ))
    if existing_event is not None:
        if existing_event.job_id != job.id or existing_event.content_hash != digest:
            raise ProviderCallbackError("event_id was reused with different content or job", status_code=409)
        from .operations import increment_counter
        increment_counter(db,'callback_duplicate')
        return job, True
    event_row = models.ProviderEvent(tenant_id=job.tenant_id, job_id=job.id,
                                     provider_key=callback_provider, event_id=event["event_id"],
                                     content_hash=digest, status=state,
                                     payload_json=_redact_untrusted(payload))
    db.add(event_row)
    db.flush()
    if operation is not None and state in {"succeeded", "failed", "cancelled"}:
        # Remote termination frees execution capacity, including late events.
        # This does not prove its bill: reservation release still treats this
        # submitted operation as chargeable until cost reconciliation.
        operation.state = "terminal_received"
        operation.result_json = {**(operation.result_json or {}), "terminal_event_id": event["event_id"], "terminal_status": state}
    metadata = _redact_untrusted(payload.get("metadata") or {})
    if not isinstance(metadata, dict):
        metadata = {}
    cost = payload.get("cost") if isinstance(payload.get("cost"), dict) else metadata.get("cost", {})
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else metadata.get("usage", {})
    cost = _redact_untrusted(cost) if isinstance(cost, dict) else {}
    usage = _redact_untrusted(usage) if isinstance(usage, dict) else {}
    if job.status in {"succeeded", "failed", "cancelled"}:
        if state == "succeeded" or cost:
            settle_provider_cost(db, job=job, cost=cost, usage=usage, provider=callback_provider,
                                 source="late_callback")
        event_row.disposition = "late_evidence"
        db.flush()
        return job, False
    if operation is not None and external_id:
        operation.external_job_id = external_id
    result = {"event_id": event["event_id"], "status": state,
              "provider": callback_provider, "external_job_id": external_id or None,
              "received_at": datetime.now(timezone.utc).isoformat()}
    if request_id:
        result["request_id"] = request_id
    target = None
    if state == "succeeded":
        uri = _remote_uri(payload.get("artifact_uri") or payload.get("output_uri"))
        kind = str(payload.get("artifact_type") or "provider.artifact")[:80]
        analysis = None
        production = get_settings().app_environment == "production"
        if job.type == "asset.analyze":
            analysis = metadata.get("analysis") or (metadata.get("provenance") or {}).get("analysis")
            if not isinstance(analysis, dict) and not production:
                raise ProviderCallbackError("analysis completion requires inline analysis", status_code=422)
            kind = "analysis"
        budget = settle_provider_cost(db, job=job, cost=cost, usage=usage, provider=callback_provider,
                                      model_id=str(metadata.get("model_id") or "") or None,
                                      model_version=str(metadata.get("model_version") or "") or None,
                                      source="provider_callback")
        result.update({"artifact_uri": uri, "artifact_type": kind, "cost": cost, "usage": usage, "budget": budget.audit})
        media_valid, quality = True, {}
        if production and budget.allowed:
            try:
                if job.type == "asset.analyze":
                    source_asset = db.scalar(select(models.Asset).where(
                        models.Asset.id == job.payload.get("asset_id"), models.Asset.tenant_id == job.tenant_id))
                    if source_asset is None:
                        raise ValueError("analysis source missing")
                    analysis = validate_hosted_analysis(analysis or {}, source_uri=source_asset.uri)
                if job.type.startswith("render."):
                    manifest = RenderManifest.model_validate(job.payload.get("manifest"))
                    if not manifest.verify_hash():
                        raise ValueError("render manifest integrity invalid")
                    timeline = Timeline.model_validate(manifest.source["timeline"])
                    quality = validate_render_quality(metadata.get("quality_control") or
                        (metadata.get("metrics") or {}).get("quality_control") or {}, timeline=timeline, output_uri=uri)
            except (ProviderExecutionError, ValueError, KeyError, TypeError) as exc:
                media_valid = False
                job.failure_code = exc.code if isinstance(exc, ProviderExecutionError) else "provider_media_contract_invalid"
                job.retryable = False
                target = "failed"
                result.update({"error": "Provider output failed its media contract", "failure_code": job.failure_code})
        if budget.allowed and media_valid:
            store_artifact(db, job, kind=kind, uri=uri, metadata={
                "source": "provider_callback", "remote": True, "provider": callback_provider,
                "event_id": event["event_id"], "external_job_id": external_id,
                "metadata": metadata, "cost": cost, "usage": usage, "budget": budget.audit,
                **({"analysis": analysis} if analysis is not None else {}),
                **({"quality_control": quality, "output_sha256": quality["output_sha256"]} if quality else {}),
            })
            if analysis is not None:
                asset = db.scalar(select(models.Asset).where(
                    models.Asset.id == job.payload.get("asset_id"), models.Asset.tenant_id == job.tenant_id,
                ).with_for_update().execution_options(populate_existing=True))
                if asset is None:
                    raise ProviderCallbackError("analysis asset no longer exists", status_code=409)
                asset.metadata_json = {**asset.metadata_json, "analysis": analysis}
                job.payload = {**job.payload, "analysis": analysis}
            if job.type.startswith("render."):
                job.payload = {**job.payload, "output_uri": uri}
            target = "succeeded"
        elif not budget.allowed:
            job.policy_blocked = True
            job.failure_code = "budget_exceeded" if budget.decision != "amount_unknown" else "budget_cost_unknown"
            result.update({"error": "provider cost requires budget reconciliation", "failure_code": job.failure_code, "policy_blocked": True})
            target = "failed"
    elif state in {"failed", "cancelled"}:
        if cost:
            settle_provider_cost(db, job=job, cost=cost, usage=usage, provider=callback_provider, source="provider_callback")
        result["error"] = str(payload.get("error") or f"provider callback {state}")[:2000]
        job.failure_code = str(payload.get("failure_code") or f"provider_callback_{state}")[:120]
        job.retryable = bool(payload.get("retryable", False))
        result.update({"failure_code": job.failure_code, "retryable": job.retryable})
        target = state
    elif state == "running" and job.status == "queued":
        transition_job(db, job, "running")
    job.payload = {**job.payload, "provider_callback": result,
                   **({"provider_job_id": external_id} if external_id else {}),
                   **({"provider_result": result} if state == "succeeded" else {})}
    if target:
        finish_job(db, job, target, result_json=result, error=result.get("error"), failure_code=result.get("failure_code"))
    event_row.disposition = "applied"
    db.flush()
    return job, False
