"""Metadata-only storage gateway adapter. Media bytes never pass this process."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..config import Settings, get_settings
from .remote_uri import RemoteURIError, validate_presigned_upload_url, validate_remote_uri


MAX_GATEWAY_RESPONSE_BYTES = 1024 * 1024
GatewayTransport = Callable[[Request, float], tuple[int, bytes]]
_RECOVERABLE_OBJECT_ERRORS = {
    "package_not_found": (404, "尚未找到原打包请求"),
    "package_identity_conflict": (409, "原打包身份与文件集合不符"),
    "package_quota_exceeded": (429, "打包服务操作额度已用完"),
    "package_disabled": (503, "打包服务尚未启用"),
    "package_source_unmanaged": (422, "成片归档不支持当前打包服务"),
    "package_manifest_invalid": (422, "文件集合与交付清单不符"),
    "package_text_invalid": (422, "字幕或清单校验失败"),
    "package_filename_duplicate": (422, "包内文件名称重复"),
    "package_size_limit": (413, "所选文件超过打包上限"),
    "object_not_uploaded": (409, "素材尚未上传，请重新选择原文件"),
    "upload_expired": (410, "上传授权已过期，请刷新授权后继续"),
    "object_hash_mismatch": (422, "文件指纹与上传记录不一致，不能替换原素材"),
    "object_size_mismatch": (422, "文件大小与上传记录不一致"),
}


class StorageGatewayError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def gateway_transport(request: Request, timeout: float) -> tuple[int, bytes]:
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_GATEWAY_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        return exc.code, exc.read(MAX_GATEWAY_RESPONSE_BYTES + 1)
    except (OSError, URLError, TimeoutError) as exc:
        raise StorageGatewayError("storage_gateway_unreachable", "Storage service did not return a confirmed result", status_code=503) from exc


def _expiry(value: object, *, max_seconds: int) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError) as exc:
        raise StorageGatewayError("storage_gateway_expiry_invalid", "Storage service returned an invalid expiry") from exc
    seconds = (result - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0 or seconds > max_seconds + 5:
        raise StorageGatewayError("storage_gateway_expiry_invalid", "Storage service returned an expired or excessive grant")
    return result


def _string_fields(value: object, *, headers: bool = False) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 50:
        raise StorageGatewayError("storage_gateway_fields_invalid", "Storage upload fields are invalid")
    for key, item in value.items():
        if (not isinstance(key, str) or not key or len(key) > 200 or not isinstance(item, str)
                or len(item) > 16384 or any(char in key for char in "\r\n\x00")
                or any(char in item for char in "\r\n\x00")):
            raise StorageGatewayError("storage_gateway_fields_invalid", "Storage upload fields are invalid")
        if headers and key.lower() in {"host", "cookie", "connection", "transfer-encoding"}:
            raise StorageGatewayError("storage_gateway_fields_invalid", "Storage service returned a forbidden upload header")
    return value


@dataclass(frozen=True)
class UploadGrant:
    asset_id: str
    object_uri: str
    upload_url: str
    method: str
    headers: dict[str, str]
    fields: dict[str, str]
    expires_at: datetime
    file_field: str = "file"


class StorageGateway:
    def __init__(self, settings: Settings, *, transport: GatewayTransport | None = None):
        if not settings.storage_gateway_url or not settings.storage_gateway_api_key:
            raise StorageGatewayError("storage_gateway_unconfigured", "Storage service is not configured", status_code=503)
        try:
            endpoint = validate_remote_uri(settings.storage_gateway_url, field="storage_gateway_url")
            parsed = urlparse(endpoint)
            if parsed.scheme != "https" or parsed.query or parsed.fragment:
                raise ValueError("HTTPS endpoint without query required")
        except (RemoteURIError, ValueError) as exc:
            raise StorageGatewayError("storage_gateway_configuration_invalid", "Storage service configuration is invalid", status_code=503) from exc
        if not 1 <= settings.storage_gateway_timeout_seconds <= 120:
            raise StorageGatewayError("storage_gateway_configuration_invalid", "Storage service timeout is invalid", status_code=503)
        if any(char in settings.storage_gateway_api_key for char in "\r\n\x00"):
            raise StorageGatewayError("storage_gateway_configuration_invalid", "Storage service credential configuration is invalid", status_code=503)
        if settings.app_environment == "production" and (not settings.storage_upload_hosts or not settings.storage_public_hosts):
            raise StorageGatewayError("storage_gateway_configuration_invalid", "Storage upload and playback domains must be configured", status_code=503)
        self.settings = settings
        self.endpoint = endpoint.rstrip("/")
        self.transport = transport or gateway_transport

    def _post(self, path: str, payload: dict, *, key: str) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        request = Request(self.endpoint + path, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {self.settings.storage_gateway_api_key}", "Idempotency-Key": key,
        })
        status, raw = self.transport(request, float(self.settings.storage_gateway_timeout_seconds))
        if len(raw) > MAX_GATEWAY_RESPONSE_BYTES:
            raise StorageGatewayError("storage_gateway_response_invalid", "Storage service response exceeds its contract")
        if status not in {200, 201}:
            try:
                code = json.loads(raw).get("code")
            except (ValueError, AttributeError, TypeError):
                code = None
            if isinstance(code, str) and code in _RECOVERABLE_OBJECT_ERRORS and status == _RECOVERABLE_OBJECT_ERRORS[code][0]:
                safe_status, safe_message = _RECOVERABLE_OBJECT_ERRORS[code]
                raise StorageGatewayError(code, safe_message, status_code=safe_status)
            if path.startswith('/v1/packages') and status<500:
                raise StorageGatewayError('package_gateway_rejected','打包服务尚未接收本次请求，请核对服务配置或文件范围',status_code=status)
            # Never echo upstream bodies or URLs, which may contain secrets.
            raise StorageGatewayError("storage_gateway_request_failed", "Storage service could not complete this operation", status_code=503 if status >= 500 else 502)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise StorageGatewayError("storage_gateway_response_invalid", "Storage service returned invalid metadata") from exc
        if not isinstance(value, dict):
            raise StorageGatewayError("storage_gateway_response_invalid", "Storage service returned invalid metadata")
        return value

    def upload_grant(self, *, asset_id: str, tenant_id: str, project_id: str | None, specification: dict) -> UploadGrant:
        value = self._post("/v1/uploads", {
            **specification, "asset_id": asset_id, "tenant_id": tenant_id, "project_id": project_id,
            "ttl_seconds": self.settings.asset_upload_session_ttl_seconds,
        }, key=f"upload:{asset_id}")
        if value.get("asset_id") != asset_id:
            raise StorageGatewayError("storage_gateway_identity_mismatch", "Storage service returned a different asset identity")
        try:
            uri = validate_remote_uri(value.get("object_uri"), field="object_uri")
            url = validate_presigned_upload_url(value.get("upload_url"))
            if urlparse(uri).query or urlparse(uri).fragment or urlparse(url).scheme != "https":
                raise ValueError("stable object and HTTPS upload required")
        except (RemoteURIError, TypeError, ValueError) as exc:
            raise StorageGatewayError("storage_gateway_response_invalid", "Storage service returned an invalid upload destination") from exc
        host = (urlparse(url).hostname or "").lower()
        if self.settings.storage_upload_hosts and host not in self.settings.storage_upload_hosts:
            raise StorageGatewayError("storage_upload_domain_forbidden", "Storage upload domain is not configured")
        method = value.get("upload_method", "PUT")
        if method not in {"PUT", "POST"}:
            raise StorageGatewayError("storage_gateway_response_invalid", "Storage service returned an unsupported upload method")
        fields = _string_fields(value.get("upload_fields", {}))
        file_field = value.get("upload_file_field", "file")
        if not isinstance(file_field, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", file_field):
            raise StorageGatewayError("storage_gateway_fields_invalid", "Storage upload file field is invalid")
        if file_field in fields or (method == "PUT" and fields):
            raise StorageGatewayError("storage_gateway_fields_invalid", "Storage upload fields conflict with the file transfer")
        return UploadGrant(asset_id, uri, url, method,
                           _string_fields(value.get("upload_headers", {}), headers=True),
                           fields, _expiry(value.get("expires_at"), max_seconds=self.settings.asset_upload_session_ttl_seconds),
                           file_field)

    def verify_upload(self, *, asset_id: str, tenant_id: str, object_uri: str) -> dict:
        value = self._post(f"/v1/uploads/{quote(asset_id, safe='')}/verify", {
            "asset_id": asset_id, "tenant_id": tenant_id, "object_uri": object_uri,
        }, key=f"verify:{asset_id}")
        if value.get("asset_id") != asset_id or value.get("object_uri") != object_uri:
            raise StorageGatewayError("storage_gateway_identity_mismatch", "Storage verification does not match the upload")
        return value

    def artifact_access(self, *, artifact_id: str, tenant_id: str, object_uri: str, purpose: str, ttl_seconds: int = 300) -> dict:
        ttl_seconds = max(1, min(300, ttl_seconds))
        value = self._post("/v1/artifacts/access", {
            "artifact_id": artifact_id, "tenant_id": tenant_id, "object_uri": object_uri,
            "purpose": purpose, "ttl_seconds": ttl_seconds,
        }, key=f"access:{artifact_id}:{purpose}:{ttl_seconds}:{int(datetime.now(timezone.utc).timestamp()) // 60}")
        if value.get("artifact_id") != artifact_id or value.get("object_uri") != object_uri:
            raise StorageGatewayError("storage_gateway_identity_mismatch", "Storage access does not match the requested artifact")
        try:
            url = validate_presigned_upload_url(value.get("url"))
            if urlparse(url).scheme != "https":
                raise ValueError("HTTPS access required")
        except (RemoteURIError, TypeError, ValueError) as exc:
            raise StorageGatewayError("storage_gateway_response_invalid", "Storage service returned an invalid access URL") from exc
        host = (urlparse(url).hostname or "").lower()
        if not self.settings.storage_public_hosts or host not in self.settings.storage_public_hosts:
            raise StorageGatewayError("storage_public_domain_forbidden", "Playback and download domain is not configured")
        return {"artifact_id": artifact_id, "url": url, "purpose": purpose,
                "expires_at": _expiry(value.get("expires_at"), max_seconds=ttl_seconds)}

    def archive_artifact(self, *, artifact_id: str, tenant_id: str, source_uri: str, expected_sha256: str | None) -> dict:
        value = self._post("/v1/artifacts/archive", {
            "artifact_id": artifact_id, "tenant_id": tenant_id, "source_uri": source_uri,
            "expected_sha256": expected_sha256,
        }, key=f"archive:{artifact_id}")
        if value.get("artifact_id") != artifact_id:
            raise StorageGatewayError("storage_gateway_identity_mismatch", "Archive belongs to a different artifact")
        try:
            uri = validate_remote_uri(value.get("object_uri"), field="object_uri")
            if urlparse(uri).query or urlparse(uri).fragment:
                raise ValueError("Archive must use a stable object reference")
        except (RemoteURIError, ValueError) as exc:
            raise StorageGatewayError("storage_archive_invalid", "Archive does not have a stable object reference") from exc
        return value

    def delivery_package(self, *, package_id: str, tenant_id: str, manifest_hash: str, specification: dict | None = None, cancel: bool = False) -> dict:
        body={'package_id':package_id,'tenant_id':tenant_id,'manifest_hash':manifest_hash}
        if specification is not None:body.update(specification)
        path='/v1/packages/cancel' if cancel else '/v1/packages/build' if specification is not None else '/v1/packages/status'
        value=self._post(path,body,key='package:'+package_id+('' if specification is not None else ':cancel' if cancel else ':status'))
        if value.get('package_id')!=package_id or value.get('manifest_hash')!=manifest_hash or value.get('status') not in {'queued','running','succeeded','failed'}:
            raise StorageGatewayError('package_gateway_identity_invalid','打包响应与原请求不符')
        if value['status']=='succeeded':
            expected=f'/tenants/{tenant_id}/packages/{package_id}/bundle.zip'
            try:
                uri=validate_remote_uri(value.get('object_uri'),field='package_uri');parsed=urlparse(uri)
                if parsed.scheme!='oss' or parsed.path!=expected or parsed.query or parsed.fragment:raise ValueError('stable package namespace required')
                if not re.fullmatch(r'[0-9a-f]{64}',value.get('sha256','')) or not isinstance(value.get('size_bytes'),int) or isinstance(value['size_bytes'],bool) or not 1<=value['size_bytes']<=self.settings.delivery_package_max_bytes+1024*1024:raise ValueError('invalid package bytes')
            except (RemoteURIError,ValueError,TypeError) as error:raise StorageGatewayError('package_gateway_output_invalid','打包输出校验信息无效') from error
        return value


def configured_storage_gateway() -> StorageGateway:
    return StorageGateway(get_settings())
