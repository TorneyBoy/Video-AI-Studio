"""Durable asynchronous ICE rendering; only external services inspect media."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from typing import Literal
from uuid import uuid4

from fastapi import Depends, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, JSON, String, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from timeline_engine import RenderManifest, Timeline, validate_timeline

from app.services.media_quality import assess_render_quality, assert_renderer_quality, renderer_findings, validate_render_quality
from app.services.render_features import advanced_features
from app.services.providers import ProviderExecutionError
from .database import Base
from .ice_client import API_VERSION, SDK_VERSION, ICEAPIError, ICEClient
from .ice_timeline import COMPILER_VERSION, OPTIONAL_FEATURES, compile_ice_timeline, oss_media_url
from .oss import ObjectStoreError
from .model_identity import registered_result


MODEL_ID = "aliyun-ice-cloud-editing"
PROVIDER = "aliyun-ice-editing"
PRICE_VERSION = "aliyun-ims-basic-clip-cn-20261018"
PRICE_SOURCE = "https://help.aliyun.com/zh/ims/video-clip"
ID = r"^[A-Za-z0-9_-]{1,64}$"


class RenderQuota(Base):
    __tablename__ = "gateway_render_quota"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RenderOperation(Base):
    __tablename__ = "gateway_render_operations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    specification: Mapped[dict] = mapped_column(JSON, nullable=False)
    object_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    cloud_job_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False)
    submit_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    polls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    request_id: str = Field(pattern=ID)
    tenant_id: str = Field(pattern=ID)
    capability: Literal["video_editing"]
    model_id: str | None = None
    model_version: str | None = None
    commercial_use: bool = True
    inputs: dict
    parameters: dict = Field(default_factory=dict)
    consent_ids: list[str] = Field(default_factory=list, max_length=100)
    region: str | None = None


def calculated_clip_cost(*, duration: float, width: int, height: int, region: str) -> dict:
    """A transparent tariff calculation; never label it an issued cloud bill."""
    if not region.startswith("cn-") or region == "cn-hongkong" or not math.isfinite(duration) or duration <= 0:
        return {"status": "unknown", "currency": "CNY"}
    short_edge = min(width, height)
    rate = next((Decimal(amount) for bound, amount in [(480, "0.015"), (720, "0.03"), (1080, "0.06")]
                 if 0 < short_edge <= bound), None)
    if rate is None:
        return {"status": "unknown", "currency": "CNY"}
    minutes = math.ceil(duration / 60)
    return {"amount": str(rate * minutes), "currency": "CNY", "status": "calculated",
            "bill_confirmed": False, "billable_minutes": minutes, "rate_per_minute": str(rate),
            "price_version": PRICE_VERSION, "price_source": PRICE_SOURCE,
            "basis": "measured_output_and_published_non_advanced_template_tariff"}


def register_render_routes(app, *, settings, sessions, store, authorize, ice_client=None):
    def enabled():
        if not settings.ice_render_enabled:
            raise ObjectStoreError("ice_render_disabled", "Cloud rendering has not been enabled", 503)

    def response(row):
        if row.result:
            return registered_result(row.result, provider=PROVIDER, legacy_provider="aliyun-ice",
                                     model_id=MODEL_ID, version=API_VERSION)
        if row.status == "failed":
            return {"provider_job_id": row.id, "status": "failed", "retryable": False,
                    "error": {"code": row.error_code, "message": "Cloud rendering requires attention"}}
        return {"provider_job_id": row.id, "status": row.status, "request_id": row.request_id}

    @app.get("/v1/render/health", dependencies=[Depends(authorize)])
    def health():
        return {"status": "ok", "available": settings.ice_render_enabled, "cloud_verified": False,
                "service_api_version": API_VERSION, "compiler_version": COMPILER_VERSION,
                "enabled_features": settings.ice_render_features, "compiler_features": sorted(OPTIONAL_FEATURES),
                "quality_scope": "structural_probe",
                "blocked_execution_constraints": ["ice_image_end_unverified"]}

    @app.post("/v1/render", dependencies=[Depends(authorize)])
    def submit(payload: RenderRequest, request_id: str = Header(alias="X-Request-ID"),
               idempotency_key: str = Header(alias="Idempotency-Key")):
        enabled()
        if payload.request_id != request_id or idempotency_key != request_id:
            raise ObjectStoreError("render_request_identity_mismatch", "Render request identity differs from its headers", 409)
        if payload.region not in {None, settings.oss_region} or payload.model_id not in {None, MODEL_ID} or payload.model_version not in {None, API_VERSION}:
            raise ObjectStoreError("render_configuration_mismatch", "Render service configuration differs from the request", 422)
        try:
            manifest = RenderManifest.model_validate(payload.inputs.get("manifest"))
            if not manifest.verify_hash():
                raise ValueError("manifest integrity invalid")
            timeline = Timeline.model_validate(manifest.source["timeline"])
            validate_timeline(timeline).raise_for_errors()
            supplied = Timeline.model_validate(payload.inputs.get("timeline"))
            if supplied != timeline or manifest.duration != timeline.duration or manifest.fps != timeline.fps:
                raise ValueError("manifest and timeline differ")
            if (manifest.timeline_id != timeline.id or manifest.frame_count != timeline.frame_count or
                    manifest.resolution != timeline.resolution.model_dump(mode="json")):
                raise ValueError("manifest measurements differ")
            expected_assets = [(asset.id, asset.uri, asset.type.value, asset.checksum) for asset in timeline.assets]
            if [(asset.id, asset.uri, asset.type, asset.checksum) for asset in manifest.assets] != expected_assets:
                raise ValueError("manifest asset bindings differ")
            if payload.inputs.get("asset_uris") != [asset.uri for asset in manifest.assets if asset.uri]:
                raise ValueError("input asset list differs")
            quality = manifest.options.get("quality", "draft")
            if quality not in {"draft", "standard", "high", "studio", "final"} or payload.parameters.get("quality", quality) != quality:
                raise ValueError("quality profile differs")
            # The shared control API uses standard/high/studio for exports.
            # Keep the original manifest/hash; only map its output quality.
            # `source` records the UI entry point, never a cloud edit/effect.
            source = manifest.options.get("source")
            if source is not None and (not isinstance(source, str) or len(source) > 64):
                raise ValueError("invalid entry point metadata")
            if set(manifest.options) - {"quality", "source"} or manifest.output != {"format": "mp4"}:
                raise ValueError("unsupported output options")
            for asset in timeline.assets:
                store.key_from_uri(asset.uri, payload.tenant_id)
            if timeline.duration > settings.ice_render_max_duration_seconds or min(timeline.resolution.width, timeline.resolution.height) > 1080:
                raise ValueError("render exceeds the configured trial limits")
            cloud_timeline, output = compile_ice_timeline(timeline, bucket=settings.oss_bucket, region=settings.oss_region,
                                                        enabled_features=settings.ice_render_features)
        except (ValueError, KeyError, TypeError) as exc:
            raise ObjectStoreError("render_input_unsupported", "Render input does not satisfy the supported cloud profile", 422) from exc
        token = hashlib.sha256(f"{payload.tenant_id}\n{request_id}".encode()).hexdigest()
        uri = store.object_uri(f"tenants/{payload.tenant_id}/renders/{token}/output.mp4")
        output.update(MediaURL=oss_media_url(uri, bucket=settings.oss_bucket, region=settings.oss_region),
                      Bitrate=1500 if quality == "draft" else 5000)
        specification = {"timeline": timeline.model_dump(mode="json"), "cloud_timeline": cloud_timeline,
                         "output": output, "manifest_hash": manifest.content_hash}
        digest = hashlib.sha256(json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        # Metadata is frozen on first creation but excluded from the historical
        # request digest, so replaying an accepted M1 operation remains valid.
        specification["compiler_version"] = COMPILER_VERSION
        specification["optional_features"] = sorted(set(advanced_features(specification["timeline"])) & OPTIONAL_FEATURES)
        with sessions() as db:
            existing = db.get(RenderOperation, token)
            if existing:
                if existing.request_hash != digest:
                    raise ObjectStoreError("render_idempotency_conflict", "Render identity is bound to different input", 409)
                return response(existing)
            try:
                assert_renderer_quality(timeline, renderer_model_id=MODEL_ID)
            except ProviderExecutionError as exc:
                raise ObjectStoreError(exc.code, "Renderer cannot satisfy the declared quality contract", 422) from exc
            try:
                if db.get(RenderQuota, "trial") is None:
                    with db.begin_nested():
                        db.add(RenderQuota(id="trial", used=0))
                        db.flush()
            except IntegrityError:
                pass
            try:
                row = RenderOperation(id=token, request_id=request_id, tenant_id=payload.tenant_id,
                    request_hash=digest, specification=specification, object_uri=uri)
                db.add(row)
                db.flush()
                reserved = db.execute(update(RenderQuota).where(RenderQuota.id == "trial", RenderQuota.used < settings.ice_render_max_operations)
                                      .values(used=RenderQuota.used + 1))
                if reserved.rowcount != 1:
                    raise ObjectStoreError("render_trial_quota_exhausted", "Cloud trial operation limit reached", 429)
                db.commit()
            except IntegrityError:
                db.rollback()
                row = db.get(RenderOperation, token)
                if row is None or row.request_hash != digest:
                    raise ObjectStoreError("render_idempotency_conflict", "Render identity is bound to different input", 409)
            return response(row)

    @app.get("/v1/render/{operation_id}", dependencies=[Depends(authorize)])
    def poll(operation_id: str, request_id: str = Header(alias="X-Request-ID")):
        enabled()
        now, lease = datetime.now(timezone.utc), uuid4().hex
        with sessions() as db:
            row = db.get(RenderOperation, operation_id)
            if row is None or row.request_id != request_id:
                raise ObjectStoreError("render_not_found", "Render operation not found", 404)
            if row.status in {"succeeded", "failed"}:
                return response(row)
            claimed = db.execute(update(RenderOperation).where(RenderOperation.id == operation_id,
                RenderOperation.status.not_in(["succeeded", "failed"]),
                or_(RenderOperation.lease_expires_at.is_(None), RenderOperation.lease_expires_at <= now))
                .values(lease_token=lease, lease_expires_at=now + timedelta(minutes=5))
                .execution_options(synchronize_session=False))
            db.commit()
            if claimed.rowcount != 1:
                return response(row)
            db.refresh(row)
            specification, uri, remote_id = row.specification, row.object_uri, row.cloud_job_id
            if row.submit_attempts >= 3 and not remote_id or row.polls >= 120:
                row.status, row.error_code = "failed", "ice_operation_reconciliation_required"
                db.commit()
                return response(row)
            if remote_id is None:
                try:
                    # Count only real supplier submissions, not an admission
                    # failure for a request queued by an earlier release.
                    assert_renderer_quality(specification["timeline"], renderer_model_id=MODEL_ID)
                except ProviderExecutionError as exc:
                    row.status, row.error_code = "failed", exc.code
                    row.lease_token, row.lease_expires_at = None, None
                    db.commit()
                    return response(row)
            row.submit_attempts += 0 if remote_id else 1
            row.polls += 1 if remote_id else 0
            db.commit()
        client = ice_client or ICEClient(region=settings.oss_region, credentials_provider=store.credentials_provider)
        changes = {"lease_token": None, "lease_expires_at": None}
        try:
            if remote_id is None:
                value = client.submit_render(client_token=operation_id, timeline=specification["cloud_timeline"], output=specification["output"])
                changes.update(cloud_job_id=value["job_id"], status="running")
            else:
                value = client.render_status(remote_id)
                changes["status"] = value["status"]
                if value["status"] == "failed":
                    changes["error_code"] = "ice_cloud_render_failed"
                if value["status"] == "succeeded":
                    changes["result"] = inspect_render(client, store=store, settings=settings, uri=uri,
                        specification=specification, status=value, request_id=request_id, operation_id=operation_id)
        except ICEAPIError as exc:
            changes.update(status="failed" if exc.submission_rejected or not exc.retryable else "running", error_code=exc.code)
        except ProviderExecutionError as exc:
            changes.update(status="failed", error_code=exc.code)
        except (ObjectStoreError, ValueError, TypeError, KeyError):
            # Remote rendering may already be billable. Preserve its ID for
            # reconciliation and never restart it as a new operation.
            changes.update(status="failed", error_code="ice_result_verification_failed")
        except Exception:
            changes.update(status="running", error_code="ice_operation_unconfirmed")
        with sessions.begin() as db:
            db.execute(update(RenderOperation).where(RenderOperation.id == operation_id, RenderOperation.lease_token == lease).values(**changes))
        with sessions() as db:
            return response(db.get(RenderOperation, operation_id))


def inspect_render(client, *, store, settings, uri: str, specification: dict, status: dict,
                   request_id: str, operation_id: str) -> dict:
    expected_url = oss_media_url(uri, bucket=settings.oss_bucket, region=settings.oss_region)
    if oss_media_url(status.get("media_url") or "", bucket=settings.oss_bucket, region=settings.oss_region) != expected_url:
        raise ValueError("cloud output identity differs")
    probe = client.media_info(uri)
    facts, timeline = probe["facts"], Timeline.model_validate(specification["timeline"])
    if facts["size_bytes"] > settings.max_upload_bytes:
        raise ValueError("render output exceeds verification limit")
    observed = store.observe(key=store.key_from_uri(uri, specification_tenant(uri)),
                             expected_size=facts["size_bytes"], expected_hash=None)
    video = facts.get("video") or {}
    report = {"schema_version": "media-quality/1", "status": "passed", "object_uri": uri,
        "output_sha256": observed["sha256"], "size_bytes": observed["size_bytes"],
        "duration_seconds": facts["duration_seconds"], "width": video.get("width"), "height": video.get("height"),
        "fps": video.get("frame_rate"), "video_codec": video.get("codec"), "container": facts["container"],
        "has_audio": facts["has_audio"], "inspector": "aliyun-ice-media-info-and-oss-sha256",
        "inspector_version": f"{API_VERSION}/sdk-{SDK_VERSION}/sha256-v1"}
    assessment = assess_render_quality(report, timeline=timeline, output_uri=uri)
    try:
        report = validate_render_quality(report, timeline=timeline, output_uri=uri)
    except ProviderExecutionError:
        # Return the charge-bearing cloud result with failed quality evidence;
        # the control worker records cost and refuses delivery.
        report.update(status="failed", failure_code="provider_render_quality_invalid")
    return {"provider_job_id": operation_id, "status": "succeeded", "request_id": request_id,
        "artifact_uri": uri, "artifact_type": "render",
        "model": {"provider": PROVIDER, "model_id": MODEL_ID, "version": API_VERSION},
        "metrics": {"quality_control": report, "cloud_job_id": status["job_id"],
                    "render_quality_assessment": assessment,
                    "renderer_quality_findings": renderer_findings(timeline, renderer_model_id=MODEL_ID),
                    "cloud_request_id": status["request_id"], "probe_request_id": probe["request_id"],
                    "probe_job_id": probe["job_id"], "manifest_hash": specification["manifest_hash"],
                    "compiler_version": specification.get("compiler_version", "initial-v3-accepted-subset"),
                    "render_features": specification.get("optional_features", [])},
        "usage": {"output_seconds": facts["duration_seconds"]},
        "cost": ({"status": "unknown", "currency": "CNY", "bill_confirmed": False,
                  "reason": "advanced_render_tariff_requires_reconciliation"} if specification.get("optional_features") else
                 calculated_clip_cost(duration=facts["duration_seconds"], width=video.get("width", 0),
                                      height=video.get("height", 0), region=settings.oss_region))}


def specification_tenant(uri: str) -> str:
    from urllib.parse import urlparse

    parts = urlparse(uri).path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "tenants":
        raise ValueError("render has no tenant namespace")
    return parts[1]
