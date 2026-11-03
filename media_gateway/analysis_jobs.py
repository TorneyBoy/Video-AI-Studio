"""Durable hosted analysis with explicit unknown-submission fences.

ICE probes and transcribes; Function Compute detects shots; OSS produces thumbnails. This
process only relays bounded objects and normalizes the returned metadata.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import Literal
from uuid import uuid4

from fastapi import Depends, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, JSON, String, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.services.analysis_contract import validate_hosted_analysis
from .database import Base
from .ice_client import API_VERSION as ICE_VERSION, ICEAPIError, ICEClient
from .oss import ObjectStoreError
from .model_identity import registered_result
from .shot_client import DETECTOR_VERSION, ShotClient, ShotServiceError, normalize_shots


MODEL_ID = "aliyun-hosted-media-analysis"
MODEL_VERSION = "media-analysis-2-20261018"
PROVIDER = "aliyun-hosted-analysis"
ID = r"^[A-Za-z0-9_-]{1,64}$"


class AnalysisQuota(Base):
    __tablename__ = "gateway_analysis_quota"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AnalysisOperation(Base):
    __tablename__ = "gateway_analysis_operations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    specification: Mapped[dict] = mapped_column(JSON, nullable=False)
    progress: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    polls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    request_id: str = Field(pattern=ID)
    tenant_id: str = Field(pattern=ID)
    capability: Literal["media_analysis"]
    model_id: str | None = None
    model_version: str | None = None
    commercial_use: bool = True
    inputs: dict
    parameters: dict = Field(default_factory=dict)
    consent_ids: list[str] = Field(default_factory=list, max_length=100)
    region: str | None = None


class LeaseLost(ValueError):
    pass


def register_analysis_routes(app, *, settings, sessions, store, authorize, ice_client=None, shot_client=None):
    def enabled():
        if not settings.analysis_enabled:
            raise ObjectStoreError("analysis_disabled", "Hosted media analysis has not been enabled", 503)

    def response(row):
        if row.result:
            return registered_result(row.result, provider=PROVIDER, legacy_provider="aliyun-hosted",
                                     model_id=MODEL_ID, version=MODEL_VERSION)
        if row.status == "failed":
            return {"provider_job_id": row.id, "request_id": row.request_id, "status": "failed", "retryable": False,
                    "error": {"code": row.error_code, "message": "Hosted analysis requires attention"}}
        return {"provider_job_id": row.id, "status": row.status, "request_id": row.request_id}

    @app.get("/v1/analysis/health", dependencies=[Depends(authorize)])
    def health():
        return {"status": "ok", "available": settings.analysis_enabled, "cloud_verified": False,
                "contract_version": "media-analysis/2"}

    @app.post("/v1/analysis", dependencies=[Depends(authorize)])
    def submit(payload: AnalysisRequest, request_id: str = Header(alias="X-Request-ID"),
               idempotency_key: str = Header(alias="Idempotency-Key")):
        enabled()
        if payload.request_id != request_id or idempotency_key != request_id:
            raise ObjectStoreError("analysis_request_identity_mismatch", "Analysis request identity differs", 409)
        if (payload.region not in {None, settings.oss_region} or payload.model_id not in {None, MODEL_ID}
                or payload.model_version not in {None, MODEL_VERSION}):
            raise ObjectStoreError("analysis_configuration_mismatch", "Analysis service configuration differs", 422)
        uri = payload.inputs.get("asset_uri") or payload.inputs.get("uri")
        if not isinstance(uri, str) or payload.inputs.get("uri", uri) != uri:
            raise ObjectStoreError("analysis_source_invalid", "One stable analysis source is required", 422)
        store.key_from_uri(uri, payload.tenant_id)
        asset_id, expected_hash = payload.inputs.get("asset_id"), payload.inputs.get("sha256")
        if not isinstance(asset_id, str) or not re.fullmatch(ID, asset_id):
            raise ObjectStoreError("analysis_asset_invalid", "A stable asset identity is required", 422)
        if expected_hash is not None and (not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
            raise ObjectStoreError("analysis_hash_invalid", "Invalid source checksum", 422)
        allowed = {"shot_analysis", "transcribe", "run_transcription", "timeout_seconds", "whisper_model", "provider_job_id"}
        if set(payload.parameters) - allowed:
            raise ObjectStoreError("analysis_options_unsupported", "Analysis options are not supported by this hosted profile", 422)
        for flag in ("shot_analysis", "transcribe", "run_transcription"):
            if flag in payload.parameters and not isinstance(payload.parameters[flag], bool):
                raise ObjectStoreError("analysis_options_invalid", "Analysis flags must be booleans", 422)
        specification = {"source_uri": uri, "asset_id": asset_id, "sha256": expected_hash,
            "shots": payload.parameters.get("shot_analysis", True),
            "transcribe": bool(payload.parameters.get("transcribe") or payload.parameters.get("run_transcription"))}
        if specification["shots"] and not settings.analysis_fc_account_id:
            raise ObjectStoreError("analysis_function_unconfigured", "The cloud shot function must be configured", 503)
        token = hashlib.sha256(f"{payload.tenant_id}\n{request_id}".encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with sessions() as db:
            row = db.get(AnalysisOperation, token)
            if row:
                if row.request_hash != digest:
                    raise ObjectStoreError("analysis_idempotency_conflict", "Analysis identity is bound to different input", 409)
                return response(row)
            try:
                if db.get(AnalysisQuota, "trial") is None:
                    with db.begin_nested():
                        db.add(AnalysisQuota(id="trial", used=0))
                        db.flush()
            except IntegrityError:
                pass
            try:
                row = AnalysisOperation(id=token, request_id=request_id, tenant_id=payload.tenant_id,
                    request_hash=digest, specification=specification, progress={})
                db.add(row)
                db.flush()
                reserved = db.execute(update(AnalysisQuota).where(AnalysisQuota.id == "trial", AnalysisQuota.used < settings.analysis_max_operations)
                                      .values(used=AnalysisQuota.used + 1))
                if reserved.rowcount != 1:
                    raise ObjectStoreError("analysis_trial_quota_exhausted", "Analysis trial operation limit reached", 429)
                db.commit()
            except IntegrityError:
                db.rollback()
                row = db.get(AnalysisOperation, token)
                if row is None or row.request_hash != digest:
                    raise ObjectStoreError("analysis_idempotency_conflict", "Analysis identity is bound to different input", 409)
            return response(row)

    @app.get("/v1/analysis/{operation_id}", dependencies=[Depends(authorize)])
    def poll(operation_id: str, request_id: str = Header(alias="X-Request-ID")):
        enabled()
        now, lease = datetime.now(timezone.utc), uuid4().hex
        with sessions() as db:
            row = db.get(AnalysisOperation, operation_id)
            if row is None or row.request_id != request_id:
                raise ObjectStoreError("analysis_not_found", "Analysis operation not found", 404)
            if row.result or row.status == "failed":
                return response(row)
            claimed = db.execute(update(AnalysisOperation).where(AnalysisOperation.id == operation_id,
                or_(AnalysisOperation.lease_expires_at.is_(None), AnalysisOperation.lease_expires_at <= now))
                .values(lease_token=lease, lease_expires_at=now + timedelta(minutes=5), status="running", polls=AnalysisOperation.polls + 1)
                .execution_options(synchronize_session=False))
            db.commit()
            if claimed.rowcount != 1:
                db.refresh(row)
                return response(row)
            db.refresh(row)
            spec, progress, tenant, polls = row.specification, row.progress, row.tenant_id, row.polls

        def save(data, **values):
            with sessions.begin() as db:
                saved = db.execute(update(AnalysisOperation).where(AnalysisOperation.id == operation_id,
                    AnalysisOperation.lease_token == lease).values(progress=data, **values))
                if saved.rowcount != 1:
                    raise LeaseLost("Analysis execution lease was superseded")

        try:
            if polls > 180:
                # Preserve cloud identifiers and paid/unknown stage states for reconciliation.
                save(progress, status="failed", error_code="analysis_reconciliation_required")
            else:
                ice = ice_client or ICEClient(region=settings.oss_region, credentials_provider=store.credentials_provider)
                detector = shot_client
                if spec["shots"] and detector is None:
                    detector = ShotClient(account_id=settings.analysis_fc_account_id, region=settings.oss_region,
                        function_name=settings.analysis_fc_function_name, credentials_provider=store.credentials_provider)
                advance_analysis(progress, spec=spec, tenant=tenant, operation_id=operation_id,
                    request_id=request_id, store=store, ice=ice, detector=detector,
                    settings=settings, save=save)
        except LeaseLost:
            pass
        except (ICEAPIError, ShotServiceError) as exc:
            # Only read-only probe/poll calls reach this branch; billable
            # submissions handle their own explicit fence below.
            save(progress, error_code=exc.code, **({} if exc.retryable else {"status": "failed"}))
        except ObjectStoreError as exc:
            save(progress, status="failed", error_code=exc.code)
        except Exception:
            save(progress, status="failed", error_code="analysis_result_unconfirmed")
        finally:
            with sessions.begin() as db:
                db.execute(update(AnalysisOperation).where(AnalysisOperation.id == operation_id,
                    AnalysisOperation.lease_token == lease).values(lease_token=None, lease_expires_at=None))
        with sessions() as db:
            return response(db.get(AnalysisOperation, operation_id))


def advance_analysis(data, *, spec, tenant, operation_id, request_id, store, ice, detector, settings, save):
    source, source_key = spec["source_uri"], store.key_from_uri(spec["source_uri"], tenant)
    if "probe" not in data:
        probe = ice.media_info(source)
        facts = probe["facts"]
        if facts["duration_seconds"] > settings.analysis_max_duration_seconds or facts["size_bytes"] > settings.max_upload_bytes:
            raise ObjectStoreError("analysis_trial_input_limit", "Media exceeds the configured analysis trial limits", 413)
        data["probe"] = probe
        data["observed"] = store.observe(key=source_key, expected_size=facts["size_bytes"], expected_hash=spec["sha256"])
        data["shots"] = {"status": "pending" if spec["shots"] and facts.get("video") else "not_requested"}
        data["asr"] = {"status": "pending" if spec["transcribe"] and facts.get("audio") else "not_requested"}
        if spec["transcribe"] and not facts.get("audio"):
            data["asr"] = {"status": "unavailable", "failure_code": "source_has_no_audio"}
        save(data)
        return
    facts, observed = data["probe"]["facts"], data["observed"]
    duration = facts["duration_seconds"]
    shots, asr = data["shots"], data["asr"]
    if shots["status"] == "pending":
        key = f"tenants/{tenant}/analyses/{operation_id}/shot-result.json"
        source_grant = store.access(key=source_key, ttl_seconds=900, purpose="preview")
        result_grant = store.result_grant(key=key, ttl_seconds=900)
        shots.update(status="submitting", job_id="shots_" + operation_id, result_key=key)
        save(data)  # Commit task/result identity before the potentially billable call.
        try:
            shots.update(detector.submit_shots(operation_id=operation_id, event={"operation_id": operation_id,
                "tenant_id": tenant, "source_url": source_grant["url"], "source_sha256": observed["sha256"],
                "source_size_bytes": observed["size_bytes"], "duration_seconds": duration,
                "result_upload": result_grant}), status="running")
        except ShotServiceError as exc:
            # Task ID is known before submission; query it even after an
            # uncertain response. No automatic paid re-submission is needed.
            shots.update(status="failed" if exc.submission_rejected else "running", failure_code=exc.code)
        save(data)
        return
    if shots["status"] == "submitting":
        shots.update(status="running", failure_code="shot_submission_outcome_unknown")
        save(data)
        return
    if shots["status"] == "running":
        result = detector.shot_status(shots["job_id"])
        shots["request_id"] = result["request_id"]
        if result["status"] == "succeeded":
            document = store.read_json(key=shots["result_key"], max_bytes=1024 * 1024)
            shots.update(status="ok", elements=normalize_shots(document, duration=duration,
                operation_id=operation_id, source_sha256=observed["sha256"]), duration_ms=result.get("duration_ms"))
            measurements = document.get("measurements") or {}
            shots["detector_evidence"] = {key: measurements[key] for key in (
                "decoded_frames", "container_duration_seconds", "pts_origin_seconds", "runtime", "pyav_version", "numpy_version", "detector_profile")
                if key in measurements}
            shots.pop("failure_code", None)
        elif result["status"] == "failed":
            shots.update(status="failed", failure_code="shot_cloud_job_failed")
        save(data)
        return
    if asr["status"] == "pending":
        asr["status"] = "submitting"
        save(data)
        try:
            asr.update(ice.submit_asr(object_uri=source, operation_id=operation_id), status="running")
        except ICEAPIError as exc:
            asr.update(status="failed" if exc.submission_rejected else "unknown", failure_code=exc.code)
        save(data)
        return
    if asr["status"] == "submitting":
        asr.update(status="unknown", failure_code="asr_submission_outcome_unknown")
        save(data)
        return
    if asr["status"] == "running":
        result = ice.asr_status(asr["job_id"], object_uri=source, duration=duration)
        asr["request_id"] = result["request_id"]
        if result["status"] == "succeeded":
            asr.update(status="ok", segments=result["segments"])
        elif result["status"] == "failed":
            asr.update(status="failed", failure_code="asr_cloud_job_failed")
        save(data)
        return
    elements = shots.get("elements", [])
    thumbs = data.setdefault("thumbnails", {})
    if facts.get("video", {}).get("codec") in {"h264", "h265", "hevc"}:
        for index, shot in enumerate(elements[:settings.analysis_max_thumbnails]):
            if str(index) in thumbs:
                continue
            key = f"tenants/{tenant}/analyses/{operation_id}/shots/{index}.jpg"
            # Exact cloud snapshot inside the measured shot, never a fabricated boundary.
            time_ms = int((shot["start"] + shot["end"]) * 500)
            try:
                evidence = store.snapshot(source_key=source_key, target_key=key, time_ms=time_ms, source_etag=observed["etag"])
                thumbs[str(index)] = {"uri": store.object_uri(key), "time_ms": time_ms, **evidence}
            except Exception:
                thumbs[str(index)] = {"failure_code": "cloud_thumbnail_unavailable"}
            save(data)
            return
    def stage(row, provider, version):
        return {"status": "failed" if row["status"] == "unknown" else row["status"],
                "provider": provider, "version": version,
                **({"failure_code": row["failure_code"]} if row.get("failure_code") else {})}
    analysis = {"schema_version": "media-analysis/2", "provider": "aliyun-hosted-services", "source": {"uri": source},
        "media": {"status": "ok", "duration_seconds": duration, "size_bytes": facts["size_bytes"], "format": facts["format"]},
        "video": facts.get("video"), "audio": facts.get("audio"),
        "shots": [{**shot, **({"thumbnail_uri": thumbs[str(index)]["uri"]} if thumbs.get(str(index), {}).get("uri") else {})}
                  for index, shot in enumerate(elements)],
        "shot_analysis": stage(shots, "aliyun-fc-project-shot-detector", DETECTOR_VERSION),
        "transcription": {**stage(asr, "aliyun-ice-ASR", ICE_VERSION), "segments": asr.get("segments", [])},
        "warnings": (["thumbnails_incomplete"] if elements and sum(bool(v.get("uri")) for v in thumbs.values()) < len(elements) else [])}
    analysis = validate_hosted_analysis(analysis, source_uri=source)
    key = f"tenants/{tenant}/analyses/{operation_id}/analysis.json"
    evidence = store.put_verified(key=key, data=json.dumps(analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(), content_type="application/json")
    result = {"provider_job_id": operation_id, "request_id": request_id, "status": "succeeded",
        "artifact_uri": store.object_uri(key), "artifact_type": "media_analysis", "analysis": analysis,
        "model": {"provider": PROVIDER, "model_id": MODEL_ID, "version": MODEL_VERSION},
        "metrics": {"source_evidence": observed, "analysis_evidence": evidence,
                    "cloud_jobs": {"probe": data["probe"]["job_id"], "shots": shots.get("job_id"), "asr": asr.get("job_id")},
                    "detector_evidence": shots.get("detector_evidence", {}),
                    "thumbnail_evidence": thumbs},
        "usage": {"input_seconds": duration, "thumbnails": sum(bool(v.get("uri")) for v in thumbs.values())},
        "cost": calculated_analysis_cost(duration=duration, shots=shots["status"], asr=asr["status"])}
    save(data, status="succeeded", result=result, error_code=None)


def calculated_analysis_cost(*, duration, shots, asr):
    """Published processing tariff, separate from storage and the final bill."""
    components = []
    if asr == "ok":
        components.append({"service": "ICE-ASR", "amount": str(Decimal(str(duration)) * Decimal("0.0417") / 60),
            "source": "https://help.aliyun.com/zh/ims/smart-tasks"})
    result = {"currency": "CNY", "bill_confirmed": False, "components": components,
        "price_version": "aliyun-analysis-cn-20261018", "excludes": ["OSS_storage_requests_traffic", "OSS_snapshots"]}
    # FC is billed by resource usage and instance lifetime, not input video
    # duration. Keep the total unknown until the metered bill is reconciled.
    if shots != "not_requested" or asr in {"failed", "unknown"}:
        return {**result, "status": "unknown"}
    return {**result, "status": "calculated", "amount": str(sum((Decimal(item["amount"]) for item in components), Decimal(0)))}
