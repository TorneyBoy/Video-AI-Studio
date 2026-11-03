from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .deps import RequestIdentity, authenticate_request, ensure_project, ensure_tenant, reject_public_tenant_admin, require_internal_execution, require_tenant_access, require_tenant_id
from ..database import get_db
from .. import models, schemas
from ..config import get_settings
from timeline_engine import Timeline as CanonicalTimeline, TemplateDefinition
from timeline_engine import create_manifest, validate_timeline
from ..services.auto_edit import AutoEditPlanner, shot_features_from_assets
from ..services.analysis_contract import require_planning_analysis
from ..services.jobs import InvalidJobTransition, create_step, request_job_retry, transition_job, transition_step
from ..services.completion import ExecutionSuperseded, finish_job, lock_job
from ..services.capabilities import capability_catalog, require_available_capability
from ..services.render_features import require_render_support
from ..services.render_quotes import request_hash as render_request_hash, consume_quote
from ..services import preparation_quotes
from ..services.providers import ProviderExecutionError
from ..services.provider_registry import ProviderRegistryError
from ..services.observability import job_stats
from ..services.budgets import BudgetDataError, budget_snapshot, list_usage, preflight_job_budget, reconcile_usage_evidence, upsert_budget
from ..services.workflows import (
    WorkflowError,
    WorkflowLeaseLost,
    cancel_workflow,
    claim_workflow,
    create_workflow,
    heartbeat_workflow,
    retry_workflow,
    request_workflow_retry,
)
from ..services.provider_callbacks import ProviderCallbackError, apply_callback, verify_signature
from ..services.remote_uri import RemoteURIError, validate_presigned_upload_url, validate_remote_uri
from ..services.storage_attestation import (
    StorageAttestationError,
    attestation_expiry_seconds,
    verify_attestation,
)
from ..services.storage_ingestion import apply_upload_completion
from ..services.editing import (EditingError, assert_render_sources, digest as edit_digest,
                               document_for_tenant, revision_for_document, timeline_for_revision)

router = APIRouter()
settings = get_settings()


@router.get("/session", tags=["identity"])
def session_context(identity: RequestIdentity = Depends(authenticate_request)):
    """Expose verified identity context without accepting browser tenant claims."""
    return {"auth_mode": get_settings().auth_mode,
            "principal_id": identity.principal_id, "tenant_id": identity.tenant_id}

def _validate_remote_asset_uri(value: str, *, field: str = "uri") -> str:
    try:
        return validate_remote_uri(value, field=field)
    except RemoteURIError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "remote_uri_required", "field": field, "message": str(exc)},
        ) from exc


def _validate_presigned_url(value: str) -> str:
    try:
        return validate_presigned_upload_url(value)
    except RemoteURIError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "presigned_url_required", "field": "upload_url", "message": str(exc)},
        ) from exc


