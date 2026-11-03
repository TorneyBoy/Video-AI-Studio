"""Storage metadata API shared by both user clients."""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import ensure_project, ensure_tenant, require_internal_execution, require_tenant_id
from .. import models, schemas
from ..config import get_settings
from ..database import get_db
from ..services.storage_gateway import StorageGateway, configured_storage_gateway
from ..services.storage_ingestion import create_gateway_upload, verify_gateway_upload
from ..services.artifact_delivery import archive_artifact, artifact_access
from ..services.asset_library import upload_intent_read, update_asset_labels


router = APIRouter(tags=["storage"])


@router.get("/assets/upload-intents", response_model=list[schemas.UploadIntentRead])
def pending_uploads(project_id: str | None = None, skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                    sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"), include_completed: bool = False,
                    tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_tenant(db, tenant_id)
    if project_id:
        ensure_project(db, project_id, tenant_id)
    query = select(models.StorageUploadIntent).where(models.StorageUploadIntent.tenant_id == tenant_id)
    if not include_completed:
        query = query.where(models.StorageUploadIntent.status != "completed")
    if project_id:
        query = query.where(models.StorageUploadIntent.project_id == project_id)
    if sha256:
        query = query.where(models.StorageUploadIntent.request_json["sha256"].as_string() == sha256)
    rows = db.scalars(query.order_by(models.StorageUploadIntent.created_at.desc()).offset(skip).limit(limit))
    return [upload_intent_read(row) for row in rows]


@router.patch("/assets/{asset_id}/labels", response_model=schemas.AssetRead)
@router.post("/assets/{asset_id}/labels", response_model=schemas.AssetRead)
def edit_asset_labels(asset_id: str, payload: schemas.AssetLabelsUpdate,
                      tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return update_asset_labels(db, tenant_id=tenant_id, asset_id=asset_id, changes=payload)


@router.get("/storage/config")
def storage_configuration(tenant_id: str = Depends(require_tenant_id)):
    settings = get_settings()
    return {"mode": settings.asset_storage_mode, "max_upload_bytes": settings.max_upload_bytes,
            "direct_upload_configured": bool(settings.storage_gateway_url and settings.storage_gateway_api_key),
            "upload_protocols": ["PUT", "POST"]}


@router.post("/assets/upload-intents", response_model=schemas.RemoteUploadSessionRead, status_code=201)
def create_upload_intent(
    payload: schemas.UploadSessionCreate,
    idempotency_key: str = Header(min_length=1, max_length=255, alias="Idempotency-Key"),
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    gateway: StorageGateway = Depends(configured_storage_gateway),
):
    ensure_tenant(db, tenant_id)
    if payload.project_id:
        ensure_project(db, payload.project_id, tenant_id)
    key = idempotency_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail={"code": "idempotency_key_required"})
    return create_gateway_upload(db, tenant_id=tenant_id, key=key, specification=payload,
                                  settings=get_settings(), gateway=gateway)


@router.post("/assets/{asset_id}/verify-storage-upload", response_model=schemas.AssetRead)
def verify_storage_upload(
    asset_id: str,
    tenant_id: str = Depends(require_tenant_id),
    db: Session = Depends(get_db),
    gateway: StorageGateway = Depends(configured_storage_gateway),
):
    ensure_tenant(db, tenant_id)
    return verify_gateway_upload(db, tenant_id=tenant_id, asset_id=asset_id,
                                  settings=get_settings(), gateway=gateway)


@router.post("/artifacts/{artifact_id}/archive", dependencies=[Depends(require_internal_execution)])
def archive_output(artifact_id: str, tenant_id: str = Depends(require_tenant_id),
                   db: Session = Depends(get_db), gateway: StorageGateway = Depends(configured_storage_gateway)):
    row = archive_artifact(db, tenant_id=tenant_id, artifact_id=artifact_id,
                           settings=get_settings(), gateway=gateway)
    return {"artifact_id": row.artifact_id, "object_uri": row.object_uri, "sha256": row.sha256,
            "size_bytes": row.size_bytes, "retention_expires_at": row.expires_at}


@router.post("/artifacts/{artifact_id}/access", response_model=schemas.ArtifactAccessRead)
def output_access(artifact_id: str, payload: schemas.ArtifactAccessRequest,
                  tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                  gateway: StorageGateway = Depends(configured_storage_gateway)):
    return artifact_access(db, tenant_id=tenant_id, artifact_id=artifact_id, purpose=payload.purpose, gateway=gateway)
