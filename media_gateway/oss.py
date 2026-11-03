"""OSS V4 form grants and independently observed content integrity.

Only the gateway reads media bytes. The control plane imports neither this
module nor the OSS SDK. Sources: Alibaba OSS Python V2 1.4.0 and PostObject V4.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
from urllib.parse import unquote, urlparse


class ObjectStoreError(ValueError):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.status = code, status


class OSSStore:
    def __init__(self, *, bucket: str, region: str, internal: bool = False,
                 credentials_provider=None, client=None):
        import alibabacloud_oss_v2 as oss

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
            raise ValueError("Invalid OSS bucket")
        if not re.fullmatch(r"[a-z]{2}-[a-z0-9-]+", region):
            raise ValueError("Invalid OSS region")
        self.oss, self.bucket, self.region = oss, bucket, region
        self.host = f"{bucket}.oss-{region}.aliyuncs.com"
        self.credentials_provider = credentials_provider or oss.credentials.EnvironmentVariableCredentialsProvider()
        cfg = oss.config.load_default()
        cfg.region, cfg.credentials_provider = region, self.credentials_provider
        cfg.use_internal_endpoint = internal
        self.client = client or oss.Client(cfg)
        # Signing uses the public endpoint so browser/WeChat can reach it.
        public = oss.config.load_default()
        public.region, public.credentials_provider = region, self.credentials_provider
        self.public_client = oss.Client(public) if client is None else client

    def object_uri(self, key: str) -> str:
        return f"oss://{self.bucket}/{key}"

    def key_from_uri(self, uri: str, tenant_id: str) -> str:
        parsed = urlparse(uri)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ObjectStoreError("object_not_managed", "A stable managed object is required", 422)
        if not ((parsed.scheme == "oss" and parsed.netloc == self.bucket)
                or (parsed.scheme == "https" and parsed.netloc == self.host)):
            raise ObjectStoreError("object_not_managed", "Object does not belong to this store", 422)
        key = unquote(parsed.path.lstrip("/"))
        if not key.startswith(f"tenants/{tenant_id}/") or any(part in {".", ".."} for part in key.split("/")):
            raise ObjectStoreError("object_tenant_mismatch", "Object is outside the tenant namespace", 403)
        return key

    def grant(self, *, key: str, size_bytes: int, content_type: str, expires_at: datetime) -> dict:
        # OSS ignores forbid-overwrite on enabled OR suspended versioning.
        versioning = self.client.get_bucket_versioning(self.oss.GetBucketVersioningRequest(bucket=self.bucket))
        if versioning.version_status not in {None, ""}:
            raise ObjectStoreError("bucket_versioning_incompatible", "Direct uploads require a bucket that has never enabled versioning", 503)
        credential = self.credentials_provider.get_credentials()
        now = datetime.now(timezone.utc)
        if not credential.has_keys() or credential.is_expired():
            raise ObjectStoreError("oss_credential_unavailable", "OSS credentials are unavailable", 503)
        if credential.expiration:
            expires_at = min(expires_at, credential.expiration - timedelta(seconds=30))
        if expires_at <= now:
            raise ObjectStoreError("oss_credential_expiring", "OSS credential lifetime is too short", 503)
        date = now.strftime("%Y%m%d")
        fields = {
            "key": key, "x-oss-signature-version": "OSS4-HMAC-SHA256",
            "x-oss-credential": f"{credential.access_key_id}/{date}/{self.region}/oss/aliyun_v4_request",
            "x-oss-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "x-oss-forbid-overwrite": "true", "x-oss-object-acl": "private",
            "x-oss-content-type": content_type, "success_action_status": "200",
        }
        if credential.security_token:
            fields["x-oss-security-token"] = credential.security_token
        policy = {"expiration": expires_at.isoformat().replace("+00:00", "Z"),
                  "conditions": [{"bucket": self.bucket}, ["content-length-range", size_bytes, size_bytes],
                                 *[{key: value} for key, value in fields.items()]]}
        encoded = base64.b64encode(json.dumps(policy, separators=(",", ":")).encode()).decode()
        signing_key = ("aliyun_v4" + credential.access_key_secret).encode()
        for component in (date, self.region, "oss", "aliyun_v4_request"):
            signing_key = hmac.new(signing_key, component.encode(), hashlib.sha256).digest()
        fields.update({"policy": encoded,
                       "x-oss-signature": hmac.new(signing_key, encoded.encode(), hashlib.sha256).hexdigest()})
        return {"upload_url": f"https://{self.host}/", "upload_method": "POST", "upload_headers": {},
                "upload_fields": fields, "upload_file_field": "file", "expires_at": expires_at.isoformat()}

    def observe(self, *, key: str, expected_size: int, expected_hash: str | None) -> dict:
        try:
            head = self.client.head_object(self.oss.HeadObjectRequest(bucket=self.bucket, key=key))
        except (self.oss.exceptions.ServiceError, self.oss.exceptions.OperationError) as exc:
            cause = exc.unwrap() if isinstance(exc, self.oss.exceptions.OperationError) else exc
            if isinstance(cause, self.oss.exceptions.ServiceError) and cause.status_code == 404:
                raise ObjectStoreError("object_not_uploaded", "素材尚未上传，请重新选择原文件", 409) from None
            raise
        if head.content_length != expected_size:
            raise ObjectStoreError("object_size_mismatch", "Stored object size does not match its intent", 422)
        if not head.etag:
            raise ObjectStoreError("object_identity_missing", "OSS did not return an object identity")
        result = self.client.get_object(self.oss.GetObjectRequest(bucket=self.bucket, key=key, if_match=head.etag))
        digest, total = hashlib.sha256(), 0
        try:
            for chunk in self._stream_chunks(result.body):
                total += len(chunk)
                if total > expected_size:
                    raise ObjectStoreError("object_size_mismatch", "Stored object exceeded its intent", 422)
                digest.update(chunk)
        finally:
            result.body.close()
        if total != expected_size or result.etag != head.etag:
            raise ObjectStoreError("object_changed", "Object changed during verification", 409)
        actual = digest.hexdigest()
        if expected_hash and actual != expected_hash.lower():
            raise ObjectStoreError("object_hash_mismatch", "Stored object hash does not match its intent", 422)
        return {"size_bytes": total, "sha256": actual, "etag": head.etag,
                "metadata": {"verification": "oss-get-sha256-v1", "oss_request_id": result.request_id}}

    def access(self, *, key: str, ttl_seconds: int, purpose: str) -> dict:
        result = self.public_client.presign(self.oss.GetObjectRequest(
            bucket=self.bucket, key=key,
            response_content_disposition="attachment" if purpose == "download" else "inline"),
            expires=timedelta(seconds=ttl_seconds))
        return {"url": result.url, "expires_at": result.expiration.isoformat()}

    def result_grant(self, *, key: str, ttl_seconds: int) -> dict:
        version = self.client.get_bucket_versioning(self.oss.GetBucketVersioningRequest(bucket=self.bucket))
        if version.version_status not in {None, ""}:
            raise ObjectStoreError("bucket_versioning_incompatible", "Cloud analysis output requires immutable objects", 503)
        result = self.public_client.presign(self.oss.PutObjectRequest(bucket=self.bucket, key=key,
            content_type="application/json", acl="private", forbid_overwrite=True), expires=timedelta(seconds=ttl_seconds))
        return {"url": result.url, "headers": dict(result.signed_headers or {}), "expires_at": result.expiration.isoformat()}

    def read_json(self, *, key: str, max_bytes: int) -> dict:
        head = self.client.head_object(self.oss.HeadObjectRequest(bucket=self.bucket, key=key))
        if not head.etag or not head.content_length or head.content_length > max_bytes:
            raise ObjectStoreError("analysis_document_size_invalid", "Cloud analysis output exceeds its metadata limit")
        result = self.client.get_object(self.oss.GetObjectRequest(bucket=self.bucket, key=key, if_match=head.etag))
        try:
            value = json.loads(self._read_bounded(result.body, max_bytes))
        except (UnicodeError, ValueError) as exc:
            raise ObjectStoreError("analysis_document_invalid", "Cloud analysis metadata is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ObjectStoreError("analysis_document_invalid", "Cloud analysis metadata must be an object")
        return value

    @staticmethod
    def _stream_chunks(stream):
        # OSS V2 StreamBodyReader.read() takes no size and reads the whole
        # object. Its documented iterator is the bounded-memory interface.
        iterator = getattr(stream, "iter_bytes", None)
        if callable(iterator):
            yield from iterator(block_size=64 * 1024)
        else:
            # In-memory streams used by adapters/fixtures expose sized reads.
            while chunk := stream.read(64 * 1024):
                yield chunk

    @staticmethod
    def _read_bounded(stream, limit: int) -> bytes:
        chunks, total = [], 0
        try:
            for chunk in OSSStore._stream_chunks(stream):
                total += len(chunk)
                if total > limit:
                    raise ObjectStoreError("object_size_limit", "Object exceeds the configured byte limit", 413)
                chunks.append(chunk)
        finally:
            stream.close()
        return b"".join(chunks)

    def put_verified(self, *, key: str, data: bytes, content_type: str) -> dict:
        version = self.client.get_bucket_versioning(self.oss.GetBucketVersioningRequest(bucket=self.bucket))
        if version.version_status not in {None, ""}:
            raise ObjectStoreError("bucket_versioning_incompatible", "Immutable output requires an unversioned bucket", 503)
        digest = hashlib.sha256(data).hexdigest()
        try:
            self.client.put_object(self.oss.PutObjectRequest(bucket=self.bucket, key=key, body=data,
                content_length=len(data), content_type=content_type, acl="private", forbid_overwrite=True,
                content_md5=base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()))
        except (self.oss.exceptions.ServiceError, self.oss.exceptions.OperationError) as exc:
            cause = exc.unwrap() if isinstance(exc, self.oss.exceptions.OperationError) else exc
            if not isinstance(cause, self.oss.exceptions.ServiceError) or cause.status_code != 409 or cause.code != "FileAlreadyExists":
                raise
        return self.observe(key=key, expected_size=len(data), expected_hash=digest)

    def package(self,*,tenant_id,package_id,manifest,files,max_bytes,temp_root,fence):
        """Stream verified source objects into a deterministic, bounded ZIP."""
        import tempfile
        import time
        from pathlib import Path
        from zipfile import ZipFile,ZipInfo,ZIP_STORED
        root=Path(temp_root).resolve();root.mkdir(parents=True,exist_ok=True)
        state=self.client.get_bucket_versioning(self.oss.GetBucketVersioningRequest(bucket=self.bucket))
        if state.version_status not in {None,''}:raise ObjectStoreError('bucket_versioning_incompatible','Package requires immutable output names',503)
        started=time.monotonic();total=0
        def check():
            if time.monotonic()-started>240:raise ObjectStoreError('package_storage_timeout','Package build exceeded its bounded execution time',503)
            fence()
        def member(name):
            value=ZipInfo(name,date_time=(1980,1,1,0,0,0));value.compress_type=ZIP_STORED;value.create_system=3;value.external_attr=(0o100644<<16);return value
        with tempfile.TemporaryDirectory(prefix='video-ai-package-',dir=root) as directory:
            path=Path(directory)/'bundle.zip'
            with ZipFile(path,'w',compression=ZIP_STORED,allowZip64=True) as archive:
                archive.writestr(member('manifest.json'),manifest.encode('utf-8'))
                for file in files:
                    check()
                    if file['kind']=='text':archive.writestr(member(file['filename']),file['content'].encode('utf-8'));continue
                    key=self.key_from_uri(file['object_uri'],tenant_id)
                    if key!=f'tenants/{tenant_id}/artifacts/{file["artifact_id"]}/output':raise ObjectStoreError('package_source_unmanaged','Source must be an archived tenant artifact',422)
                    head=self.client.head_object(self.oss.HeadObjectRequest(bucket=self.bucket,key=key))
                    if head.content_length!=file['size_bytes'] or not head.etag:raise ObjectStoreError('package_source_size_mismatch','Source object changed after selection',409)
                    result=self.client.get_object(self.oss.GetObjectRequest(bucket=self.bucket,key=key,if_match=head.etag));size=0;digest=hashlib.sha256();last_check=time.monotonic()
                    try:
                        with archive.open(member(file['filename']),'w',force_zip64=True) as target:
                            for chunk in self._stream_chunks(result.body):
                                size+=len(chunk);total+=len(chunk)
                                if size>file['size_bytes'] or total>max_bytes:raise ObjectStoreError('package_source_size_mismatch','Source exceeds its fixed byte count',409)
                                if time.monotonic()-last_check>=1:check();last_check=time.monotonic()
                                digest.update(chunk);target.write(chunk)
                    finally:result.body.close()
                    if size!=file['size_bytes']:raise ObjectStoreError('package_source_size_mismatch','Source object was truncated',409)
                    if digest.hexdigest()!=file['sha256']:raise ObjectStoreError('package_source_hash_mismatch','Source checksum changed after approval',409)
            check();size=path.stat().st_size
            if size>max_bytes+1024*1024:raise ObjectStoreError('package_size_limit','Package exceeds its metadata overhead bound',413)
            sha=hashlib.sha256();md5=hashlib.md5(usedforsecurity=False)
            with path.open('rb') as source:
                while chunk:=source.read(65536):sha.update(chunk);md5.update(chunk)
            key=f'tenants/{tenant_id}/packages/{package_id}/bundle.zip';hash=sha.hexdigest()
            try:
                with path.open('rb') as source:
                    self.client.put_object(self.oss.PutObjectRequest(bucket=self.bucket,key=key,body=source,content_length=size,content_type='application/zip',
                        acl='private',forbid_overwrite=True,content_md5=base64.b64encode(md5.digest()).decode()))
            except (self.oss.exceptions.ServiceError,self.oss.exceptions.OperationError) as error:
                cause=error.unwrap() if isinstance(error,self.oss.exceptions.OperationError) else error
                if not isinstance(cause,self.oss.exceptions.ServiceError) or cause.status_code!=409 or cause.code!='FileAlreadyExists':raise
            check();verified=self.observe(key=key,expected_size=size,expected_hash=hash)
            return {'object_uri':self.object_uri(key),'sha256':verified['sha256'],'size_bytes':verified['size_bytes']}

    def snapshot(self, *, source_key: str, target_key: str, time_ms: int, source_etag: str) -> dict:
        """OSS performs the paid decode; cache the private JPEG under a stable key."""
        if not isinstance(time_ms, int) or time_ms < 0:
            raise ValueError("Invalid snapshot timestamp")
        try:
            head = self.client.head_object(self.oss.HeadObjectRequest(bucket=self.bucket, key=target_key))
        except (self.oss.exceptions.ServiceError, self.oss.exceptions.OperationError) as exc:
            cause = exc.unwrap() if isinstance(exc, self.oss.exceptions.OperationError) else exc
            if not isinstance(cause, self.oss.exceptions.ServiceError) or cause.status_code != 404 or cause.code != "NoSuchKey":
                raise
        else:
            if not head.content_length or head.content_length > 5 * 1024 * 1024:
                raise ObjectStoreError("snapshot_size_invalid", "Cached snapshot exceeds its size limit")
            return self.observe(key=target_key, expected_size=head.content_length, expected_hash=None)
        result = self.client.get_object(self.oss.GetObjectRequest(bucket=self.bucket, key=source_key,
            if_match=source_etag, process=f"video/snapshot,t_{time_ms},f_jpg,w_320,h_0,ar_auto"))
        data = self._read_bounded(result.body, 5 * 1024 * 1024)
        if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            raise ObjectStoreError("snapshot_format_invalid", "Cloud snapshot did not return a JPEG")
        return self.put_verified(key=target_key, data=data, content_type="image/jpeg")

    def archive(self, *, source_key: str, target_key: str, max_bytes: int, expected_hash: str | None) -> dict:
        state = self.client.get_bucket_versioning(self.oss.GetBucketVersioningRequest(bucket=self.bucket))
        if state.version_status not in {None, ""}:
            raise ObjectStoreError("bucket_versioning_incompatible", "Archive requires immutable object names", 503)
        head = self.client.head_object(self.oss.HeadObjectRequest(bucket=self.bucket, key=source_key))
        if not head.content_length or head.content_length > max_bytes:
            raise ObjectStoreError("archive_size_limit", "Artifact exceeds the archive verification limit", 413)
        observed = self.observe(key=source_key, expected_size=head.content_length, expected_hash=expected_hash)
        try:
            self.client.copy_object(self.oss.CopyObjectRequest(bucket=self.bucket, key=target_key,
                source_bucket=self.bucket, source_key=source_key, if_match=observed["etag"],
                forbid_overwrite=True, acl="private"))
        except (self.oss.exceptions.ServiceError, self.oss.exceptions.OperationError) as exc:
            cause = exc.unwrap() if isinstance(exc, self.oss.exceptions.OperationError) else exc
            if not isinstance(cause, self.oss.exceptions.ServiceError) or cause.status_code != 409 or cause.code != "FileAlreadyExists":
                raise
        # A resumed copy is accepted only if its actual content matches the source.
        return self.observe(key=target_key, expected_size=observed["size_bytes"], expected_hash=observed["sha256"])
