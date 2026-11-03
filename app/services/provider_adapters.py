"""Production-facing Provider Gateway adapters.

The control plane keeps provider-specific SDKs out of the worker.  These
small adapters make the boundary executable without choosing a vendor. The
production adapter is an HTTP service; the trusted local command adapter is
kept only for explicit development and contract tests. Both implement the
same ``ProviderResult`` contract, including timeout and retry classification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from .providers import (
    ModelSnapshot,
    ProviderCapability,
    ProviderExecutionError,
    ProviderExecutionPending,
    ProviderCallbackEvent,
    ProviderHealth,
    ProviderRequest,
    ProviderResult,
)
from .remote_uri import RemoteURIError, validate_remote_uri


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
HttpTransport = Callable[[Request, float], tuple[int, bytes]]
CommandBuilder = Callable[[ProviderRequest, Path], list[str]]
_SENSITIVE_KEYS = {"authorization", "api_key", "apikey", "token", "secret", "password", "credential"}


def _redact(value: Any) -> Any:
    """Keep model metadata auditable without persisting provider credentials."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if any(part in str(key).lower() for part in _SENSITIVE_KEYS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _default_http_transport(request: Request, timeout: float) -> tuple[int, bytes]:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - endpoint is operator configured
        return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)


def _retryable_status(status: int) -> bool:
    return status in {408, 409, 425, 429} or status >= 500


def _json_body(payload: bytes, *, provider: str, capability: str) -> dict[str, Any]:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProviderExecutionError(
            "provider response exceeded the configured size limit",
            code="provider_response_too_large",
            provider=provider,
            capability=capability,
            retryable=False,
        )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderExecutionError(
            "provider returned invalid JSON",
            code="provider_invalid_response",
            provider=provider,
            capability=capability,
            retryable=False,
        ) from exc
    if not isinstance(value, dict):
        raise ProviderExecutionError(
            "provider response must be a JSON object",
            code="provider_invalid_response",
            provider=provider,
            capability=capability,
            retryable=False,
        )
    return value


def _validate_remote_input_uri(value: Any, *, field: str, provider: str, capability: str) -> Any:
    """Reject local paths before a hosted provider request is sent.

    Production workers resolve tenant assets to remotely reachable object
    URIs.  Keeping this check in the adapter as well protects callers that
    construct ``ProviderRequest`` directly and prevents a local path or
    ``file://`` URI from crossing the hosted API boundary by accident.
    """

    values = value if isinstance(value, (list, tuple)) else [value]
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ProviderExecutionError(
                f"provider input {field} must be a remote object URL",
                code="provider_invalid_input_uri",
                provider=provider,
                capability=capability,
                retryable=False,
            )
        try:
            validate_remote_uri(item, field=field)
        except RemoteURIError as exc:
            raise ProviderExecutionError(
                str(exc),
                code="provider_invalid_input_uri",
                provider=provider,
                capability=capability,
                retryable=False,
            ) from exc
    return value


