"""Safe, structured failure projections for Jobs and durable Workflows.

Provider and worker exceptions are intentionally persisted as an audit JSON
record so operators can investigate what happened.  That record may contain
provider-specific fields, however, and is not a suitable public API payload.
This module defines the small, stable projection returned to clients and keeps
sensitive values (credentials, URLs, prompts and local paths) out of it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
import re
from typing import Any

# These values are useful for retry UX and support triage while remaining
# safe to expose to a tenant administrator.  Provider adapters can add new
# fields to the internal audit without accidentally making them public.
_SAFE_DETAIL_KEYS = frozenset(
    {
        "provider_job_id",
        "poll_after_seconds",
        "http_status",
        "status",
        "error_type",
        "field",
        "reason",
        "attempt",
        "max_attempts",
        "request_id",
        "external_job_id",
        "category",
    }
)
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "credential",
        "signature",
        "cookie",
        "header",
        "url",
        "uri",
        "path",
        "prompt",
        "text",
        "input",
        "output",
    }
)
_PAYLOAD_SECRET_PARTS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "credential",
        "signature",
        "cookie",
    }
)
_PAYLOAD_PRIVATE_PARTS = frozenset({"url", "uri", "path", "prompt", "text"})
_PAYLOAD_PRIVATE_ENVELOPES = frozenset(
    {"provider_request", "provider_response", "provider_result", "provider_callback", "provider_failure"}
)
_MAX_STRING = 500
# Plans and analysis can legitimately contain more than twenty shots. Keep a
# generous bound for a public response while still preventing an accidental
# unbounded provider payload from becoming an API response.
_MAX_LIST_ITEMS = 2000
_URL_RE = re.compile(r"(?i)\b(?:https?|s3|gs|oss|az)://[^\s]+")
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s\\/]+[\\/])+[^\s]+")
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(api[-_ ]?key|authorization|bearer|token|secret|password)\b"
    r"\s*(?:[:=]\s*|\s+)(?:bearer\s+)?[^\s,;]+"
)


def _payload_secret_key(key: object) -> bool:
    """Match credential fields without treating ``input_tokens`` as a secret."""

    normalized = str(key).strip().lower().replace("-", "_")
    return any(
        normalized == part
        or normalized.startswith(f"{part}_")
        or normalized.endswith(f"_{part}")
        or f"_{part}_" in normalized
        for part in _PAYLOAD_SECRET_PARTS
    )


def _safe_key(key: object) -> bool:
    value = str(key).strip().lower()
    if not value or any(part in value for part in _SENSITIVE_KEY_PARTS):
        return False
    return value in _SAFE_DETAIL_KEYS


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return safe_failure_message(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if _safe_key(key)
        }
    if isinstance(value, (list, tuple)):
        # Lists are only retained when their scalar members are harmless.  A
        # nested map still goes through the same allow-list above.
        return [_safe_value(item) for item in list(value)[:_MAX_LIST_ITEMS]]
    return str(value)[:_MAX_STRING]


def safe_failure_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return an allow-listed and bounded provider detail object."""

    if not isinstance(value, Mapping):
        return {}
    result = _safe_value(value)
    return result if isinstance(result, dict) else {}


def safe_failure_message(value: object, *, fallback: str = "job failed") -> str:
    """Bound and redact a human-readable error string for API consumers."""

    message = str(value or fallback).replace("\x00", " ").strip()
    message = _URL_RE.sub("[REDACTED_URL]", message)
    message = _PATH_RE.sub("[REDACTED_PATH]", message)
    message = _CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    return message[:_MAX_STRING]


def _parse_audit(error: str | None) -> dict[str, Any]:
    if not error:
        return {}
    try:
        value = json.loads(error)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def failure_projection(
    *,
    error: str | None,
    failure_code: str | None,
    retryable: bool = False,
    policy_blocked: bool = False,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build the stable public representation of a failed execution.

    ``error`` can be a legacy plain string or the provider worker's structured
    audit JSON.  The returned shape intentionally does not include raw payload
    data or arbitrary provider response fields.
    """

    audit = _parse_audit(error)
    provider_failure = payload.get("provider_failure") if isinstance(payload, Mapping) else None
    if not audit and isinstance(provider_failure, Mapping):
        audit = dict(provider_failure)
    code = str(audit.get("code") or failure_code or "job_failed")[:120]
    message = safe_failure_message(audit.get("message") or error or code, fallback=code)
    # A raw structured audit is already represented by ``message`` above, but
    # expose a useful fallback for older rows that only persisted ``error``.
    if message.startswith("{") and audit:
        message = code
    if not error and not failure_code and not provider_failure:
        return None
    occurred_at = audit.get("occurred_at")
    if occurred_at is not None:
        occurred_at = str(occurred_at)[:80]
    details = safe_failure_details(audit.get("details"))
    return {
        "schema_version": "job-failure/1.0",
        "code": code,
        "message": message,
        "retryable": bool(audit.get("retryable", retryable)),
        "policy_blocked": bool(policy_blocked),
        "provider": str(audit["provider"])[:120] if audit.get("provider") else None,
        "capability": str(audit["capability"])[:120] if audit.get("capability") else None,
        "details": details,
        "occurred_at": occurred_at,
    }


def job_failure(job: Any) -> dict[str, Any] | None:
    return failure_projection(
        error=getattr(job, "error", None),
        failure_code=getattr(job, "failure_code", None),
        retryable=bool(getattr(job, "retryable", False)),
        policy_blocked=bool(getattr(job, "policy_blocked", False)),
        payload=getattr(job, "payload", None),
    )


def workflow_failure(run: Any) -> dict[str, Any] | None:
    return failure_projection(
        error=getattr(run, "error", None),
        failure_code=getattr(run, "failure_code", None),
        payload=getattr(run, "result_json", None),
    )


def public_error(obj: Any) -> str | None:
    failure = job_failure(obj) if hasattr(obj, "payload") else workflow_failure(obj)
    if failure:
        return str(failure["message"])
    value = getattr(obj, "error", None)
    return safe_failure_message(value) if value else None


def _safe_payload_value(value: Any, *, private_context: bool = False) -> Any:
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if _payload_secret_key(normalized):
                continue
            if private_context and any(part in normalized for part in _PAYLOAD_PRIVATE_PARTS):
                continue
            child_private_context = private_context or normalized in _PAYLOAD_PRIVATE_ENVELOPES
            clean[str(key)] = _safe_payload_value(item, private_context=child_private_context)
        return clean
    if isinstance(value, (list, tuple)):
        return [_safe_payload_value(item, private_context=private_context) for item in list(value)[:_MAX_LIST_ITEMS]]
    if isinstance(value, str):
        return value[:_MAX_STRING]
    return value


def public_payload(job: Any) -> dict[str, Any]:
    """Return a bounded Job payload suitable for a tenant-facing response."""

    raw = getattr(job, "payload", None)
    clean = _safe_payload_value(raw if isinstance(raw, Mapping) else {})
    audit = raw.get("provider_failure") if isinstance(raw, Mapping) else None
    if isinstance(audit, Mapping):
        # Replace the internal audit envelope instead of recursively exposing
        # provider messages and detail keys that are not part of the API.
        projected = failure_projection(
            error=json.dumps(audit, ensure_ascii=False),
            failure_code=getattr(job, "failure_code", None),
            retryable=bool(getattr(job, "retryable", False)),
            policy_blocked=bool(getattr(job, "policy_blocked", False)),
            payload={"provider_failure": audit},
        )
        if projected is not None:
            clean["provider_failure"] = projected
    return clean
