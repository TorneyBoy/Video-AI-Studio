"""Durable Wan generation/person replacement with an unknown-submission fence."""
from datetime import datetime, timedelta, timezone
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

from .database import Base
from .ice_client import ICEAPIError, ICEClient
from .oss import ObjectStoreError
from .wan_client import WanAPIError, WanClient, WanSettings


ID = r"^[A-Za-z0-9_-]{1,64}$"
PROVIDER = "aliyun-wan"
PROVIDER_MODELS = {"aliyun-wan-t2v": "wan2.2-t2v-plus", "aliyun-wan-i2v": "wan2.2-i2v-plus",
                   "aliyun-wan-person": "wan2.2-animate-mix"}
MODELS = {"video_generation": {"wan2.2-t2v-plus", "wan2.2-i2v-plus"},
          "person_replacement": {"wan2.2-animate-mix"}}
CONTEXT_INPUTS = {"asset_id", "source_asset_id", "reference_asset_ids", "avatar_profile_id", "voice_profile_id",
                  "audio_asset_id", "operation", "consent_id", "quality"}


class WanQuota(Base):
    __tablename__ = "gateway_wan_quota"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WanOperation(Base):
    __tablename__ = "gateway_wan_operations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    specification: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    cloud_job_id: Mapped[str | None] = mapped_column(String(128))
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    request_id: str = Field(pattern=ID)
    tenant_id: str = Field(pattern=ID)
    capability: Literal["video_generation", "person_replacement"]
    model_id: str
    model_version: str
    inputs: dict
    parameters: dict = Field(default_factory=dict)
    consent_ids: list[str] = Field(default_factory=list, max_length=100)
    region: str | None = None
    commercial_use: bool = True


def _specification(payload, settings, store, *, provider_name=PROVIDER):
    if provider_name != PROVIDER and PROVIDER_MODELS.get(provider_name) != payload.model_id:
        raise ValueError("provider identity does not match model")
    if payload.model_id not in MODELS[payload.capability] or payload.model_version != settings.model_version:
        raise ValueError("model configuration differs")
    if payload.region not in {None, store.region}:
        raise ValueError("region differs")
    inputs, params = payload.inputs, payload.parameters
    if set(inputs) - ({"prompt", "source_asset_uri", "reference_asset_uris", "negative_prompt"} | CONTEXT_INPUTS):
        raise ValueError("unsupported input")
    if set(params) - {"duration", "seed", "resolution", "mode", "negative_prompt"}:
        raise ValueError("unsupported parameter")
    expected_operation = "person_replace" if payload.capability == "person_replacement" else "video_generation"
    if inputs.get("operation") not in {None, expected_operation}:
        raise ValueError("operation does not match capability")
    if inputs.get("consent_id") and inputs["consent_id"] not in payload.consent_ids:
        raise ValueError("consent context differs")
    if "negative_prompt" in inputs and "negative_prompt" in params and inputs["negative_prompt"] != params["negative_prompt"]:
        raise ValueError("ambiguous negative prompt")
    negative = inputs.get("negative_prompt", params.get("negative_prompt"))
    prompt = inputs.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 800:
        raise ValueError("prompt exceeds model contract")
    references = inputs.get("reference_asset_uris", [])
    if not isinstance(references, list) or len(references) > 1 or any(not isinstance(uri, str) for uri in references):
        raise ValueError("exactly one or no image reference required")
    source = inputs.get("source_asset_uri")
    for uri in [source, *references]:
        if uri is not None:
            if not isinstance(uri, str) or not uri:
                raise ValueError("invalid source")
            store.key_from_uri(uri, payload.tenant_id)
    cloud_input, cloud_parameters = {}, {}
    if payload.capability == "person_replacement":
        if not source or len(references) != 1 or not payload.consent_ids:
            raise ValueError("person replacement needs video, image and consent")
        if set(params) - {"duration", "mode"} or prompt or negative:
            raise ValueError("replacement does not accept generation parameters")
        mode = params.get("mode", "wan-std")
        if mode not in {"wan-std", "wan-pro"}:
            raise ValueError("invalid replacement mode")
        duration = params.get("duration")
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                                     or not math.isfinite(duration) or not 2 <= duration <= 30):
            raise ValueError("invalid replacement source duration")
        cloud_input = {"video_url": source, "image_url": references[0]}
        cloud_parameters = {"mode": mode, "check_image": True}
    else:
        duration = params.get("duration", 5)
        if isinstance(duration, bool) or duration != 5 or "mode" in params:
            raise ValueError("selected Wan generation models only produce five seconds")
        resolution = params.get("resolution", "1080P")
        if resolution not in {"480P", "1080P"}:
            raise ValueError("unsupported resolution")
        cloud_input = {"prompt": prompt}
        if negative is not None:
            if not isinstance(negative, str) or len(negative) > 500:
                raise ValueError("invalid negative prompt")
            cloud_input["negative_prompt"] = negative
        cloud_parameters = {"prompt_extend": False}
        if "seed" in params:
            seed = params["seed"]
            if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2147483647:
                raise ValueError("invalid seed")
            cloud_parameters["seed"] = seed
        if payload.model_id == "wan2.2-t2v-plus":
            if not prompt.strip() or source or references:
                raise ValueError("text generation requires prompt and no image inputs")
            cloud_parameters["size"] = {"480P": "480*832", "1080P": "1080*1920"}[resolution]
        else:
            if bool(source) == bool(references):
                raise ValueError("image generation requires one unambiguous image")
            cloud_input["img_url"] = source or references[0]
            cloud_parameters["resolution"] = resolution
    return {"provider": provider_name, "capability": payload.capability, "model_id": payload.model_id, "model_version": payload.model_version,
            "cloud_input": cloud_input, "cloud_parameters": cloud_parameters,
            "consent_ids": sorted(set(payload.consent_ids)), "duration": params.get("duration", 5),
            "control_context": {key: inputs[key] for key in sorted(CONTEXT_INPUTS) if key in inputs}}