class HTTPProvider:
    """Adapter for hosted JSON APIs with bounded polling and webhook support.

    A provider may answer the submit request with a finished artifact or with
    a remote job ID. The adapter polls for a bounded window; if the provider
    is still processing it raises ``ProviderExecutionPending`` with the remote
    ID. A durable retry resumes polling instead of submitting duplicate work.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        capabilities: tuple[ProviderCapability, ...],
        path: str = "/v1/generate",
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 120.0,
        health_path: str = "/health",
        model: ModelSnapshot | None = None,
        transport: HttpTransport | None = None,
        poll_path: str | None = None,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float | None = None,
        max_poll_attempts: int = 30,
        callback_secret: str | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("provider base_url must be an absolute http(s) URL")
        if timeout_seconds <= 0:
            raise ValueError("provider timeout_seconds must be greater than zero")
        if poll_interval_seconds < 0:
            raise ValueError("provider poll_interval_seconds must not be negative")
        if max_poll_attempts < 1:
            raise ValueError("provider max_poll_attempts must be at least one")
        self.name = name
        self.base_url = base_url.rstrip("/") + "/"
        self.capabilities = capabilities
        self.path = path
        self.headers = dict(headers or {})
        self.timeout_seconds = timeout_seconds
        self.health_path = health_path
        self.model = model
        self.poll_path = poll_path
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds if poll_timeout_seconds is not None else timeout_seconds
        if self.poll_timeout_seconds <= 0:
            raise ValueError("provider poll_timeout_seconds must be greater than zero")
        self.max_poll_attempts = max_poll_attempts
        self.callback_secret = callback_secret
        self.enabled = True
        self.available = True
        self._transport = transport or _default_http_transport

    def _request_body(self, request: ProviderRequest) -> bytes:
        # A remote service cannot read a worker's local filesystem. Exclude
        # resolved paths and output destinations, while retaining stable asset
        # IDs and remote object URIs supplied by the caller.
        remote_inputs = {
            key: value
            for key, value in request.inputs.items()
            if key not in {"input_path", "audio_input_path", "output_path", "output_dir"}
        }
        for key, value in remote_inputs.items():
            # URI-bearing input names are part of the provider contract.  A
            # list is used by rendering and face/motion workflows.
            if "uri" in str(key).lower():
                _validate_remote_input_uri(
                    value,
                    field=str(key),
                    provider=self.name,
                    capability=request.capability,
                )
        body = {
            "request_id": request.request_id,
            "capability": request.capability,
            "model_id": request.model_id,
            "model_version": request.model_version,
            "commercial_use": request.commercial_use,
            "inputs": remote_inputs,
            "parameters": {
                k: v for k, v in request.parameters.items()
                if k not in {"output_dir", "output_path"}
            },
            "tenant_id": request.tenant_id,
            "consent_ids": list(request.consent_ids),
            "region": request.region,
        }
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _send(self, request_obj: Request, timeout: float) -> tuple[int, bytes]:
        try:
            return self._transport(request_obj, timeout)
        except HTTPError as exc:
            return int(exc.code), exc.read(MAX_RESPONSE_BYTES + 1)
        except (TimeoutError, URLError, OSError) as exc:
            raise ProviderExecutionError(
                "provider request could not be completed",
                code="provider_transport_error",
                provider=self.name,
                retryable=True,
                details={"error_type": exc.__class__.__name__},
            ) from exc

    def _status_error(self, status: int, capability: str) -> ProviderExecutionError:
        return ProviderExecutionError(
            f"provider returned HTTP {status}",
            code="provider_http_error",
            provider=self.name,
            capability=capability,
            retryable=_retryable_status(status),
            details={"http_status": status},
        )

    def _result_from_response(
        self,
        response: dict[str, Any],
        request: ProviderRequest,
        *,
        provider_job_id: str | None = None,
    ) -> ProviderResult | None:
        artifact_uri = response.get("artifact_uri") or response.get("output_uri")
        state = str(response.get("status") or "").lower()
        if artifact_uri:
            if not isinstance(artifact_uri, str):
                raise ProviderExecutionError(
                    "provider artifact_uri must be a string",
                    code="provider_invalid_response",
                    provider=self.name,
                    capability=request.capability,
                    retryable=False,
                )
            try:
                validate_remote_uri(artifact_uri, field="artifact_uri")
            except RemoteURIError as exc:
                raise ProviderExecutionError(
                    str(exc),
                    code="provider_invalid_artifact_uri",
                    provider=self.name,
                    capability=request.capability,
                    retryable=False,
                ) from exc
            model_data = response.get("model") if isinstance(response.get("model"), dict) else {}
            configured = self.model
            model = ModelSnapshot(
                provider=str(model_data.get("provider") or (configured.provider if configured else self.name)),
                model_id=str(model_data.get("model_id") or (configured.model_id if configured else "unknown")),
                version=str(model_data.get("version") or (configured.version if configured else "unknown")),
                weights_sha256=model_data.get("weights_sha256") or (configured.weights_sha256 if configured else None),
                runtime_digest=model_data.get("runtime_digest") or (configured.runtime_digest if configured else None),
                license_snapshot=_redact(model_data.get("license_snapshot") or (configured.license_snapshot if configured else {})),
            )
            provenance = {"provider_request_id": response.get("request_id") or request.request_id}
            if provider_job_id:
                provenance["provider_job_id"] = provider_job_id
            warnings = response.get("warnings") or []
            metrics = response.get("metrics") if isinstance(response.get("metrics"), dict) else {}
            # Hosted analysis APIs often return useful structured data inline
            # alongside an artifact URI. Preserve it under the typed metrics
            # envelope so the worker can persist it without vendor SDKs.
            if isinstance(response.get("analysis"), dict):
                metrics = {**metrics, "analysis": response["analysis"]}
            if isinstance(response.get("result"), dict):
                metrics = {**metrics, "result": response["result"]}
            return ProviderResult(
                artifact_uri=artifact_uri,
                artifact_type=str(response.get("artifact_type") or "provider.artifact"),
                model=model,
                metrics=metrics,
                provenance=provenance,
                status=state or "succeeded",
                warnings=tuple(str(item) for item in warnings),
                usage=response.get("usage") if isinstance(response.get("usage"), dict) else {},
                cost=response.get("cost") if isinstance(response.get("cost"), dict) else {},
            )
        if state in {"failed", "error", "cancelled", "canceled"}:
            error_data = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise ProviderExecutionError(
                str(error_data.get("message") or response.get("message") or "provider job failed"),
                code=str(error_data.get("code") or "provider_job_failed"),
                provider=self.name,
                capability=request.capability,
                retryable=bool(response.get("retryable", False)),
                details={"provider_job_id": provider_job_id, "provider_status": state},
            )
        return None

    @staticmethod
    def _provider_job_id(response: dict[str, Any]) -> str | None:
        for key in ("provider_job_id", "job_id", "task_id", "id"):
            value = response.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
        return None

    def _poll(self, request: ProviderRequest, provider_job_id: str) -> ProviderResult:
        if not self.poll_path:
            raise ProviderExecutionPending(
                provider=self.name,
                capability=request.capability,
                provider_job_id=provider_job_id,
                poll_after_seconds=self.poll_interval_seconds,
                details={"provider_status": "queued", "poll_supported": False},
            )
        deadline = time.monotonic() + self.poll_timeout_seconds
        for attempt in range(1, self.max_poll_attempts + 1):
            path = self.poll_path.replace("{provider_job_id}", quote(provider_job_id, safe=""))
            endpoint = urljoin(self.base_url, path.lstrip("/"))
            poll_request = Request(
                endpoint,
                method="GET",
                headers={**self.headers, "X-Request-ID": request.request_id},
            )
            status, raw = self._send(
                poll_request,
                min(self.timeout_seconds, max(0.1, self.poll_timeout_seconds)),
            )
            if status < 200 or status >= 300:
                raise self._status_error(status, request.capability)
            response = _json_body(raw, provider=self.name, capability=request.capability)
            result = self._result_from_response(response, request, provider_job_id=provider_job_id)
            if result is not None:
                return result
            provider_status = str(response.get("status") or "processing").lower()
            if attempt >= self.max_poll_attempts or time.monotonic() >= deadline:
                raise ProviderExecutionPending(
                    provider=self.name,
                    capability=request.capability,
                    provider_job_id=provider_job_id,
                    poll_after_seconds=self.poll_interval_seconds,
                    details={"provider_status": provider_status, "poll_attempts": attempt},
                )
            if self.poll_interval_seconds:
                time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))
        raise ProviderExecutionPending(provider=self.name, capability=request.capability, provider_job_id=provider_job_id)

    def execute(self, request: ProviderRequest) -> ProviderResult:
        provider_job_id = request.parameters.get("provider_job_id")
        if provider_job_id:
            return self._poll(request, str(provider_job_id))
        endpoint = urljoin(self.base_url, self.path.lstrip("/"))
        request_obj = Request(
            endpoint,
            data=self._request_body(request),
            method="POST",
            # Keep the request identity at both the JSON and HTTP boundary.
            # Hosted APIs commonly implement de-duplication from
            # ``Idempotency-Key`` while gateways and observability systems
            # use ``X-Request-ID``.  Both must remain stable when a durable
            # worker retries a request after a transport timeout.
            headers={
                **self.headers,
                "Content-Type": "application/json",
                "Idempotency-Key": request.request_id,
                "X-Request-ID": request.request_id,
            },
        )
        status, raw = self._send(request_obj, self.timeout_seconds)
        if status < 200 or status >= 300:
            raise self._status_error(status, request.capability)
        response = _json_body(raw, provider=self.name, capability=request.capability)
        result = self._result_from_response(response, request)
        if result is not None:
            return result
        provider_job_id = self._provider_job_id(response)
        if not provider_job_id:
            raise ProviderExecutionError(
                "provider response did not contain artifact_uri or a remote job ID",
                code="provider_invalid_response",
                provider=self.name,
                capability=request.capability,
                retryable=False,
            )
        return self._poll(request, provider_job_id)

    def verify_callback_signature(self, payload: bytes, signature: str | None) -> bool:
        """Verify an HMAC-SHA256 callback when a secret is configured."""
        if not self.callback_secret:
            return True
        if not signature:
            return False
        supplied = signature.strip()
        if supplied.startswith("sha256="):
            supplied = supplied.split("=", 1)[1]
        expected = hmac.new(self.callback_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(supplied, expected)

    def parse_callback(self, payload: bytes | dict[str, Any], *, signature: str | None = None) -> ProviderCallbackEvent:
        """Validate and normalize a vendor callback without creating artifacts."""
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if not self.verify_callback_signature(raw, signature):
            raise ProviderExecutionError(
                "provider callback signature is invalid",
                code="provider_callback_unauthorized",
                provider=self.name,
                retryable=False,
            )
        value = _json_body(raw, provider=self.name, capability="provider_callback")
        request_id = value.get("request_id") or value.get("job_id")
        provider_job_id = self._provider_job_id(value) or (
            str(value.get("external_job_id")) if value.get("external_job_id") else None
        )
        status = value.get("status")
        if not isinstance(request_id, str) or not request_id or not provider_job_id or not isinstance(status, str):
            raise ProviderExecutionError(
                "provider callback must include request_id, job ID and status",
                code="provider_callback_invalid",
                provider=self.name,
                retryable=False,
            )
        return ProviderCallbackEvent(
            provider=self.name,
            request_id=request_id,
            provider_job_id=provider_job_id,
            status=status,
            artifact_uri=value.get("artifact_uri") or value.get("output_uri"),
            artifact_type=value.get("artifact_type"),
            usage=value.get("usage") if isinstance(value.get("usage"), dict) else {},
            cost=value.get("cost") if isinstance(value.get("cost"), dict) else {},
            error=value.get("error") if isinstance(value.get("error"), dict) else {},
        )

    def health_check(self, *, probe: bool = False) -> ProviderHealth:
        """Check the configured endpoint when an operator explicitly probes.

        Configuration-only reads are deliberately network-free. This keeps
        ordinary readiness/metrics scrapes from generating third-party
        traffic and avoids leaking provider availability into an unauthenticated
        request timing channel.
        """
        if not probe:
            return ProviderHealth(
                provider=self.name,
                available=True,
                status="configured",
                reason="network_probe_disabled",
                model=self.model,
                probed=False,
            )
        endpoint = urljoin(self.base_url, self.health_path.lstrip("/"))
        request_obj = Request(endpoint, method="GET", headers=dict(self.headers))
        started = time.perf_counter()
        try:
            status, _raw = self._transport(request_obj, min(self.timeout_seconds, 10.0))
        except (TimeoutError, HTTPError, URLError, OSError) as exc:
            self.available = False
            return ProviderHealth(
                provider=self.name,
                available=False,
                status="unavailable",
                reason=exc.__class__.__name__,
                model=self.model,
                probed=True,
                latency_ms=round(min(60_000.0, (time.perf_counter() - started) * 1000.0), 3),
            )
        self.available = 200 <= status < 300
        return ProviderHealth(
            provider=self.name,
            available=self.available,
            status="ready" if self.available else "unavailable",
            reason=None if self.available else f"http_{status}",
            model=self.model,
            probed=True,
            latency_ms=round(min(60_000.0, (time.perf_counter() - started) * 1000.0), 3),
        )


class CommandProvider:
    """Adapter for a trusted local/GPU command without shell evaluation."""

    def __init__(
        self,
        *,
        name: str,
        capabilities: tuple[ProviderCapability, ...],
        command_builder: CommandBuilder,
        model: ModelSnapshot,
        artifact_type: str = "provider.artifact",
        timeout_seconds: float = 2 * 60 * 60,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("provider timeout_seconds must be greater than zero")
        self.name = name
        self.capabilities = capabilities
        self.command_builder = command_builder
        self.model = model
        self.artifact_type = artifact_type
        self.timeout_seconds = timeout_seconds
        self.enabled = True
        self.available = True

    def execute(self, request: ProviderRequest) -> ProviderResult:
        output_value = request.parameters.get("output_path")
        if not output_value:
            raise ProviderExecutionError(
                "command provider requires parameters.output_path",
                code="missing_output_path",
                provider=self.name,
                capability=request.capability,
                retryable=False,
            )
        output_path = Path(str(output_value))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = self.command_builder(request, output_path)
        if not command or any(not isinstance(part, str) for part in command):
            raise ProviderExecutionError(
                "command provider returned an invalid argv",
                code="invalid_command",
                provider=self.name,
                capability=request.capability,
                retryable=False,
            )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            output_path.unlink(missing_ok=True)
            raise ProviderExecutionError(
                "command provider timed out",
                code="provider_timeout",
                provider=self.name,
                capability=request.capability,
                retryable=True,
            ) from exc
        except subprocess.CalledProcessError as exc:
            output_path.unlink(missing_ok=True)
            raise ProviderExecutionError(
                "command provider exited unsuccessfully",
                code="provider_command_failed",
                provider=self.name,
                capability=request.capability,
                retryable=True,
                details={"returncode": exc.returncode},
            ) from exc
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ProviderExecutionError(
                "command provider did not produce an artifact",
                code="provider_missing_output",
                provider=self.name,
                capability=request.capability,
                retryable=False,
            )
        return ProviderResult(
            artifact_uri=str(output_path),
            artifact_type=self.artifact_type,
            model=self.model,
            provenance={"request_id": request.request_id},
        )

    def health_check(self, *, probe: bool = False) -> ProviderHealth:
        """Validate the executable without invoking a potentially expensive model."""
        if not probe:
            return ProviderHealth(
                provider=self.name,
                available=True,
                status="configured",
                reason="execution_probe_disabled",
                model=self.model,
                probed=False,
            )
        try:
            command = self.command_builder(
                ProviderRequest(request_id="health-check", capability=self.capabilities[0].name, inputs={}),
                Path("/tmp/provider-health-check.out"),
            )
            executable = command[0] if command else ""
            available = bool(executable) and (Path(executable).is_file() or shutil.which(executable) is not None)
        except Exception:  # noqa: BLE001 - health must never expose template internals
            available = False
        self.available = available
        return ProviderHealth(
            provider=self.name,
            available=available,
            status="ready" if available else "unavailable",
            reason=None if available else "executable_not_found",
            model=self.model,
            probed=True,
        )
