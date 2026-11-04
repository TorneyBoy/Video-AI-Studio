"""Shared web and mini-program reference/storyboard workbench."""
from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .deps import RequestIdentity, authenticate_request, require_tenant_id
from .identity import PrivateValidationRoute
from .. import models, storyboard_schemas as schemas
from ..database import get_db
from ..services import storyboards as service
from ..services.editing_errors import EditingError
from ..services.storage_gateway import StorageGateway, configured_storage_gateway

router = APIRouter(tags=["storyboards"], route_class=PrivateValidationRoute)


def request_key(key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255)):
    if not key.strip() or any(ord(char) < 32 for char in key):
        raise EditingError("idempotency_key_invalid", "操作标识无效", 422)
    return key


def commit(db):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise EditingError("storyboard_concurrent_write", "有另一请求正在保存，请查询原操作后再继续") from None


@router.get("/projects/{project_id}/references")
def references(project_id: str, skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
               tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    service.project_for(db, tenant, project_id)
    rows = db.scalars(select(models.ReferenceAnalysis).where(models.ReferenceAnalysis.tenant_id == tenant,
        models.ReferenceAnalysis.project_id == project_id).order_by(models.ReferenceAnalysis.created_at.desc(),
        models.ReferenceAnalysis.id).offset(skip).limit(limit))
    return [service.reference_read(db, row, detail=False) for row in rows]


@router.post("/projects/{project_id}/references")
def create_reference(project_id: str, payload: schemas.ReferenceCreate, key: str = Depends(request_key),
                     tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                     identity: RequestIdentity = Depends(authenticate_request)):
    row = service.create_reference(db, tenant, project_id, payload, key, identity.principal_id or "development_user")
    result = service.reference_read(db, row)
    commit(db)
    return result


@router.get("/projects/{project_id}/reference-requests/{key}")
def reference_receipt(project_id: str, key: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    service.project_for(db, tenant, project_id)
    row = db.scalar(select(models.ReferenceAnalysis).where(models.ReferenceAnalysis.project_id == project_id,
        models.ReferenceAnalysis.tenant_id == tenant, models.ReferenceAnalysis.request_key == key))
    return {"found": row is not None, "result": service.reference_read(db, row) if row else None}


@router.get("/references/{reference_id}")
def reference(reference_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return service.reference_read(db, service.reference_for(db, tenant, reference_id))


@router.post("/references/{reference_id}/usage")
def reference_usage(reference_id: str, payload: schemas.ReferenceUsageUpdate,
                    tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    result = service.set_usage(db, tenant, service.reference_for(db, tenant, reference_id), payload.allow_output)
    commit(db)
    return result


@router.post("/references/{reference_id}/access")
def reference_access(reference_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                     gateway: StorageGateway = Depends(configured_storage_gateway)):
    row = service.reference_for(db, tenant, reference_id)
    asset = service._asset(db, tenant, row.project_id, row.asset_id)
    if service.source_hash(asset) != row.source_hash:
        raise EditingError("reference_source_changed", "素材对象已变化；请为当前对象重新登记参考")
    # The storage service signs only its tenant-scoped object. No new AI job.
    from ..services.derived_rights import asset_playback
    grant = asset_playback(db, asset, tenant_id=tenant, gateway=gateway, expected_source_hash=row.source_hash)
    return {"url": grant["url"], "expires_at": grant["expires_at"]}


@router.get("/projects/{project_id}/storyboards")
def storyboards(project_id: str, skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    service.project_for(db, tenant, project_id)
    rows = db.scalars(select(models.StoryboardDocument).where(models.StoryboardDocument.tenant_id == tenant,
        models.StoryboardDocument.project_id == project_id).order_by(models.StoryboardDocument.created_at.desc(),
        models.StoryboardDocument.id).offset(skip).limit(limit))
    return [service.story_read(db, row, detail=False) for row in rows]


@router.post("/projects/{project_id}/storyboards")
def create_storyboard(project_id: str, payload: schemas.StoryCreate, key: str = Depends(request_key),
                      tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                      identity: RequestIdentity = Depends(authenticate_request)):
    result = service.create_story(db, tenant, project_id, payload, key, identity.principal_id or "development_user")
    commit(db)
    return result


@router.get("/projects/{project_id}/storyboard-requests/{key}")
def storyboard_creation_receipt(project_id: str, key: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    service.project_for(db, tenant, project_id)
    row = db.scalar(select(models.StoryboardDocument).where(models.StoryboardDocument.project_id == project_id,
        models.StoryboardDocument.tenant_id == tenant, models.StoryboardDocument.request_key == key))
    first = db.scalar(select(models.StoryboardRevision).where(models.StoryboardRevision.document_id == row.id,
        models.StoryboardRevision.number == 1)) if row else None
    return {"found": row is not None, "result": service.story_read(db, row, first) if row else None}


@router.get("/storyboards/{document_id}")
def storyboard(document_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return service.story_read(db, service.document_for(db, tenant, document_id))


@router.get("/storyboards/{document_id}/revisions")
def revisions(document_id: str, skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
              tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = service.document_for(db, tenant, document_id)
    rows = db.scalars(select(models.StoryboardRevision).where(models.StoryboardRevision.document_id == document.id,
        models.StoryboardRevision.tenant_id == tenant).order_by(models.StoryboardRevision.number.desc()).offset(skip).limit(limit))
    return [service.story_read(db, document, row) for row in rows]


@router.get("/storyboards/{document_id}/requests/{key}")
def revision_receipt(document_id: str, key: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = service.document_for(db, tenant, document_id)
    row = db.scalar(select(models.StoryboardRevision).where(models.StoryboardRevision.document_id == document.id,
        models.StoryboardRevision.tenant_id == tenant, models.StoryboardRevision.request_key == key))
    return {"found": row is not None, "result": service.story_read(db, document, row) if row else None}


@router.post("/storyboards/{document_id}/revisions")
def save_storyboard(document_id: str, payload: schemas.StorySave, key: str = Depends(request_key),
                    tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                    identity: RequestIdentity = Depends(authenticate_request)):
    result = service.save_story(db, tenant, document_id, payload, key, identity.principal_id or "development_user")
    commit(db)
    return result
