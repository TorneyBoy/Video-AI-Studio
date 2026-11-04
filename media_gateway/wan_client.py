"""Bounded DashScope Wan HTTP calls; no local inference or automatic POST retry."""
from __future__ import annotations

import json
import os
import re
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


JSON_LIMIT = 4 * 1024 * 1024
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
GENERATION_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
REPLACEMENT_PATH = "/api/v1/services/aigc/image2video/video-synthesis"
TRANSPORT = Callable[[Request, float, int], tuple[int, bytes]]


class WanSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, allow_inf_nan=False)
    enabled: bool = False
    api_key: SecretStr | None = None
    base_url: str = "https://dashscope.aliyuncs.com"
    model_version: str = Field(default="wan2.2", min_length=1, max_length=128)
    max_operations: int = Field(default=0, ge=0, le=10000)
    max_output_bytes: int = Field(default=100 * 1024 * 1024, ge=1, le=200 * 1024 * 1024)
    timeout_seconds: float = Field(default=60, gt=0, le=120)
    result_hosts: tuple[str, ...] = (
        "dashscope-result-bj.oss-cn-beijing.aliyuncs.com",
        "dashscope-result-sh.oss-cn-shanghai.aliyuncs.com",
        "dashscope-result-sg.oss-ap-southeast-1.aliyuncs.com",
    )

    @field_validator("base_url")
    @classmethod
    def official_endpoint(cls, value):
        parsed = urlparse(value)
        host = parsed.hostname or ""
        workspace = re.fullmatch(r"[a-zA-Z0-9-]+\.(cn-beijing|ap-southeast-1|us-east-1|eu-central-1)\.maas\.aliyuncs\.com", host)
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or parsed.port not in {None, 443}
                or (host not in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} and not workspace)):
            raise ValueError("Wan endpoint must be an official HTTPS DashScope origin")
        return value.rstrip("/")

    @field_validator("result_hosts")
    @classmethod
    def exact_result_hosts(cls, hosts):
        for host in hosts:
            if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.aliyuncs\.com", host):
                raise ValueError("Wan result hosts must be explicit Alibaba Cloud DNS names")
        return tuple(sorted(set(hosts)))

    @model_validator(mode="after")
    def enabled_credentials(self):
        if self.enabled and not (self.api_key and self.api_key.get_secret_value().strip()):
            raise ValueError("DASHSCOPE_API_KEY is required when WAN_ENABLED=true")
        return self

    @classmethod
    def from_env(cls):
        values = dict(enabled=os.getenv("WAN_ENABLED", "false").lower() == "true",
            api_key=os.getenv("DASHSCOPE_API_KEY") or None,
            base_url=os.getenv("WAN_BASE_URL", "https://dashscope.aliyuncs.com"),
            model_version=os.getenv("WAN_MODEL_VERSION", "wan2.2"),
            max_operations=int(os.getenv("WAN_MAX_OPERATIONS", "0")),
            max_output_bytes=int(os.getenv("WAN_MAX_OUTPUT_BYTES", str(100 * 1024 * 1024))))
        if os.getenv("WAN_RESULT_HOSTS"):
            values["result_hosts"] = tuple(value.strip() for value in os.environ["WAN_RESULT_HOSTS"].split(",") if value.strip())
        return cls(**values)


class WanAPIError(ValueError):
    def __init__(self, code: str, *, retryable=False, submission_rejected=False):
        super().__init__(code)
        self.code, self.retryable, self.submission_rejected = code, retryable, submission_rejected


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(request: Request, timeout: float, limit: int) -> tuple[int, bytes]:
    opener = build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(limit + 1)
    except HTTPError as exc:
        try:
            return exc.code, exc.read(limit + 1)
        finally:
            exc.close()


class WanClient:
    def __init__(self, settings: WanSettings, *, transport: TRANSPORT | None = None):
        self.settings, self.transport = settings, transport or _transport

    def _call(self, path: str, *, body: dict | None = None) -> dict:
        headers = {"Accept": "application/json", "Authorization": "Bearer " + self.settings.api_key.get_secret_value()}
        data = None
        if body is not None:
            headers.update({"Content-Type": "application/json", "X-DashScope-Async": "enable"})
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        request = Request(self.settings.base_url + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            status, raw = self.transport(request, self.settings.timeout_seconds, JSON_LIMIT)
        except Exception:
            raise WanAPIError("wan_request_unconfirmed", retryable=True) from None
        if not 200 <= status < 300:
            rejected = body is not None and status in {400, 401, 403, 404, 422, 429}
            raise WanAPIError("wan_request_rejected" if rejected else "wan_request_unconfirmed",
                              retryable=not rejected, submission_rejected=rejected)
        try:
            if len(raw) > JSON_LIMIT:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise WanAPIError("wan_response_unconfirmed", retryable=True) from None
        return result

    def submit(self, *, model_id: str, inputs: dict, parameters: dict) -> dict:
        path = REPLACEMENT_PATH if model_id == "wan2.2-animate-mix" else GENERATION_PATH
        value = self._call(path, body={"model": model_id, "input": inputs, "parameters": parameters})
        output = value.get("output") or {}
        task_id = output.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise WanAPIError("wan_submission_identity_unknown", retryable=True)
        return {"task_id": task_id, "request_id": value.get("request_id")}

    def task_status(self, task_id: str) -> dict:
        if not TASK_ID.fullmatch(task_id):
            raise WanAPIError("wan_task_identity_invalid")
        value = self._call("/api/v1/tasks/" + task_id)
        output = value.get("output") or {}
        if output.get("task_id") != task_id:
            raise WanAPIError("wan_task_identity_mismatch", retryable=True)
        status = output.get("task_status")
        states = {"PENDING": "running", "RUNNING": "running", "SUCCEEDED": "succeeded", "FAILED": "failed",
                  "CANCELED": "failed", "CANCELLED": "failed", "UNKNOWN": "unknown"}
        if status not in states:
            raise WanAPIError("wan_task_status_unknown", retryable=True)
        return {"status": states[status], "task_id": task_id, "request_id": value.get("request_id"),
                "video_url": output.get("video_url") or (output.get("results") or {}).get("video_url")}

    def download_video(self, url: str) -> bytes:
        parsed = urlparse(url)
        if (parsed.scheme not in {"https", "http"} or parsed.hostname not in self.settings.result_hosts
                or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment):
            raise WanAPIError("wan_result_url_forbidden")
        # Official examples include HTTP OSS links; retain the exact signed
        # path/query but use TLS for the same explicitly allowed hostname.
        secure = urlunparse(parsed._replace(scheme="https"))
        request = Request(secure, headers={"Accept": "video/mp4", "Accept-Encoding": "identity"}, method="GET")
        try:
            status, data = self.transport(request, self.settings.timeout_seconds, self.settings.max_output_bytes)
        except Exception:
            raise WanAPIError("wan_result_download_unconfirmed", retryable=True) from None
        if status != 200:
            raise WanAPIError("wan_result_download_unconfirmed", retryable=True)
        if not data or len(data) > self.settings.max_output_bytes:
            raise WanAPIError("wan_result_size_invalid")
        return data
