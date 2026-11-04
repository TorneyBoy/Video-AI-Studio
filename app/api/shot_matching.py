"""Personal matching and sourcing APIs, shared by both clients."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import RequestIdentity, authenticate_request, require_tenant_id
from .identity import PrivateValidationRoute
from .storyboards import commit, request_key
from .. import matching_schemas as schemas, models
from ..database import get_db
from ..services import shot_matching as service
from ..services.editing_errors import EditingError
from ..services.storage_gateway import StorageGateway, configured_storage_gateway

router = APIRouter(tags=["shot-matching"], route_class=PrivateValidationRoute)


@router.get("/assets/{asset_id}/semantics")
def semantics(asset_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return service.semantic_read(db, service.asset_for(db, tenant, asset_id))


@router.post("/assets/{asset_id}/semantics")
def save_semantics(asset_id: str, payload: schemas.SemanticSave, key: str = Depends(request_key),
                   tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                   identity: RequestIdentity = Depends(authenticate_request)):
    result = service.save_semantics(db, tenant, asset_id, payload, key, identity.principal_id or "development_user")
    commit(db); return result


@router.get("/assets/{asset_id}/semantic-revisions/{revision_id}")
def semantic_revision(asset_id: str, revision_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    asset = service.asset_for(db, tenant, asset_id)
    row = db.scalar(select(models.AssetSemanticRevision).where(models.AssetSemanticRevision.id == revision_id,
        models.AssetSemanticRevision.asset_id == asset.id, models.AssetSemanticRevision.tenant_id == tenant))
    if row is None:
        raise EditingError("semantic_revision_missing", "未找到此素材的内容标注版本", 404)
    return service.semantic_read(db, asset, row)


@router.get("/assets/{asset_id}/semantic-requests/{key}")
def semantics_receipt(asset_id: str, key: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    asset = service.asset_for(db, tenant, asset_id)
    row = db.scalar(select(models.AssetSemanticRevision).where(models.AssetSemanticRevision.asset_id == asset.id,
        models.AssetSemanticRevision.tenant_id == tenant, models.AssetSemanticRevision.request_key == key))
    return {"found": row is not None, "result": service.semantic_read(db, asset, row) if row else None}


@router.post("/assets/{asset_id}/access")
def asset_access(asset_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                 gateway: StorageGateway = Depends(configured_storage_gateway)):
    asset = service.asset_for(db, tenant, asset_id)
    if not service.ready(asset) or not asset.kind.startswith(("video", "image")):
        raise EditingError("matching_asset_not_ready", "请先完成图片或视频的上传核验")
    from ..services.derived_rights import asset_playback
    value = asset_playback(db, asset, tenant_id=tenant, gateway=gateway)
    return {"url": value["url"], "expires_at": value["expires_at"]}


@router.post("/storyboards/{document_id}/match-reports")
def create_report(document_id: str, payload: schemas.MatchCreate, key: str = Depends(request_key),
                  tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                  identity: RequestIdentity = Depends(authenticate_request)):
    result = service.create_report(db, tenant, document_id, payload, key, identity.principal_id or "development_user")
    commit(db); return result


@router.get("/storyboards/{document_id}/match-reports")
def reports(document_id: str, skip: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
            tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = service.document_for(db, tenant, document_id)
    rows = db.scalars(select(models.ShotMatchReport).where(models.ShotMatchReport.document_id == document.id,
        models.ShotMatchReport.tenant_id == tenant).order_by(models.ShotMatchReport.created_at.desc(), models.ShotMatchReport.id)
        .offset(skip).limit(limit))
    return [service.report_read(db, document, row, detail=False) for row in rows]


@router.get("/storyboards/{document_id}/match-reports/{report_id}")
def report(document_id: str, report_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = service.document_for(db, tenant, document_id)
    return service.report_read(db, document, service.report_for(db, document, report_id))


@router.get("/storyboards/{document_id}/match-requests/{key}")
def report_receipt(document_id: str, key: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = service.document_for(db, tenant, document_id)
    row = db.scalar(select(models.ShotMatchReport).where(models.ShotMatchReport.document_id == document.id,
        models.ShotMatchReport.tenant_id == tenant, models.ShotMatchReport.request_key == key))
    return {"found": row is not None, "result": service.report_read(db, document, row) if row else None}


@router.get("/storyboards/{document_id}/sourcing")
def sourcing(document_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return service.source_read(db, service.document_for(db, tenant, document_id))


@router.post("/storyboards/{document_id}/sourcing")
def save_sourcing(document_id: str, payload: schemas.SourceSave, key: str = Depends(request_key),
                  tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                  identity: RequestIdentity = Depends(authenticate_request)):
    result = service.save_sources(db, tenant, document_id, payload, key, identity.principal_id or "development_user")
    commit(db); return result


@router.get("/storyboards/{document_id}/source-revisions")
def source_revisions(document_id: str, skip: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                     tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = service.document_for(db, tenant, document_id)
    rows = db.scalars(select(models.StoryboardSourceRevision).where(models.StoryboardSourceRevision.document_id == document.id,
        models.StoryboardSourceRevision.tenant_id == tenant).order_by(models.StoryboardSourceRevision.number.desc()).offset(skip).limit(limit))
    return [service.source_read(db, document, row) for row in rows]


@router.get("/storyboards/{document_id}/source-requests/{key}")
def source_receipt(document_id: str, key: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = service.document_for(db, tenant, document_id)
    row = db.scalar(select(models.StoryboardSourceRevision).where(models.StoryboardSourceRevision.document_id == document.id,
        models.StoryboardSourceRevision.tenant_id == tenant, models.StoryboardSourceRevision.request_key == key))
    return {"found": row is not None, "result": service.source_read(db, document, row) if row else None}


@router.get("/storyboards/{document_id}/reshoot-manifest")
def reshoot(document_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return service.reshoot_manifest(db, service.document_for(db, tenant, document_id))