def register_wan_routes(app, *, settings: WanSettings, sessions, store, authorize, client=None, ice_client=None):
    def enabled():
        if not settings.enabled:
            raise ObjectStoreError("wan_disabled", "Hosted Wan has not been enabled", 503)

    def response(row):
        if row.result:
            return row.result
        value = {"provider_job_id": row.id, "request_id": row.request_id,
                 "status": "failed" if row.status == "failed" else "queued" if row.status == "queued" else "running"}
        if row.error_code:
            value["error"] = {"code": row.error_code, "message": "Hosted Wan requires confirmation"}
        if row.status == "submit_unknown":
            value["submission_unknown"] = True
        if row.status == "failed":
            value["retryable"] = False
            if row.cloud_job_id:
                value["cost"] = {"status": "unknown", "currency": "CNY", "bill_confirmed": False}
                value["metrics"] = {"cloud_job_id": row.cloud_job_id}
        return value

    @app.get("/v1/wan/health", dependencies=[Depends(authorize)])
    def health():
        return {"status": "ok", "available": settings.enabled, "cloud_verified": False,
                "model_version": settings.model_version, "capabilities": sorted(MODELS)}

    @app.post("/v1/wan", dependencies=[Depends(authorize)])
    def submit(payload: WanRequest, request_id: str = Header(alias="X-Request-ID"),
               idempotency_key: str = Header(alias="Idempotency-Key"),
               provider_name: str = Header(default=PROVIDER, alias="X-Wan-Provider")):
        enabled()
        if request_id != payload.request_id or idempotency_key != request_id:
            raise ObjectStoreError("wan_request_identity_mismatch", "Wan request identity differs from its headers", 409)
        try:
            specification = _specification(payload, settings, store, provider_name=provider_name)
        except (ValueError, TypeError, KeyError):
            raise ObjectStoreError("wan_input_unsupported", "Input does not satisfy the selected Wan model contract", 422) from None
        token = hashlib.sha256(f"{payload.tenant_id}\n{request_id}".encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with sessions() as db:
            existing = db.get(WanOperation, token)
            if existing:
                if existing.request_hash != digest:
                    raise ObjectStoreError("wan_idempotency_conflict", "Wan identity is bound to different input", 409)
                return response(existing)
            try:
                if db.get(WanQuota, "wan") is None:
                    with db.begin_nested():
                        db.add(WanQuota(id="wan", used=0)); db.flush()
            except IntegrityError:
                pass
            try:
                row = WanOperation(id=token, request_id=request_id, tenant_id=payload.tenant_id,
                                   request_hash=digest, specification=specification, progress={})
                db.add(row); db.flush()
                reserved = db.execute(update(WanQuota).where(WanQuota.id == "wan", WanQuota.used < settings.max_operations)
                                      .values(used=WanQuota.used + 1))
                if reserved.rowcount != 1:
                    raise ObjectStoreError("wan_operation_limit", "Wan operation limit reached", 429)
                db.commit()
            except IntegrityError:
                db.rollback(); row = db.get(WanOperation, token)
                if row is None or row.request_hash != digest:
                    raise ObjectStoreError("wan_idempotency_conflict", "Wan identity is bound to different input", 409)
            return response(row)

    @app.get("/v1/wan/jobs/{operation_id}", dependencies=[Depends(authorize)])
    def poll(operation_id: str, request_id: str = Header(alias="X-Request-ID")):
        enabled()
        now, lease = datetime.now(timezone.utc), uuid4().hex
        with sessions() as db:
            row = db.get(WanOperation, operation_id)
            if row is None or row.request_id != request_id:
                raise ObjectStoreError("wan_not_found", "Wan operation not found", 404)
            if row.result or row.status in {"failed", "submit_unknown"}:
                return response(row)
            claimed = db.execute(update(WanOperation).where(WanOperation.id == operation_id,
                WanOperation.status.not_in(["failed", "submit_unknown", "succeeded"]),
                or_(WanOperation.lease_expires_at.is_(None), WanOperation.lease_expires_at <= now))
                .values(lease_token=lease, lease_expires_at=now + timedelta(minutes=5)).execution_options(synchronize_session=False))
            db.commit(); db.refresh(row)
            if claimed.rowcount != 1:
                return response(row)
            spec, remote_id, progress, tenant = row.specification, row.cloud_job_id, dict(row.progress), row.tenant_id
            if not remote_id and row.status == "submitting":
                row.status, row.error_code = "submit_unknown", "wan_submission_outcome_unknown"
                row.lease_token = row.lease_expires_at = None
                db.commit()
                return response(row)

        def save(**values):
            with sessions.begin() as db:
                saved = db.execute(update(WanOperation).where(WanOperation.id == operation_id,
                    WanOperation.lease_token == lease).values(**values))
                if saved.rowcount != 1:
                    raise LeaseLost()

        wan = client or WanClient(settings)
        try:
            if not remote_id:
                cloud_input = dict(spec["cloud_input"])
                for key in ("image_url", "video_url", "img_url"):
                    if key in cloud_input:
                        source_key = store.key_from_uri(cloud_input[key], tenant)
                        cloud_input[key] = store.access(key=source_key, ttl_seconds=3600, purpose="preview")["url"]
                save(status="submitting")  # Persist the fence before the billable POST.
                try:
                    value = wan.submit(model_id=spec["model_id"], inputs=cloud_input, parameters=spec["cloud_parameters"])
                except WanAPIError as exc:
                    save(status="failed" if exc.submission_rejected else "submit_unknown", error_code=exc.code)
                except Exception:
                    save(status="submit_unknown", error_code="wan_submission_outcome_unknown")
                else:
                    save(cloud_job_id=value["task_id"], status="running", progress={"submit_request_id": value.get("request_id")})
            else:
                value = wan.task_status(remote_id)
                if value["status"] in {"failed", "unknown"}:
                    save(status="failed" if value["status"] == "failed" else "running",
                         error_code="wan_cloud_job_failed" if value["status"] == "failed" else "wan_remote_reconciliation_required")
                elif value["status"] == "succeeded":
                    key = f"tenants/{tenant}/generated/{operation_id}/output.mp4"
                    uri = store.object_uri(key)
                    if "observed" not in progress:
                        data = wan.download_video(value["video_url"])
                        progress["observed"] = store.put_verified(key=key, data=data, content_type="video/mp4")
                        save(progress=progress)
                    ice = ice_client or ICEClient(region=store.region, credentials_provider=store.credentials_provider)
                    result = _inspect_result(ice, store, key, uri, spec, progress["observed"], operation_id, request_id, remote_id)
                    save(status="succeeded", result=result, error_code=None)
        except LeaseLost:
            pass
        except (WanAPIError, ICEAPIError) as exc:
            save(status="running" if exc.retryable else "failed", error_code=exc.code)
        except (ObjectStoreError, ValueError, KeyError, TypeError):
            save(status="failed", error_code="wan_result_verification_failed")
        except Exception:
            # Persisted submitting is never reset to queued. A later query
            # fences it instead of treating an uncertain POST as retryable.
            save(error_code="wan_operation_unconfirmed")
        finally:
            with sessions.begin() as db:
                db.execute(update(WanOperation).where(WanOperation.id == operation_id, WanOperation.lease_token == lease)
                           .values(lease_token=None, lease_expires_at=None))
        with sessions() as db:
            return response(db.get(WanOperation, operation_id))


class LeaseLost(RuntimeError):
    pass


def _inspect_result(ice, store, key, uri, spec, observed, operation_id, request_id, remote_id):
    probe = ice.media_info(uri)
    facts = probe["facts"]; video = facts.get("video") or {}
    duration, fps = facts.get("duration_seconds"), video.get("frame_rate")
    if (not probe.get("job_id") or not probe.get("request_id")
            or facts.get("size_bytes") != observed["size_bytes"] or facts.get("container") not in {"mp4", "mov,mp4,m4a,3gp,3g2,mj2"}
            or not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0
            or not isinstance(fps, (int, float)) or isinstance(fps, bool) or not math.isfinite(fps) or not 0 < fps <= 240
            or any(not isinstance(video.get(name), int) or isinstance(video.get(name), bool) or video[name] <= 0 for name in ("width", "height"))):
        raise ValueError("invalid generated media evidence")
    if spec["capability"] == "video_generation" and abs(duration - 5) > max(1 / fps, .1):
        raise ValueError("generated media duration differs from fixed model duration")
    if spec["capability"] == "person_replacement" and not 2 <= duration <= 30.1:
        raise ValueError("replacement media duration exceeds model bounds")
    confirmed = store.observe(key=key, expected_size=observed["size_bytes"], expected_hash=observed["sha256"])
    media = {"status": "ok", "object_uri": uri, "sha256": confirmed["sha256"], "size_bytes": confirmed["size_bytes"],
             "duration_seconds": duration, "width": video["width"], "height": video["height"], "fps": fps,
             "inspector": "aliyun-ice-media-info-and-oss-sha256", "probe_job_id": probe["job_id"],
             "probe_request_id": probe["request_id"]}
    return {"provider_job_id": operation_id, "request_id": request_id, "status": "succeeded", "artifact_uri": uri,
            "artifact_type": "video", "model": {"provider": spec.get("provider", PROVIDER), "model_id": spec["model_id"], "version": spec["model_version"]},
            "metrics": {"media": media, "cloud_job_id": remote_id}, "usage": {"output_seconds": duration},
            "cost": {"status": "unknown", "currency": "CNY", "bill_confirmed": False, "reason": "wan_invoice_reconciliation_required"}}