def _storage_metadata(
    *,
    status_value: str,
    size_bytes: int,
    sha256: str | None,
    object_uri: str,
    mode: str = "external",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    upload = {
        "mode": mode,
        "status": status_value,
        "size_bytes": size_bytes,
        "object_uri": object_uri,
    }
    if sha256:
        upload["sha256"] = sha256
    if extra:
        upload.update(extra)
    return {"storage": upload}


def _require_asset_ready(asset: models.Asset, *, role: str = "asset") -> None:
    """Reject provider work that references an unfinished external upload."""

    metadata = asset.metadata_json or {}
    upload = metadata.get("upload") or {}
    if upload.get("status") == "created" and str(asset.uri).startswith("upload://"):
        raise HTTPException(status_code=409, detail=f"{role} upload has not completed")
    storage = metadata.get("storage") or {}
    if storage.get("mode") == "external_presigned" and storage.get("status") != "ready":
        raise HTTPException(status_code=409, detail=f"{role} external upload has not completed")


def _attestation_record(attestation: schemas.RemoteUploadAttestation) -> dict[str, Any]:
    """Persist only the proof envelope needed for safe idempotent retries."""

    signature = attestation.signature.strip().lower()
    if not signature.startswith("sha256="):
        signature = f"sha256={signature}"
    return {
        "version": "storage-attestation-v1",
        "nonce": attestation.nonce,
        "expires_at_epoch": attestation_expiry_seconds(attestation.expires_at),
        "signature": signature,
    }


def _require_storage_attestation(
    *,
    attestation: schemas.RemoteUploadAttestation | None,
    asset_id: str,
    object_uri: str,
    size_bytes: int | None,
    sha256: str | None,
    session_expires_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Verify an external gateway proof when production has configured one."""

    secret = settings.storage_attestation_secret
    if not secret:
        return None
    if attestation is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "storage_attestation_required", "message": "storage gateway attestation is required"},
        )
    if size_bytes is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "attestation_size_required", "message": "size_bytes is required when attestation is enabled"},
        )
    try:
        expiry_epoch = attestation_expiry_seconds(attestation.expires_at)
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        if session_expires_at is not None and expiry_epoch > attestation_expiry_seconds(session_expires_at):
            raise StorageAttestationError("storage attestation outlives the upload session", code="attestation_expiry_mismatch")
        verify_attestation(
            secret,
            attestation.signature,
            asset_id=asset_id,
            object_uri=object_uri,
            size_bytes=size_bytes,
            sha256=sha256,
            expires_at=attestation.expires_at,
            nonce=attestation.nonce,
        )
        if expiry_epoch <= now_epoch:
            raise StorageAttestationError("storage attestation has expired", code="attestation_expired")
    except StorageAttestationError as exc:
        raise HTTPException(status_code=401, detail={"code": exc.code, "message": str(exc)}) from exc
    return _attestation_record(attestation)


def _same_storage_attestation(
    stored: dict[str, Any], attestation: schemas.RemoteUploadAttestation | None,
) -> bool:
    if attestation is None:
        return False
    record = _attestation_record(attestation)
    return all(record.get(key) == stored.get(key) for key in ("nonce", "expires_at_epoch", "signature"))


def commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Resource conflicts with an existing record") from exc


def _resolve_job_idempotency_key(header: str | None) -> str | None:
    """Normalize the optional key used by long-running job creation routes."""
    if header is None:
        return None
    key = header.strip()
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key cannot be empty")
    return key


def _existing_job_for_key(
    db: Session,
    tenant_id: str,
    key: str | None,
    expected_type: str | None = None,
) -> models.Job | None:
    if not key:
        return None
    existing = db.scalar(select(models.Job).where(models.Job.tenant_id == tenant_id, models.Job.idempotency_key == key))
    if existing is not None and expected_type is not None:
        matches = existing.type == expected_type or existing.type.startswith(expected_type)
        if not matches:
            raise HTTPException(status_code=409, detail="Idempotency-Key is already used by another job type")
    return existing


def _persist_job(db: Session, row: models.Job, key: str | None, expected_type: str | None = None) -> models.Job:
    db.add(row)
    try:
        db.flush()
        _preflight_job_budget(db, row)
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        existing = _existing_job_for_key(db, row.tenant_id, key, expected_type)
        if existing is not None:
            return existing
        raise HTTPException(status_code=409, detail="Resource conflicts with an existing record")
    return row


def _preflight_job_budget(db: Session, row: models.Job) -> None:
    """Pin production providers and reserve the server-owned price envelope."""
    try:
        selected = None
        render_timeline = ((row.payload or {}).get("manifest") or {}).get("source", {}).get("timeline") if row.type.startswith("render.") else None
        pinned = (row.payload or {}).get("capability_snapshot") or {}
        remote_operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == row.id)) if render_timeline else None
        recovering_render = bool(render_timeline and ((row.payload or {}).get("provider_job_id") or (remote_operation and remote_operation.external_job_id)))
        if recovering_render:
            selected = pinned or None
        elif render_timeline:
            selected = require_render_support(render_timeline, require_price=bool((row.payload or {}).get("quote_id")), provider=pinned.get("provider"), model_id=pinned.get("model_id"), model_version=pinned.get("model_version"))
            if (row.payload or {}).get("quote_id") and selected != pinned:
                raise EditingError("render_quote_changed", "服务报价已变化，请重新获取报价后再提交", 409)
        if settings.app_environment == "production" and not recovering_render:
            request = (row.payload or {}).get("provider_request") or {}
            capability = request.get("capability")
            if row.type == "asset.analyze":
                capability = "media_analysis"
            elif row.type.startswith("render."):
                capability = "video_editing"
            if capability:
                selected = selected or require_available_capability(capability, request.get("provider"),
                                                                    request.get("model_id"), request.get("model_version"))
                row.payload = {**(row.payload or {}), "capability_snapshot": selected}
        preflight_job_budget(db, row, selected_capability=selected)
    except EditingError:
        db.rollback()
        raise
    except BudgetDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        ) from exc
    except ProviderExecutionError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc


def _budget_read(db: Session, row: models.TenantBudget) -> schemas.TenantBudgetRead:
    snapshot = budget_snapshot(db, row, row.tenant_id)
    return schemas.TenantBudgetRead.model_validate({
        **row.__dict__,
        **snapshot,
    })


@router.post("/tenants", response_model=schemas.TenantRead, status_code=status.HTTP_201_CREATED, tags=["tenants"])
def create_tenant(
    payload: schemas.TenantCreate,
    _: object = Depends(reject_public_tenant_admin),
    db: Session = Depends(get_db),
):
    row = models.Tenant(slug=payload.slug, name=payload.name, settings=payload.settings)
    db.add(row); commit_or_conflict(db); db.refresh(row)
    return row


@router.get("/capabilities", tags=["capabilities"])
def get_capabilities(tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id)
    try:
        return capability_catalog()
    except (ProviderRegistryError, ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail={"code": "capability_configuration_unavailable"}) from exc


@router.get("/tenants", response_model=list[schemas.TenantRead], tags=["tenants"])
def list_tenants(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    _: object = Depends(reject_public_tenant_admin),
    db: Session = Depends(get_db),
):
    return list(db.scalars(select(models.Tenant).order_by(models.Tenant.created_at).offset(skip).limit(limit)))


@router.get("/tenants/{tenant_id}", response_model=schemas.TenantRead, tags=["tenants"])
def get_tenant(tenant_id: str, _: object = Depends(require_tenant_access), db: Session = Depends(get_db)):
    return ensure_tenant(db, tenant_id)


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["tenants"], dependencies=[Depends(require_internal_execution)])
def delete_tenant(tenant_id: str, _: object = Depends(require_tenant_access), db: Session = Depends(get_db)):
    row = ensure_tenant(db, tenant_id); db.delete(row); commit_or_conflict(db)


@router.get("/tenants/{tenant_id}/budget", response_model=schemas.TenantBudgetRead, tags=["budgets"])
def get_tenant_budget(tenant_id: str, _: object = Depends(require_tenant_access), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id)
    row = db.scalar(select(models.TenantBudget).where(models.TenantBudget.tenant_id == tenant_id))
    if row is None:
        # An absent policy is an unlimited tenant. Return a stable read shape
        # without creating a policy row as a side effect of GET.
        now = datetime.now(timezone.utc)
        return {
            "id": "unconfigured",
            "tenant_id": tenant_id,
            "currency": "USD",
            "monthly_limit_micros": None,
            "task_limit_micros": None,
            "enforcement": "block",
            "enabled": False,
            "period_key": now.strftime("%Y-%m"),
            "settled_micros": 0,
            "reserved_micros": 0,
            "remaining_micros": None,
            "created_at": now,
            "updated_at": now,
        }
    return _budget_read(db, row)


@router.put("/tenants/{tenant_id}/budget", response_model=schemas.TenantBudgetRead, tags=["budgets"], dependencies=[Depends(require_internal_execution)])
def put_tenant_budget(
    tenant_id: str,
    payload: schemas.TenantBudgetUpsert,
    _: object = Depends(require_tenant_access),
    db: Session = Depends(get_db),
):
    ensure_tenant(db, tenant_id)
    row = upsert_budget(
        db,
        tenant_id,
        currency=payload.currency,
        monthly_limit_micros=payload.monthly_limit_micros,
        task_limit_micros=payload.task_limit_micros,
        enforcement=payload.enforcement,
        enabled=payload.enabled,
    )
    db.commit()
    db.refresh(row)
    return _budget_read(db, row)


@router.get("/billing/usage", response_model=list[schemas.UsageLedgerRead], tags=["budgets", "billing"])
def get_usage_ledger(
    limit: int = Query(100, ge=1, le=500),
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    ensure_tenant(db, tenant_id)
    return list_usage(db, tenant_id, limit=limit)


@router.post("/projects", response_model=schemas.ProjectRead, status_code=201, tags=["projects"])
def create_project(payload: schemas.ProjectCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id)
    row = models.Project(tenant_id=tenant_id, name=payload.name, description=payload.description, status=payload.status, metadata_json=payload.metadata)
    db.add(row); commit_or_conflict(db); db.refresh(row); return row


@router.get("/projects", response_model=list[schemas.ProjectRead], tags=["projects"])
def list_projects(skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id)
    return list(db.scalars(select(models.Project).where(models.Project.tenant_id == tenant_id).order_by(models.Project.created_at).offset(skip).limit(limit)))


@router.get("/projects/{project_id}", response_model=schemas.ProjectRead, tags=["projects"])
def get_project(project_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return ensure_project(db, project_id, tenant_id)


@router.delete("/projects/{project_id}", status_code=204, tags=["projects"])
def delete_project(project_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = ensure_project(db, project_id, tenant_id)
    if db.scalar(select(models.EditDocument.id).where(models.EditDocument.project_id == project_id,
                                                      models.EditDocument.tenant_id == tenant_id).limit(1)):
        raise EditingError("project_has_edit_history", "项目包含需要保留的编辑历史，当前不能直接删除")
    db.delete(row); commit_or_conflict(db)


def _check_optional_project(db: Session, project_id: str | None, tenant_id: str) -> None:
    if project_id is not None:
        ensure_project(db, project_id, tenant_id)


@router.post("/assets", response_model=schemas.AssetRead, status_code=201, tags=["assets"])
def create_asset(payload: schemas.AssetCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); _check_optional_project(db, payload.project_id, tenant_id)
    if settings.asset_storage_mode == "external":
        raise HTTPException(status_code=409, detail={"code": "verified_remote_asset_required", "message": "Use a verified remote upload or registration"})
    row = models.Asset(tenant_id=tenant_id, project_id=payload.project_id, name=payload.name, kind=payload.kind, uri=payload.uri, metadata_json=payload.metadata)
    db.add(row); db.commit(); db.refresh(row); return row


@router.post("/assets/register-remote", response_model=schemas.AssetRead, status_code=201, tags=["assets"])
def register_remote_asset(
    payload: schemas.RemoteAssetRegister,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    """Register an object that was uploaded directly to external storage.

    This endpoint only records a stable URI and client/storage attestation. It
    never downloads, probes, transcodes, or otherwise computes on the media.
    """
    if settings.asset_storage_mode == "local":
        raise HTTPException(status_code=409, detail="External asset registration is disabled by ASSET_STORAGE_MODE=local")
    ensure_tenant(db, tenant_id)
    _check_optional_project(db, payload.project_id, tenant_id)
    uri = _validate_remote_asset_uri(payload.uri)
    if payload.size_bytes is not None and payload.size_bytes > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds configured size limit")
    if settings.storage_attestation_secret and not payload.asset_id:
        raise HTTPException(
            status_code=422,
            detail={"code": "attestation_asset_id_required", "message": "asset_id is required when storage attestation is enabled"},
        )
    asset_id = payload.asset_id or models.new_id()
    attestation = _require_storage_attestation(
        attestation=payload.attestation,
        asset_id=asset_id,
        object_uri=uri,
        size_bytes=payload.size_bytes,
        sha256=payload.sha256,
    )
    metadata = dict(payload.metadata)
    storage = _storage_metadata(
        status_value="ready",
        size_bytes=payload.size_bytes or 0,
        sha256=payload.sha256,
        object_uri=uri,
        mode="external_registered",
    )["storage"]
    if attestation:
        storage["attestation"] = attestation
    metadata["storage"] = storage
    row = models.Asset(
        id=asset_id,
        tenant_id=tenant_id,
        project_id=payload.project_id,
        name=payload.name,
        kind=payload.kind,
        uri=uri,
        metadata_json=metadata,
    )
    db.add(row)
    commit_or_conflict(db)
    db.refresh(row)
    return row


@router.post("/assets/remote-upload-session", response_model=schemas.RemoteUploadSessionRead, status_code=201, tags=["assets"])
def create_remote_upload_session(
    payload: schemas.RemoteUploadSessionCreate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    """Create a direct-to-object-storage upload intent.

    A storage gateway (or the client using its own storage credentials) must
    supply ``upload_url``. The API persists no presigned URL and never proxies
    the bytes. ``register_only`` records an already uploaded object directly.
    """
    if settings.asset_storage_mode == "local":
        raise HTTPException(status_code=409, detail="External upload is disabled by ASSET_STORAGE_MODE=local")
    ensure_tenant(db, tenant_id)
    _check_optional_project(db, payload.project_id, tenant_id)
    if payload.size_bytes > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds configured size limit")
    object_uri = _validate_remote_asset_uri(payload.object_uri, field="object_uri")
    upload_url = None
    if not payload.register_only:
        if not payload.upload_url:
            raise HTTPException(status_code=422, detail={"code": "upload_url_required", "message": "upload_url is required unless register_only=true"})
        upload_url = _validate_presigned_url(payload.upload_url)
    elif payload.upload_url:
        upload_url = _validate_presigned_url(payload.upload_url)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(60, settings.asset_upload_session_ttl_seconds))
    ready = payload.register_only
    if ready and settings.storage_attestation_secret and not payload.asset_id:
        raise HTTPException(
            status_code=422,
            detail={"code": "attestation_asset_id_required", "message": "asset_id is required for attested register_only"},
        )
    asset_id = payload.asset_id or models.new_id()
    # A pending session cannot be attested until the gateway has observed the
    # uploaded object. The proof is required at remote-upload-complete.
    attestation = (
        _require_storage_attestation(
            attestation=payload.attestation,
            asset_id=asset_id,
            object_uri=object_uri,
            size_bytes=payload.size_bytes,
            sha256=payload.sha256,
            session_expires_at=expires_at,
        )
        if ready
        else None
    )
    metadata = dict(payload.metadata)
    storage = _storage_metadata(
        status_value="ready" if ready else "created",
        size_bytes=payload.size_bytes,
        sha256=payload.sha256,
        object_uri=object_uri,
        mode="external_registered" if ready else "external_presigned",
        extra={"expires_at": expires_at.isoformat()},
    )["storage"]
    if attestation:
        storage["attestation"] = attestation
    metadata["storage"] = storage
    row = models.Asset(
        id=asset_id,
        tenant_id=tenant_id,
        project_id=payload.project_id,
        name=payload.name,
        kind=payload.content_type,
        uri=object_uri,
        metadata_json=metadata,
    )
    db.add(row)
    commit_or_conflict(db)
    db.refresh(row)
    return schemas.RemoteUploadSessionRead(
        upload_id=f"upl_{row.id}",
        asset_id=row.id,
        object_uri=object_uri,
        upload_url=upload_url,
        upload_method=payload.upload_method if not payload.register_only else None,
        upload_headers=dict(payload.upload_headers) if not ready else {},
        complete_uri=None if ready else f"{settings.api_prefix}/assets/{row.id}/remote-upload-complete",
        expires_at=expires_at,
        status="ready" if ready else "created",
    )


@router.post("/assets/upload-session", response_model=schemas.UploadSessionRead, status_code=201, tags=["assets"])
def create_upload_session(payload: schemas.UploadSessionCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    """Create a resumable-upload placeholder.

    Object-storage multipart upload will replace this local placeholder. The
    asset is created before bytes arrive so every later job can refer to a
    stable asset id and tenant-scoped URI.
    """
    if settings.asset_storage_mode == "external":
        raise HTTPException(status_code=409, detail={"code": "external_upload_required"})
    ensure_tenant(db, tenant_id)
    _check_optional_project(db, payload.project_id, tenant_id)
    if payload.size_bytes > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds configured size limit")
    row = models.Asset(
        tenant_id=tenant_id,
        project_id=payload.project_id,
        name=payload.name,
        kind=payload.content_type,
        uri=f"upload://pending/{uuid4().hex}",
        metadata_json={"upload": {"size_bytes": payload.size_bytes, "sha256": payload.sha256, "status": "created"}},
    )
    db.add(row); db.commit(); db.refresh(row)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return schemas.UploadSessionRead(
        upload_id=f"upl_{row.id}",
        asset_id=row.id,
        upload_uri=f"{settings.api_prefix}/assets/{row.id}/content",
        expires_at=expires_at,
    )


@router.post("/assets/{asset_id}/content", response_model=schemas.AssetRead, tags=["assets"])
def upload_asset_content(
    asset_id: str,
    file: UploadFile = File(...),
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    """Store a development upload without buffering the whole file in memory.

    Production clients upload directly to storage, then complete a verified
    remote session. This route cannot mutate an existing remote asset.
    """
    if settings.asset_storage_mode == "external":
        raise HTTPException(status_code=409, detail={"code": "external_upload_required"})
    row = db.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not row.uri.startswith("upload://pending/") or (row.metadata_json.get("upload") or {}).get("status") != "created":
        raise HTTPException(status_code=409, detail={"code": "asset_content_immutable"})
    root = Path(settings.storage_root) / tenant_id / asset_id
    root.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or row.name).name
    destination = root / safe_name
    total = 0
    with destination.open("wb") as output:
        while chunk := file.file.read(1024 * 1024):
            total += len(chunk)
            if total > settings.max_upload_bytes:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Upload exceeds configured size limit")
            output.write(chunk)
    row.uri = str(destination)
    metadata = dict(row.metadata_json or {})
    metadata["upload"] = {**metadata.get("upload", {}), "status": "ready", "bytes": total, "content_type": file.content_type}
    row.metadata_json = metadata
    db.commit(); db.refresh(row)
    return row


@router.post("/assets/{asset_id}/remote-upload-complete", response_model=schemas.AssetRead, tags=["assets"])
def complete_remote_upload(
    asset_id: str,
    payload: schemas.RemoteUploadComplete,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    """Commit an external storage gateway's upload attestation.

    The control plane validates the declared size and optional SHA-256 against
    the upload intent. It deliberately does not issue a HEAD request or fetch
    any bytes; object existence and integrity are the storage gateway's
    responsibility.
    """
    row = db.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    apply_upload_completion(db, row, payload, settings)
    db.commit()
    db.refresh(row)
    return row



@router.get("/assets", response_model=list[schemas.AssetRead], tags=["assets"])
def list_assets(project_id: str | None = None, skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                q: str | None = Query(default=None, max_length=200), tag: str | None = Query(default=None, max_length=64),
                media_type: str | None = Query(default=None, pattern="^(video|image|audio)$"),
                tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); stmt = select(models.Asset).where(models.Asset.tenant_id == tenant_id)
    if project_id: stmt = stmt.where(models.Asset.project_id == project_id)
    if q and q.strip(): stmt = stmt.where(func.lower(models.Asset.name).contains(q.strip().lower(), autoescape=True))
    if media_type: stmt = stmt.where((models.Asset.kind == media_type) | models.Asset.kind.startswith(media_type + "/"))
    if tag and tag.strip():
        stmt = stmt.join(models.AssetLabel, models.AssetLabel.asset_id == models.Asset.id).where(
            models.AssetLabel.tenant_id == tenant_id, models.AssetLabel.tag_key == tag.strip().casefold())
    return list(db.scalars(stmt.order_by(models.Asset.created_at).offset(skip).limit(limit)))


@router.post("/assets/{asset_id}/analyze", response_model=schemas.JobRead, status_code=201, tags=["assets", "jobs"])
def create_asset_analysis_job(
    asset_id: str,
    payload: schemas.AssetAnalyzeCreate | None = None,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Queue external media analysis for an asset.

    Analysis is asynchronous so uploaded and remote assets use the same Job
    contract. In production the Worker forwards a remotely reachable URI to
    the configured HTTP Provider and stores the returned compact structure.
    """
    asset = db.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    storage = (asset.metadata_json or {}).get("storage") or {}
    if storage.get("mode") == "external_presigned" and storage.get("status") != "ready":
        raise HTTPException(status_code=409, detail="Asset upload has not completed")
    payload = payload or schemas.AssetAnalyzeCreate()
    if idempotency_header and payload.idempotency_key and idempotency_header != payload.idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header does not match payload idempotency_key")
    key = _resolve_job_idempotency_key(idempotency_header) or payload.idempotency_key
    if payload.quote_id or get_settings().auth_mode != "development_header":
        if not payload.quote_id or not key:
            raise EditingError("analysis_quote_required", "请先取得并确认素材分析报价", 422)
        quote = preparation_quotes.consume_quote(db, tenant_id, payload.quote_id, key,
            expected_kind="analysis", expected_request=preparation_quotes.analysis_request(asset_id, payload))
        result = preparation_quotes.public_result(db, quote)
        job_id = next((item["job_id"] for item in result["items"] if item["asset_id"] == asset_id), None)
        if not job_id:
            raise EditingError("analysis_already_available", "已有分析可用，请使用综合报价确认接口查看结果", 409)
        job = db.get(models.Job, job_id)
        db.commit()
        return job
    if key:
        existing = _existing_job_for_key(db, tenant_id, key, "asset.analyze")
        if existing is not None:
            if (existing.payload or {}).get("asset_id") != asset_id:
                raise EditingError("idempotency_input_conflict", "同一操作标识已经用于不同素材", 409)
            return existing
    options = payload.model_dump(exclude={"idempotency_key", "max_attempts", "quote_id", "force_new"})
    # Preserve a free-form options map while keeping the top-level fields easy
    # for workers and clients to inspect.
    nested = options.pop("options", {}) or {}
    options.update(nested)
    row = models.Job(
        tenant_id=tenant_id,
        project_id=asset.project_id,
        type="asset.analyze",
        status="queued",
        idempotency_key=key,
        max_attempts=payload.max_attempts,
        payload={"asset_id": asset.id, "uri": asset.uri, "options": options},
    )
    return _persist_job(db, row, key, "asset.analyze")


@router.get("/assets/{asset_id}", response_model=schemas.AssetRead, tags=["assets"])
def get_asset(asset_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Asset not found")
    return row


@router.delete("/assets/{asset_id}", status_code=204, tags=["assets"])
def delete_asset(asset_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Asset not found")
    if db.scalar(select(models.ReferenceAnalysis.id).where(models.ReferenceAnalysis.asset_id == asset_id).limit(1)):
        raise HTTPException(409, detail={"code": "asset_reference_in_use", "message": "此素材被参考分析引用，请保留其来源记录"})
    db.delete(row); db.commit()


@router.post("/templates", response_model=schemas.TemplateRead, status_code=201, tags=["templates"])
def create_template(payload: schemas.TemplateCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); _check_optional_project(db, payload.project_id, tenant_id)
    row = models.Template(tenant_id=tenant_id, project_id=payload.project_id, name=payload.name, version=payload.version, content=payload.content)
    db.add(row); db.commit(); db.refresh(row); return row


@router.get("/templates", response_model=list[schemas.TemplateRead], tags=["templates"])
def list_templates(project_id: str | None = None, skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); stmt = select(models.Template).where(models.Template.tenant_id == tenant_id)
    if project_id: stmt = stmt.where(models.Template.project_id == project_id)
    return list(db.scalars(stmt.order_by(models.Template.created_at).offset(skip).limit(limit)))


@router.get("/templates/{template_id}", response_model=schemas.TemplateRead, tags=["templates"])
def get_template(template_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Template).where(models.Template.id == template_id, models.Template.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Template not found")
    return row


@router.delete("/templates/{template_id}", status_code=204, tags=["templates"])
def delete_template(template_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Template).where(models.Template.id == template_id, models.Template.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Template not found")
    db.delete(row); db.commit()


@router.post("/timelines", response_model=schemas.TimelineRead, status_code=201, tags=["timelines"])
def create_timeline(payload: schemas.TimelineCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); ensure_project(db, payload.project_id, tenant_id)
    row = models.Timeline(tenant_id=tenant_id, project_id=payload.project_id, name=payload.name, fps=payload.fps, duration_ms=payload.duration_ms, timeline_json=payload.timeline)
    db.add(row); db.commit(); db.refresh(row); return row


def _canonical_timeline(row: models.Timeline) -> CanonicalTimeline:
    try:
        return CanonicalTimeline.model_validate(row.timeline_json)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_timeline", "errors": exc.errors()}) from exc


@router.post("/timelines/{timeline_id}/validate", response_model=schemas.TimelineValidationRead, tags=["timelines"])
def validate_timeline_endpoint(timeline_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Timeline).where(models.Timeline.id == timeline_id, models.Timeline.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    try:
        canonical = CanonicalTimeline.model_validate(row.timeline_json)
    except ValidationError as exc:
        issues = [{"code": "schema_error", "message": error["msg"], "path": ".".join(str(x) for x in error["loc"]), "severity": "error"} for error in exc.errors()]
        return {"valid": False, "errors": len(issues), "warnings": 0, "issues": issues}
    return validate_timeline(canonical).as_dict()


def _render_timeline_job(
    db: Session,
    row: models.Timeline,
    payload: schemas.RenderRequest,
    *,
    tenant_id: str,
    key: str | None,
) -> dict[str, Any]:
    """Create an idempotent render job for a canonical timeline."""
    frozen_row = db.scalar(select(models.TimelineRevision).where(models.TimelineRevision.timeline_id == row.id,
                                                                models.TimelineRevision.tenant_id == tenant_id))
    binding = {}
    if frozen_row is not None:
        document = document_for_tenant(db, tenant_id, frozen_row.document_id)
        revision = revision_for_document(db, document, frozen_row.id)
        frozen_row = timeline_for_revision(db, revision)
        binding = {"edit_document_id": document.id, "plan_revision_id": revision.id,
                   "timeline_revision_id": revision.id, "plan_hash": revision.content_hash, "timeline_hash": frozen_row.content_hash}
    options = {**payload.options, "quality": payload.quality}
    expected_timeline_id = frozen_row.timeline_id if frozen_row else str((row.timeline_json or {}).get("id") or row.id)
    request_hash = render_request_hash(row.id, binding, payload)
    def check_replay(existing):
        saved = existing.payload or {}; manifest = saved.get("manifest") or {}
        try: current_timeline = CanonicalTimeline.model_validate(frozen_row.timeline_json if frozen_row else row.timeline_json).model_dump(mode="json")
        except ValueError as exc: raise EditingError("idempotency_input_conflict", "原操作对应的时间线内容已改变，请核对原任务", 409) from exc
        if ((saved.get("render_request_hash") and saved["render_request_hash"] != request_hash)
                or manifest.get("timeline_id") != expected_timeline_id or manifest.get("output") != payload.output
                or manifest.get("options") != options or (manifest.get("source") or {}).get("timeline") != current_timeline):
            raise EditingError("idempotency_input_conflict", "同一操作标识已经用于不同版本或导出设置")
        return {"job": schemas.JobRead.model_validate(existing), "manifest": manifest}
    existing = _existing_job_for_key(db, tenant_id, key, "render.")
    if existing is not None:
        return check_replay(existing)
    if frozen_row is not None:
        # Serialize admission with selected-version batch rendering. Replays
        # above remain valid without renewing a price or reservation.
        db.execute(select(models.EditDocument.id).where(models.EditDocument.id==document.id,
            models.EditDocument.tenant_id==tenant_id).with_for_update())
        existing = _existing_job_for_key(db, tenant_id, key, "render.")
        if existing is not None:
            return check_replay(existing)
        assert_render_sources(db, document, revision)
        template = TemplateDefinition.model_validate(revision.template_json)
        counts = {}
        for item in revision.plan_json.get("selected", []):
            counts.setdefault(item["slot"], set()).add(item["asset_id"])
        missing = [slot.name for slot in template.slots if slot.required and len(counts.get(slot.name, set())) < max(1, slot.constraints.min_items)]
        if missing:
            raise EditingError("edit_required_slots_missing", "必需镜头尚未补齐，请补充素材并调整计划后再渲染", 422,
                               {"missing_slots": missing})
    canonical = CanonicalTimeline.model_validate(frozen_row.timeline_json) if frozen_row else _canonical_timeline(row)
    report = validate_timeline(canonical)
    if not report.valid:
        raise HTTPException(status_code=422, detail=report.as_dict())
    chosen_capability = require_render_support(canonical.model_dump(mode="json"), require_price=bool(payload.quote_id))
    manifest = create_manifest(canonical, template=TemplateDefinition.model_validate(revision.template_json) if frozen_row else None,
                               output=payload.output, options=options)
    quote_id = consume_quote(db, row, tenant_id, payload, key, request_hash, chosen_capability, canonical.model_dump(mode="json"))
    job = models.Job(
        tenant_id=tenant_id,
        project_id=row.project_id,
        type=f"render.{payload.quality}",
        status="queued",
        idempotency_key=key,
        max_attempts=payload.max_attempts,
        payload={"manifest": manifest.model_dump(mode="json"), "manifest_id": manifest.manifest_id,
                 "render_request_hash": request_hash, **binding, **({"quote_id": quote_id} if quote_id else {}),
                 **({"capability_snapshot": chosen_capability} if chosen_capability else {})},
    )
    job = _persist_job(db, job, key, "render.")
    return check_replay(job)


@router.post("/timelines/{timeline_id}/render", status_code=201, tags=["timelines"])
def render_timeline(
    timeline_id: str,
    payload: schemas.RenderRequest,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    row = db.scalar(select(models.Timeline).where(models.Timeline.id == timeline_id, models.Timeline.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    return _render_timeline_job(
        db,
        row,
        payload,
        tenant_id=tenant_id,
        key=_resolve_job_idempotency_key(idempotency_header),
    )


def _render_source_job(
    db: Session,
    job_id: str,
    payload: schemas.RenderRequest,
    *,
    tenant_id: str,
    key: str | None,
) -> dict[str, Any]:
    job = db.scalar(select(models.Job).where(models.Job.id == job_id, models.Job.tenant_id == tenant_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.type != "auto_edit.plan":
        raise HTTPException(status_code=409, detail="Preview/export requires an auto-edit plan job")
    timeline_id = str((job.payload or {}).get("timeline_id") or "")
    if not timeline_id:
        raise HTTPException(status_code=409, detail="Auto-edit job has not produced a timeline yet")
    row = db.scalar(select(models.Timeline).where(models.Timeline.id == timeline_id, models.Timeline.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=409, detail="Auto-edit timeline is not available")
    return _render_timeline_job(db, row, payload, tenant_id=tenant_id, key=key)


@router.get("/jobs/{job_id}/timeline", response_model=schemas.TimelineRead, tags=["jobs", "timelines"])
def get_job_timeline(job_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    """Return the canonical Timeline compiled by an auto-edit job."""
    job = db.scalar(select(models.Job).where(models.Job.id == job_id, models.Job.tenant_id == tenant_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    timeline_id = str((job.payload or {}).get("timeline_id") or "")
    if not timeline_id:
        raise HTTPException(status_code=409, detail="Auto-edit job has not produced a timeline yet")
    row = db.scalar(select(models.Timeline).where(models.Timeline.id == timeline_id, models.Timeline.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    return row


@router.post("/jobs/{job_id}/preview", status_code=201, tags=["jobs", "rendering"])
def preview_auto_edit_job(
    job_id: str,
    payload: schemas.RenderRequest | None = None,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Queue a draft render for a completed auto-edit plan."""
    request = payload or schemas.RenderRequest(quality="draft")
    return _render_source_job(
        db,
        job_id,
        request,
        tenant_id=tenant_id,
        key=_resolve_job_idempotency_key(idempotency_header),
    )


@router.post("/jobs/{job_id}/export", status_code=201, tags=["jobs", "rendering"])
def export_auto_edit_job(
    job_id: str,
    payload: schemas.RenderRequest | None = None,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Queue a final render for a completed auto-edit plan."""
    request = payload or schemas.RenderRequest(quality="standard")
    return _render_source_job(
        db,
        job_id,
        request,
        tenant_id=tenant_id,
        key=_resolve_job_idempotency_key(idempotency_header),
    )


@router.get("/timelines", response_model=list[schemas.TimelineRead], tags=["timelines"])
def list_timelines(project_id: str | None = None, skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); stmt = select(models.Timeline).where(models.Timeline.tenant_id == tenant_id)
    if project_id: stmt = stmt.where(models.Timeline.project_id == project_id)
    return list(db.scalars(stmt.order_by(models.Timeline.created_at).offset(skip).limit(limit)))


@router.get("/timelines/{timeline_id}", response_model=schemas.TimelineRead, tags=["timelines"])
def get_timeline(timeline_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Timeline).where(models.Timeline.id == timeline_id, models.Timeline.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Timeline not found")
    return row


@router.delete("/timelines/{timeline_id}", status_code=204, tags=["timelines"])
def delete_timeline(timeline_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Timeline).where(models.Timeline.id == timeline_id, models.Timeline.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Timeline not found")
    if db.scalar(select(models.TimelineRevision.id).where(models.TimelineRevision.timeline_id == row.id,
                                                          models.TimelineRevision.tenant_id == tenant_id)):
        raise EditingError("timeline_revision_immutable", "已保存的编辑版本不能原地删除，请创建或恢复新版本")
    db.delete(row); db.commit()

@router.post("/jobs", response_model=schemas.JobRead, status_code=201, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
def create_job(
    payload: schemas.JobCreate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    ensure_tenant(db, tenant_id)
    if idempotency_header and payload.idempotency_key and idempotency_header != payload.idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header does not match payload idempotency_key")
    key = _resolve_job_idempotency_key(idempotency_header) or payload.idempotency_key
    if key:
        existing = _existing_job_for_key(db, tenant_id, key, payload.type)
        if existing is not None:
            return existing
    _check_optional_project(db, payload.project_id, tenant_id)
    row = models.Job(tenant_id=tenant_id, project_id=payload.project_id, type=payload.type, status=payload.status, idempotency_key=key, max_attempts=payload.max_attempts, payload=payload.payload)
    db.add(row)
    try:
        db.flush()
        _preflight_job_budget(db, row)
        for item in payload.steps:
            create_step(db, row, name=item.name, sequence=item.sequence, status=item.status, progress=item.progress, payload=item.payload)
        db.commit(); db.refresh(row)
    except IntegrityError:
        db.rollback()
        if key:
            existing = _existing_job_for_key(db, tenant_id, key, payload.type)
            if existing is not None:
                return existing
        raise HTTPException(status_code=409, detail="Resource conflicts with an existing record")
    return row


def _check_auto_edit_replay(existing: models.Job, payload: schemas.AutoEditCreate) -> models.Job:
    request_hash = edit_digest({**payload.model_dump(mode="json", exclude={"idempotency_key", "max_attempts", "quote_id"}), "asset_ids": sorted(set(payload.asset_ids))})
    previous = existing.payload or {}
    if (previous.get("planning_request_hash") and previous["planning_request_hash"] != request_hash
            or previous.get("project_id") != payload.project_id or previous.get("template_id") != payload.template_id):
        raise HTTPException(status_code=409, detail={"code": "idempotency_input_conflict", "message": "同一操作标识已经用于不同计划"})
    return existing


@router.post("/jobs/auto-edit", response_model=schemas.JobRead, status_code=201, tags=["jobs"])
def create_auto_edit_job(
    payload: schemas.AutoEditCreate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    ensure_tenant(db, tenant_id)
    if idempotency_header and payload.idempotency_key and idempotency_header != payload.idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header does not match payload idempotency_key")
    key = _resolve_job_idempotency_key(idempotency_header) or payload.idempotency_key
    if payload.quote_id or get_settings().auth_mode != "development_header":
        if not payload.quote_id or not key:
            raise EditingError("plan_quote_required", "请先取得并确认分析与剪辑计划的综合报价", 422)
        quote = preparation_quotes.consume_quote(db, tenant_id, payload.quote_id, key,
            expected_kind="plan", expected_request=payload)
        job = db.get(models.Job, quote.result_json["job_id"])
        db.commit()
        return job
    existing = _existing_job_for_key(db, tenant_id, key, "auto_edit.plan")
    if existing is not None:
        return _check_auto_edit_replay(existing, payload)
    project = ensure_project(db, payload.project_id, tenant_id)
    template = db.scalar(select(models.Template).where(models.Template.id == payload.template_id, models.Template.tenant_id == tenant_id))
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        from timeline_engine import TemplateDefinition
        template_definition = TemplateDefinition.model_validate(template.content)
        if payload.target_duration is not None:
            template_definition.duration = payload.target_duration
        if payload.aspect_ratio is not None:
            template_definition.metadata = {**template_definition.metadata, "resolution": {
                "9:16": {"width": 1080, "height": 1920}, "16:9": {"width": 1920, "height": 1080},
                "1:1": {"width": 1080, "height": 1080}}[payload.aspect_ratio]}
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_template", "errors": exc.errors()}) from exc

    stmt = select(models.Asset).where(models.Asset.tenant_id == tenant_id, models.Asset.project_id == project.id)
    if payload.asset_ids:
        stmt = stmt.where(models.Asset.id.in_(payload.asset_ids))
    assets = list(db.scalars(stmt))
    from ..services.storyboards import output_assets
    assets = output_assets(db, assets, explicit=bool(payload.asset_ids))
    if not assets:
        raise HTTPException(status_code=422, detail="At least one project asset is required for auto-edit")
    if payload.asset_ids and len(assets) != len(set(payload.asset_ids)):
        raise HTTPException(status_code=404, detail="One or more assets were not found in the project")
    unsupported = [asset.name for asset in assets if not str(asset.kind or "").lower().startswith(("video", "image"))]
    if unsupported:
        raise HTTPException(status_code=422, detail="Auto-edit currently accepts video/image assets only: " + ", ".join(unsupported[:5]))
    for asset in assets:
        upload = (asset.metadata_json or {}).get("upload", {})
        if upload.get("status") == "created" and str(asset.uri).startswith("upload://"):
            raise HTTPException(status_code=409, detail=f"Asset '{asset.id}' has not finished uploading")
        storage = (asset.metadata_json or {}).get("storage", {})
        if storage.get("mode") == "external_presigned" and storage.get("status") != "ready":
            raise HTTPException(status_code=409, detail=f"Asset '{asset.id}' has not finished external upload")

    # A plan can be requested immediately after upload. Queue missing analysis
    # jobs with stable keys; the local worker processes analysis before the
    # auto-edit queue in the same poll, so the compiler sees shot metadata.
    analysis_job_ids: list[str] = []
    for asset in assets:
        analysis = (asset.metadata_json or {}).get("analysis")
        analysis_ready = isinstance(analysis, dict) and bool(
            analysis.get("shots") or (analysis.get("media") or {}).get("status") == "ok"
        )
        if analysis_ready and settings.app_environment == "production":
            try:
                require_planning_analysis(analysis, source_uri=asset.uri, kind=asset.kind)
            except ProviderExecutionError:
                analysis_ready = False
        if analysis_ready:
            continue
        # An upload may already have queued analysis from either client. Reuse
        # that durable job, including a failed/unknown outcome needing recovery,
        # rather than creating another paid analysis under a second key.
        previous = list(db.scalars(select(models.Job).where(
            models.Job.tenant_id == tenant_id, models.Job.type == "asset.analyze",
            models.Job.payload["asset_id"].as_string() == asset.id,
        ).order_by(models.Job.created_at.desc()).limit(50)))
        reusable = next((job for job in previous
                         if (job.payload or {}).get("uri") == asset.uri
                         and ((job.payload or {}).get("options") or {}).get("shot_analysis", True)), None)
        if reusable is not None:
            analysis_job_ids.append(reusable.id)
            continue
        analysis_key = f"auto-edit-analysis:{asset.id}"
        analysis_job = _existing_job_for_key(db, tenant_id, analysis_key, "asset.analyze")
        if analysis_job is None:
            capability_snapshot = None
            if settings.app_environment == "production":
                try:
                    capability_snapshot = require_available_capability("media_analysis")
                except ProviderExecutionError as exc:
                    raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
            analysis_job = models.Job(
                tenant_id=tenant_id,
                project_id=asset.project_id,
                type="asset.analyze",
                status="queued",
                idempotency_key=analysis_key,
                max_attempts=payload.max_attempts,
                payload={"asset_id": asset.id, "uri": asset.uri, "options": {"shot_analysis": True},
                         **({"capability_snapshot": capability_snapshot} if capability_snapshot else {})},
            )
            db.add(analysis_job)
            db.flush()
            _preflight_job_budget(db, analysis_job)
        analysis_job_ids.append(analysis_job.id)
    features = shot_features_from_assets(assets)
    plan = AutoEditPlanner().plan(template_definition, features)
    row = models.Job(
        tenant_id=tenant_id,
        project_id=project.id,
        type="auto_edit.plan",
        status="queued",
        idempotency_key=key,
        max_attempts=payload.max_attempts,
        payload={
            "template_id": payload.template_id,
            "project_id": project.id,
            "asset_ids": [asset.id for asset in assets],
            "analysis_job_ids": analysis_job_ids,
            "quality": payload.quality,
            "template_snapshot": template_definition.model_dump(mode="json"),
            "planning_request_hash": edit_digest({**payload.model_dump(mode="json", exclude={"idempotency_key", "max_attempts", "quote_id"}), "asset_ids": sorted(set(payload.asset_ids))}),
            "plan": {
                "template_id": plan.template_id,
                "template_version": plan.template_version,
                "selected": [shot.__dict__ for shot in plan.selected],
                "warnings": list(plan.warnings),
                "missing_required_slots": list(plan.missing_required_slots),
            },
        },
    )
    return _check_auto_edit_replay(_persist_job(db, row, key, "auto_edit.plan"), payload)


@router.post("/jobs/voice", response_model=schemas.JobRead, status_code=201, tags=["jobs", "identity-audio"])
def create_voice_job(
    payload: schemas.VoiceJobCreate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _resolve_job_idempotency_key(idempotency_header)
    existing = _existing_job_for_key(db, tenant_id, key, "voice.tts")
    if existing is not None:
        return existing
    project = ensure_project(db, payload.project_id, tenant_id)
    profile = db.scalar(select(models.VoiceProfile).where(models.VoiceProfile.id == payload.voice_profile_id, models.VoiceProfile.tenant_id == tenant_id))
    if profile is None:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    if profile.status in {"revoked", "suspended", "deleted"}:
        raise HTTPException(status_code=422, detail="Voice profile is not active")
    _active_consent(db, profile.consent_id, tenant_id)
    row = models.Job(
        tenant_id=tenant_id,
        project_id=project.id,
        type="voice.tts",
        status="queued",
        idempotency_key=key,
        max_attempts=payload.max_attempts,
        payload={"provider_request": {
            "capability": "voice_conversion" if payload.preserve_performance else "voice_tts",
            "voice_profile_id": profile.id,
            "text": payload.text,
            "language": payload.language,
            "provider": payload.provider,
            "model_id": payload.model_id,
            "model_version": payload.model_version,
            "options": payload.options,
            "consent_id": profile.consent_id,
        }},
    )
    return _persist_job(db, row, key, "voice.tts")


@router.post("/jobs/avatar", response_model=schemas.JobRead, status_code=201, tags=["jobs", "identity-audio"])
def create_avatar_job(
    payload: schemas.AvatarJobCreate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _resolve_job_idempotency_key(idempotency_header)
    existing = _existing_job_for_key(db, tenant_id, key, "avatar.")
    if existing is not None:
        return existing
    project = ensure_project(db, payload.project_id, tenant_id)
    profile = db.scalar(select(models.AvatarProfile).where(models.AvatarProfile.id == payload.avatar_profile_id, models.AvatarProfile.tenant_id == tenant_id))
    if profile is None:
        raise HTTPException(status_code=404, detail="Avatar profile not found")
    if profile.status in {"revoked", "suspended", "deleted"}:
        raise HTTPException(status_code=422, detail="Avatar profile is not active")
    _active_consent(db, profile.consent_id, tenant_id)
    source = db.scalar(select(models.Asset).where(models.Asset.id == payload.source_asset_id, models.Asset.tenant_id == tenant_id))
    if source is None:
        raise HTTPException(status_code=404, detail="Source asset not found")
    _require_asset_ready(source, role="Source asset")
    if payload.audio_asset_id:
        audio = db.scalar(select(models.Asset).where(models.Asset.id == payload.audio_asset_id, models.Asset.tenant_id == tenant_id))
        if audio is None:
            raise HTTPException(status_code=404, detail="Audio asset not found")
        _require_asset_ready(audio, role="Audio asset")
    row = models.Job(
        tenant_id=tenant_id,
        project_id=project.id,
        type=f"avatar.{payload.mode}",
        status="queued",
        idempotency_key=key,
        max_attempts=payload.max_attempts,
        payload={"provider_request": {
            "capability": "lip_sync" if payload.mode == "lip_sync" else "motion_transfer" if payload.mode == "motion_transfer" else "face_transform",
            "avatar_profile_id": profile.id,
            "source_asset_id": payload.source_asset_id,
            "audio_asset_id": payload.audio_asset_id,
            "provider": payload.provider,
            "model_id": payload.model_id,
            "model_version": payload.model_version,
            "options": payload.options,
            "consent_id": profile.consent_id,
        }},
    )
    return _persist_job(db, row, key, "avatar.")


@router.post("/jobs/video-generation", response_model=schemas.JobRead, status_code=201, tags=["jobs", "generation"])
def create_video_generation_job(
    payload: schemas.VideoGenerationJobCreate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _resolve_job_idempotency_key(idempotency_header)
    existing = _existing_job_for_key(db, tenant_id, key, "video.generate")
    if existing is not None:
        return existing
    project = ensure_project(db, payload.project_id, tenant_id)
    if payload.reference_asset_ids:
        assets = list(db.scalars(select(models.Asset).where(models.Asset.tenant_id == tenant_id, models.Asset.id.in_(payload.reference_asset_ids))))
        if len(assets) != len(set(payload.reference_asset_ids)):
            raise HTTPException(status_code=404, detail="One or more reference assets not found")
        for asset in assets:
            _require_asset_ready(asset, role=f"Reference asset {asset.id}")
    row = models.Job(
        tenant_id=tenant_id,
        project_id=project.id,
        type="video.generate",
        status="queued",
        idempotency_key=key,
        max_attempts=payload.max_attempts,
        payload={"provider_request": {
            "capability": "video_generation",
            "prompt": payload.prompt,
            "reference_asset_ids": payload.reference_asset_ids,
            "provider": payload.provider,
            "model_id": payload.model_id,
            "model_version": payload.model_version,
            "quality": payload.quality,
            "options": payload.options,
        }},
    )
    return _persist_job(db, row, key, "video.generate")


@router.post("/jobs/enhancement", response_model=schemas.JobRead, status_code=201, tags=["jobs", "restoration"])
def create_enhancement_job(
    payload: schemas.EnhancementJobCreate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _resolve_job_idempotency_key(idempotency_header)
    existing = _existing_job_for_key(db, tenant_id, key, "enhancement.")
    if existing is not None:
        return existing
    project = ensure_project(db, payload.project_id, tenant_id)
    if not payload.rights_confirmed:
        raise HTTPException(status_code=422, detail="Rights confirmation is required for authorized content cleanup")
    asset = db.scalar(select(models.Asset).where(models.Asset.id == payload.asset_id, models.Asset.tenant_id == tenant_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    _require_asset_ready(asset)
    row = models.Job(
        tenant_id=tenant_id,
        project_id=project.id,
        type="enhancement.restore",
        status="queued",
        idempotency_key=key,
        max_attempts=payload.max_attempts,
        payload={"provider_request": {
            "capability": "enhancement",
            "asset_id": asset.id,
            "operations": payload.operations,
            "rights_confirmed": payload.rights_confirmed,
            "model_id": payload.model_id,
            "model_version": payload.model_version,
            "options": payload.options,
        }},
    )
    return _persist_job(db, row, key, "enhancement.")


@router.get("/jobs", response_model=list[schemas.JobRead], tags=["jobs"])
def list_jobs(project_id: str | None = None, revision_id: str | None = Query(default=None, max_length=64), job_status: str | None = Query(default=None, alias="status"), skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), idempotency_key: str | None = Query(default=None, max_length=255), job_type: str | None = Query(default=None, max_length=80), order: str = Query(default="asc", pattern="^(asc|desc)$"), tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); stmt = select(models.Job).where(models.Job.tenant_id == tenant_id)
    if project_id: stmt = stmt.where(models.Job.project_id == project_id)
    if job_status: stmt = stmt.where(models.Job.status == job_status)
    if idempotency_key: stmt = stmt.where(models.Job.idempotency_key == idempotency_key)
    if job_type: stmt = stmt.where(models.Job.type == job_type)
    if revision_id: stmt = stmt.where(models.Job.payload["timeline_revision_id"].as_string() == revision_id)
    return list(db.scalars(stmt.order_by(models.Job.created_at.desc() if order == "desc" else models.Job.created_at, models.Job.id).offset(skip).limit(limit)))


@router.get("/jobs/stats", response_model=schemas.JobStatsRead, tags=["jobs", "operations"])
def get_job_stats(
    project_id: str | None = None,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    """Return aggregate Job counters without exposing payloads or other tenants."""
    ensure_tenant(db, tenant_id)
    if project_id:
        ensure_project(db, project_id, tenant_id)
    return job_stats(db, tenant_id=tenant_id, project_id=project_id)


@router.get("/jobs/{job_id}", response_model=schemas.JobRead, tags=["jobs"])
def get_job(job_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Job).where(models.Job.id == job_id, models.Job.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Job not found")
    return row


def _job_for_tenant(db: Session, job_id: str, tenant_id: str) -> models.Job:
    row = db.scalar(select(models.Job).where(models.Job.id == job_id, models.Job.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


@router.get("/jobs/{job_id}/steps", response_model=list[schemas.JobStepRead], tags=["jobs"])
def list_job_steps(
    job_id: str,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    _job_for_tenant(db, job_id, tenant_id)
    return list(db.scalars(select(models.JobStep).where(models.JobStep.job_id == job_id, models.JobStep.tenant_id == tenant_id).order_by(models.JobStep.sequence, models.JobStep.created_at)))


@router.get("/jobs/{job_id}/steps/{step_id}", response_model=schemas.JobStepRead, tags=["jobs"])
def get_job_step(
    job_id: str,
    step_id: str,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    _job_for_tenant(db, job_id, tenant_id)
    step = db.scalar(select(models.JobStep).where(models.JobStep.id == step_id, models.JobStep.job_id == job_id, models.JobStep.tenant_id == tenant_id))
    if step is None:
        raise HTTPException(status_code=404, detail="Job step not found")
    return step


@router.post("/jobs/{job_id}/steps", response_model=schemas.JobStepRead, status_code=201, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
def add_job_step(
    job_id: str,
    payload: schemas.JobStepCreate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    job = _job_for_tenant(db, job_id, tenant_id)
    step = create_step(db, job, name=payload.name, sequence=payload.sequence, status=payload.status, progress=payload.progress, payload=payload.payload)
    try:
        db.commit(); db.refresh(step)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A step with this name already exists for the job") from exc
    return step


@router.put("/jobs/{job_id}/steps/{step_id}", response_model=schemas.JobStepRead, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
@router.patch("/jobs/{job_id}/steps/{step_id}", response_model=schemas.JobStepRead, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
def update_job_step(
    job_id: str,
    step_id: str,
    payload: schemas.JobStepUpdate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    _job_for_tenant(db, job_id, tenant_id)
    step = db.scalar(select(models.JobStep).where(models.JobStep.id == step_id, models.JobStep.job_id == job_id, models.JobStep.tenant_id == tenant_id))
    if step is None:
        raise HTTPException(status_code=404, detail="Job step not found")
    try:
        if payload.status is not None:
            transition_step(db, step, payload.status, progress=payload.progress, error=payload.error)
        elif payload.progress is not None:
            step.progress = payload.progress
        if payload.payload is not None:
            step.payload = payload.payload
        if payload.error is not None and payload.status is None:
            step.error = payload.error
        db.commit(); db.refresh(step)
    except InvalidJobTransition as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return step


@router.post("/jobs/{job_id}/steps/{step_id}/status", response_model=schemas.JobStepRead, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
def set_job_step_status(
    job_id: str,
    step_id: str,
    payload: schemas.JobStatusUpdate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    return update_job_step(job_id, step_id, schemas.JobStepUpdate(status=payload.status, error=payload.error), tenant_id, db)


@router.put("/jobs/{job_id}/status", response_model=schemas.JobRead, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
@router.patch("/jobs/{job_id}/status", response_model=schemas.JobRead, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
@router.put("/jobs/{job_id}", response_model=schemas.JobRead, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
@router.patch("/jobs/{job_id}", response_model=schemas.JobRead, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
def update_job_status(
    job_id: str,
    payload: schemas.JobStatusUpdate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    job = _job_for_tenant(db, job_id, tenant_id)
    try:
        lock_job(db, job)
        if payload.status in {"succeeded", "failed", "cancelled"}:
            finish_job(db, job, payload.status, error=payload.error)
        else:
            transition_job(db, job, payload.status, error=payload.error)
        db.commit(); db.refresh(job)
    except (InvalidJobTransition, ExecutionSuperseded) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


@router.post("/jobs/{job_id}/cancel", response_model=schemas.JobRead, tags=["jobs"])
def cancel_job(
    job_id: str,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    """Cancel a queued or running job while preserving its audit timestamps."""
    job = _job_for_tenant(db, job_id, tenant_id)
    try:
        lock_job(db, job)
        finish_job(db, job, "cancelled")
        db.commit()
        db.refresh(job)
    except (InvalidJobTransition, ExecutionSuperseded) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


@router.post("/jobs/{job_id}/retry", response_model=schemas.JobRead, tags=["jobs", "operations"])
def retry_job(
    job_id: str,
    payload: schemas.JobRetryRequest | None = None,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    """Explicitly requeue one failed Job without duplicating provider work.

    The job and its durable workflow are updated in one transaction.  A
    repeated request while the retry is already queued returns the same state
    without appending another workflow event or changing its deadline.
    """

    job = _job_for_tenant(db, job_id, tenant_id)
    payload = payload or schemas.JobRetryRequest()
    try:
        lock_job(db, job)
        request_job_retry(db, job, delay_seconds=payload.delay_seconds, force=payload.force)
        workflow = db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job.id))
        if workflow is not None:
            request_workflow_retry(db, workflow, delay_seconds=payload.delay_seconds)
        db.commit()
        db.refresh(job)
    except (InvalidJobTransition, WorkflowError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "job_retry_rejected", "message": str(exc)}) from exc
    return job


@router.post("/jobs/{job_id}/reconcile-cost", tags=["budgets"], dependencies=[Depends(require_internal_execution)])
def reconcile_job_cost(
    job_id: str,
    payload: schemas.UsageReconciliationRequest,
    identity: RequestIdentity = Depends(authenticate_request),
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    job = _job_for_tenant(db, job_id, tenant_id)
    lock_job(db, job)
    evidence = db.scalar(select(models.UsageEvidence).where(
        models.UsageEvidence.id == payload.evidence_id,
        models.UsageEvidence.tenant_id == tenant_id,
        models.UsageEvidence.job_id == job_id,
    ))
    if evidence is None:
        raise HTTPException(status_code=404, detail="Usage evidence not found")
    try:
        row = reconcile_usage_evidence(db, job=job, evidence=evidence,
                                      approved_by=identity.principal_id or "development-operator", reason=payload.reason)
        db.commit()
        return {"id": row.id, "job_id": row.job_id, "evidence_id": row.evidence_id,
                "delta_micros": row.delta_micros, "currency": row.currency, "period_key": row.period_key}
    except BudgetDataError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/jobs/{job_id}/cost-evidence", tags=["budgets"], dependencies=[Depends(require_internal_execution)])
def get_job_cost_evidence(job_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    _job_for_tenant(db, job_id, tenant_id)
    evidence = db.scalars(select(models.UsageEvidence).where(models.UsageEvidence.job_id == job_id)
                          .order_by(models.UsageEvidence.created_at))
    corrections = db.scalars(select(models.UsageAdjustment).where(models.UsageAdjustment.job_id == job_id))
    return {
        "evidence": [{"id": row.id, "amount_micros": row.amount_micros, "currency": row.currency,
                      "provider": row.provider, "source": row.source, "disposition": row.disposition} for row in evidence],
        "adjustments": [{"id": row.id, "evidence_id": row.evidence_id, "delta_micros": row.delta_micros,
                         "currency": row.currency, "approved_by": row.approved_by, "reason": row.reason} for row in corrections],
    }


@router.delete("/jobs/{job_id}", status_code=204, tags=["jobs"], dependencies=[Depends(require_internal_execution)])
def delete_job(job_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Job).where(models.Job.id == job_id, models.Job.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Job not found")
    db.delete(row); db.commit()


def _workflow_for_tenant(db: Session, workflow_id: str, tenant_id: str) -> models.WorkflowRun:
    row = db.scalar(
        select(models.WorkflowRun).where(
            models.WorkflowRun.id == workflow_id,
            models.WorkflowRun.tenant_id == tenant_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return row


@router.post("/jobs/{job_id}/workflow", response_model=schemas.WorkflowRunRead, status_code=201, tags=["workflows"], dependencies=[Depends(require_internal_execution)])
def create_job_workflow(
    job_id: str,
    payload: schemas.WorkflowCreate | None = None,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    """Attach one durable workflow runtime to a public Job."""
    job = _job_for_tenant(db, job_id, tenant_id)
    payload = payload or schemas.WorkflowCreate()
    try:
        run = create_workflow(
            db,
            job,
            workflow_type=payload.workflow_type,
            runtime=payload.runtime,
            max_attempts=payload.max_attempts,
            steps=payload.steps,
        )
        db.commit(); db.refresh(run)
    except (ValueError, WorkflowError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@router.get("/jobs/{job_id}/workflow", response_model=schemas.WorkflowRunRead, tags=["workflows"])
def get_job_workflow(job_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    job = _job_for_tenant(db, job_id, tenant_id)
    run = db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job.id))
    if run is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return run


@router.get("/workflows/{workflow_id}", response_model=schemas.WorkflowRunRead, tags=["workflows"])
def get_workflow(workflow_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return _workflow_for_tenant(db, workflow_id, tenant_id)


@router.get("/workflows/{workflow_id}/steps", response_model=list[schemas.WorkflowStepRead], tags=["workflows"])
def list_workflow_steps(workflow_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    _workflow_for_tenant(db, workflow_id, tenant_id)
    return list(
        db.scalars(
            select(models.WorkflowStep)
            .where(models.WorkflowStep.workflow_run_id == workflow_id, models.WorkflowStep.tenant_id == tenant_id)
            .order_by(models.WorkflowStep.sequence, models.WorkflowStep.created_at)
        )
    )


@router.get("/workflows/{workflow_id}/events", response_model=list[schemas.WorkflowEventRead], tags=["workflows"])
def list_workflow_events(workflow_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    _workflow_for_tenant(db, workflow_id, tenant_id)
    return list(
        db.scalars(
            select(models.WorkflowEvent)
            .where(models.WorkflowEvent.workflow_run_id == workflow_id, models.WorkflowEvent.tenant_id == tenant_id)
            .order_by(models.WorkflowEvent.sequence)
        )
    )


@router.post("/workflows/{workflow_id}/claim", response_model=schemas.WorkflowRunRead, tags=["workflows"], dependencies=[Depends(require_internal_execution)])
def claim_workflow_route(
    workflow_id: str,
    payload: schemas.WorkflowLeaseUpdate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    run = _workflow_for_tenant(db, workflow_id, tenant_id)
    if not claim_workflow(db, run, worker_id=payload.worker_id, lease_seconds=payload.lease_seconds):
        raise HTTPException(status_code=409, detail="Workflow is not claimable")
    db.commit(); db.refresh(run)
    return run


@router.post("/workflows/{workflow_id}/heartbeat", response_model=schemas.WorkflowRunRead, tags=["workflows"], dependencies=[Depends(require_internal_execution)])
def heartbeat_workflow_route(
    workflow_id: str,
    payload: schemas.WorkflowLeaseUpdate,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    run = _workflow_for_tenant(db, workflow_id, tenant_id)
    try:
        heartbeat_workflow(db, run, worker_id=payload.worker_id, lease_seconds=payload.lease_seconds)
        db.commit(); db.refresh(run)
    except WorkflowLeaseLost as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@router.post("/workflows/{workflow_id}/retry", response_model=schemas.WorkflowRunRead, tags=["workflows"], dependencies=[Depends(require_internal_execution)])
def retry_workflow_route(
    workflow_id: str,
    payload: schemas.WorkflowRetryRequest,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    run = _workflow_for_tenant(db, workflow_id, tenant_id)
    try:
        retry_workflow(
            db,
            run,
            worker_id=payload.worker_id,
            error=payload.error,
            failure_code=payload.failure_code,
            delay_seconds=payload.delay_seconds,
        )
        db.commit(); db.refresh(run)
    except WorkflowError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@router.post("/workflows/{workflow_id}/cancel", response_model=schemas.WorkflowRunRead, tags=["workflows"], dependencies=[Depends(require_internal_execution)])
def cancel_workflow_route(
    workflow_id: str,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
):
    run = _workflow_for_tenant(db, workflow_id, tenant_id)
    try:
        cancel_workflow(db, run)
        db.commit(); db.refresh(run)
    except WorkflowError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@router.post("/artifacts", response_model=schemas.ArtifactRead, status_code=201, tags=["artifacts"], dependencies=[Depends(require_internal_execution)])
def create_artifact(payload: schemas.ArtifactCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); _check_optional_project(db, payload.project_id, tenant_id)
    if payload.job_id and db.scalar(select(models.Job).where(models.Job.id == payload.job_id, models.Job.tenant_id == tenant_id)) is None:
        raise HTTPException(404, "Job not found")
    row = models.Artifact(tenant_id=tenant_id, project_id=payload.project_id, job_id=payload.job_id, kind=payload.kind, uri=payload.uri, metadata_json=payload.metadata)
    db.add(row); db.commit(); db.refresh(row); return row


@router.post("/provider-callbacks/{job_id}", response_model=schemas.JobRead, tags=["providers", "jobs"])
async def provider_callback(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    signature: str | None = Header(default=None, alias="X-Provider-Signature"),
    provider: str | None = Header(default=None, alias="X-Provider-Name"),
    timestamp: str | None = Header(default=None, alias="X-Provider-Timestamp"),
):
    """Accept a signed completion event from an external AI provider.

    The provider owns inference and output storage. This endpoint only stores
    a remote Artifact URL and advances the durable Job state; it never runs a
    model or downloads the returned media.
    """
    if not settings.provider_webhook_secret:
        raise HTTPException(status_code=503, detail="provider callbacks are not configured")
    if settings.app_environment == "production" and not timestamp:
        raise HTTPException(status_code=401, detail="provider callback timestamp is required")
    raw = await request.body()
    if not verify_signature(
        raw,
        signature,
        settings.provider_webhook_secret,
        timestamp=timestamp,
        max_age_seconds=settings.provider_webhook_max_age_seconds,
    ):
        raise HTTPException(status_code=401, detail="invalid provider callback signature")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="provider callback body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="provider callback body must be an object")
    try:
        callback = schemas.ProviderCallbackPayload.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    if settings.provider_webhook_require_request_id and not callback.request_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "callback_request_id_required",
                "message": "provider callback request_id is required",
            },
        )
    if settings.provider_webhook_require_request_id and not callback.external_job_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "callback_external_job_id_required",
                "message": "provider callback external_job_id is required",
            },
        )
    job = db.scalar(select(models.Job).where(models.Job.id == job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        updated, _already_applied = apply_callback(db, job=job, payload=callback.model_dump(exclude_none=True), provider=provider)
        db.commit(); db.refresh(updated)
        return updated
    except ProviderCallbackError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="provider event conflicts with existing evidence") from exc
    except BudgetDataError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc), "details": exc.details}) from exc


@router.get("/artifacts", response_model=list[schemas.ArtifactRead], tags=["artifacts"])
def list_artifacts(project_id: str | None = None, job_id: str | None = None, skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); stmt = select(models.Artifact).where(models.Artifact.tenant_id == tenant_id)
    if project_id: stmt = stmt.where(models.Artifact.project_id == project_id)
    if job_id: stmt = stmt.where(models.Artifact.job_id == job_id)
    rows = list(db.scalars(stmt.order_by(models.Artifact.created_at).offset(skip).limit(limit)))
    jobs = {job.id: job for job in db.scalars(select(models.Job).where(models.Job.tenant_id == tenant_id,
        models.Job.id.in_([row.job_id for row in rows if row.job_id])))}
    revision_ids = [str((row.metadata_json or {}).get("timeline_revision_id") or "") for row in rows]
    revisions = {revision.id: revision for revision in db.scalars(select(models.PlanRevision).where(
        models.PlanRevision.tenant_id == tenant_id, models.PlanRevision.id.in_(revision_ids)))}
    return [schemas.ArtifactRead.model_validate(row).model_copy(update={
        "job_status": jobs[row.job_id].status if row.job_id in jobs else None,
        "job_policy_blocked": bool(jobs[row.job_id].policy_blocked) if row.job_id in jobs else True,
        "edit_revision_number": revisions[str((row.metadata_json or {}).get("timeline_revision_id"))].number
            if str((row.metadata_json or {}).get("timeline_revision_id")) in revisions else None}) for row in rows]


@router.get("/artifacts/{artifact_id}", response_model=schemas.ArtifactRead, tags=["artifacts"])
def get_artifact(artifact_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Artifact).where(models.Artifact.id == artifact_id, models.Artifact.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Artifact not found")
    job = db.get(models.Job, row.job_id) if row.job_id else None
    if job and job.tenant_id != tenant_id:
        job = None
    revision = db.get(models.PlanRevision, str((row.metadata_json or {}).get("timeline_revision_id") or ""))
    return schemas.ArtifactRead.model_validate(row).model_copy(update={"job_status": job.status if job else None,
        "job_policy_blocked": bool(job.policy_blocked) if job else True,
        "edit_revision_number": revision.number if revision and revision.tenant_id == tenant_id else None})


@router.delete("/artifacts/{artifact_id}", status_code=204, tags=["artifacts"])
def delete_artifact(artifact_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Artifact).where(models.Artifact.id == artifact_id, models.Artifact.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Artifact not found")
    db.delete(row); db.commit()


@router.post("/consents", response_model=schemas.ConsentRead, status_code=201, tags=["consents"])
def create_consent(payload: schemas.ConsentCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); _check_optional_project(db, payload.project_id, tenant_id)
    row = models.Consent(tenant_id=tenant_id, project_id=payload.project_id, subject_id=payload.subject_id, scope=payload.scope, granted=payload.granted, granted_at=payload.granted_at, expires_at=payload.expires_at, evidence_uri=payload.evidence_uri)
    db.add(row); db.commit(); db.refresh(row); return row


def _active_consent(db: Session, consent_id: str, tenant_id: str) -> models.Consent:
    consent = db.scalar(select(models.Consent).where(models.Consent.id == consent_id, models.Consent.tenant_id == tenant_id))
    if consent is None:
        raise HTTPException(status_code=404, detail="Consent not found")
    if not consent.granted or consent.revoked_at is not None:
        raise HTTPException(status_code=422, detail="Consent is not active")
    if consent.expires_at is not None and consent.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="Consent has expired")
    return consent


@router.post("/voice-profiles", response_model=schemas.VoiceProfileRead, status_code=201, tags=["identity-audio"])
def create_voice_profile(payload: schemas.VoiceProfileCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    consent = _active_consent(db, payload.consent_id, tenant_id)
    if "voice" not in consent.scope and payload.scopes:
        raise HTTPException(status_code=422, detail="Consent scope does not allow voice operations")
    _check_optional_project(db, payload.project_id, tenant_id)
    if payload.sample_asset_id:
        asset = db.scalar(select(models.Asset).where(models.Asset.id == payload.sample_asset_id, models.Asset.tenant_id == tenant_id))
        if asset is None:
            raise HTTPException(status_code=404, detail="Sample asset not found")
        _require_asset_ready(asset, role="Sample asset")
    row = models.VoiceProfile(
        tenant_id=tenant_id, project_id=payload.project_id, consent_id=payload.consent_id,
        name=payload.name, provider=payload.provider, model_version=payload.model_version,
        sample_asset_id=payload.sample_asset_id, scopes=payload.scopes, expires_at=payload.expires_at,
        status="ready" if payload.provider != "pending" else "consent_verified",
    )
    db.add(row); db.commit(); db.refresh(row); return row


@router.get("/voice-profiles", response_model=list[schemas.VoiceProfileRead], tags=["identity-audio"])
def list_voice_profiles(tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id)
    return list(db.scalars(select(models.VoiceProfile).where(models.VoiceProfile.tenant_id == tenant_id).order_by(models.VoiceProfile.created_at)))


@router.post("/voice-profiles/{profile_id}/revoke", response_model=schemas.VoiceProfileRead, tags=["identity-audio"])
def revoke_voice_profile(profile_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.VoiceProfile).where(models.VoiceProfile.id == profile_id, models.VoiceProfile.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    row.status = "revoked"; row.revoked_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(row); return row


@router.post("/avatar-profiles", response_model=schemas.AvatarProfileRead, status_code=201, tags=["identity-audio"])
def create_avatar_profile(payload: schemas.AvatarProfileCreate, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    consent = _active_consent(db, payload.consent_id, tenant_id)
    if "face" not in consent.scope and "avatar" not in consent.scope:
        raise HTTPException(status_code=422, detail="Consent scope does not allow avatar operations")
    _check_optional_project(db, payload.project_id, tenant_id)
    if payload.reference_asset_id:
        asset = db.scalar(select(models.Asset).where(models.Asset.id == payload.reference_asset_id, models.Asset.tenant_id == tenant_id))
        if asset is None:
            raise HTTPException(status_code=404, detail="Reference asset not found")
        _require_asset_ready(asset, role="Reference asset")
    row = models.AvatarProfile(
        tenant_id=tenant_id, project_id=payload.project_id, consent_id=payload.consent_id,
        name=payload.name, provider=payload.provider, model_version=payload.model_version,
        reference_asset_id=payload.reference_asset_id, scopes=payload.scopes, expires_at=payload.expires_at,
        status="ready" if payload.provider != "pending" else "consent_verified",
    )
    db.add(row); db.commit(); db.refresh(row); return row


@router.get("/avatar-profiles", response_model=list[schemas.AvatarProfileRead], tags=["identity-audio"])
def list_avatar_profiles(tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id)
    return list(db.scalars(select(models.AvatarProfile).where(models.AvatarProfile.tenant_id == tenant_id).order_by(models.AvatarProfile.created_at)))


@router.post("/avatar-profiles/{profile_id}/revoke", response_model=schemas.AvatarProfileRead, tags=["identity-audio"])
def revoke_avatar_profile(profile_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.AvatarProfile).where(models.AvatarProfile.id == profile_id, models.AvatarProfile.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Avatar profile not found")
    row.status = "revoked"; row.revoked_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(row); return row


@router.get("/consents", response_model=list[schemas.ConsentRead], tags=["consents"])
def list_consents(subject_id: str | None = None, project_id: str | None = None, skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id); stmt = select(models.Consent).where(models.Consent.tenant_id == tenant_id)
    if subject_id: stmt = stmt.where(models.Consent.subject_id == subject_id)
    if project_id: stmt = stmt.where(models.Consent.project_id == project_id)
    return list(db.scalars(stmt.order_by(models.Consent.created_at).offset(skip).limit(limit)))


@router.get("/consents/{consent_id}", response_model=schemas.ConsentRead, tags=["consents"])
def get_consent(consent_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Consent).where(models.Consent.id == consent_id, models.Consent.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Consent not found")
    return row


@router.post("/consents/{consent_id}/revoke", response_model=schemas.ConsentRead, tags=["consents"])
def revoke_consent(consent_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Consent).where(models.Consent.id == consent_id, models.Consent.tenant_id == tenant_id))
    if not row:
        raise HTTPException(404, "Consent not found")
    row.granted = False
    row.revoked_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(row); return row


@router.delete("/consents/{consent_id}", status_code=204, tags=["consents"])
def delete_consent(consent_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row = db.scalar(select(models.Consent).where(models.Consent.id == consent_id, models.Consent.tenant_id == tenant_id))
    if not row: raise HTTPException(404, "Consent not found")
    db.delete(row); db.commit()
