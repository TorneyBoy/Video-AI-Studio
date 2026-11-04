"""Personal, project-scoped AI production with durable confirmation receipts."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .deps import RequestIdentity, authenticate_request, require_tenant_id
from .identity import PrivateValidationRoute
from .storyboards import request_key
from .. import ai_production_schemas as schemas, models
from ..database import get_db
from ..schemas import AssetRead, AvatarProfileRead
from ..services import ai_production as service
from ..services.budgets import BudgetDataError
from ..services.capabilities import capability_catalog
from ..services.editing_errors import EditingError
from ..services.provider_registry import ProviderRegistryError
from ..services.scenario_profiles import list_scenario_profiles

router = APIRouter(prefix="/projects/{project_id}/ai-production", tags=["ai-production"],
                   route_class=PrivateValidationRoute)


def _commit(db):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise EditingError("ai_concurrent_write", "另一个请求正在保存，请查询原操作后继续", 409) from None


def _write(db, action):
    # A unique-key race can happen at flush, before commit. Re-read the
    # deterministic receipt once after rollback, preserving the same intent.
    try:
        result = action()
    except IntegrityError:
        db.rollback()
        result = action()
    _commit(db)
    return result


@router.post("/profiles", response_model=AvatarProfileRead, status_code=201)
def create_profile(project_id: str, payload: schemas.IdentityCreate, key: str = Depends(request_key),
                   tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                   identity: RequestIdentity = Depends(authenticate_request)):
    return _write(db, lambda: service.create_profile(db, tenant, project_id, payload, key,
                                                   identity.principal_id or "development_user"))


@router.get("/profiles/requests/{key}")
def profile_receipt(project_id: str, key: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    service.project_for(db, tenant, project_id)
    return service.profile_receipt(db, tenant, project_id, key)


@router.get("")
def overview(project_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    service.project_for(db, tenant, project_id)
    try:
        capabilities = [entry for entry in capability_catalog()["capabilities"]
                        if entry["capability"] in set(service.CAPABILITIES.values())]
    except (ProviderRegistryError, ValueError, OSError):
        capabilities = []
    profiles = list(db.scalars(select(models.AvatarProfile).where(models.AvatarProfile.tenant_id == tenant,
        models.AvatarProfile.project_id.in_([project_id]) | models.AvatarProfile.project_id.is_(None))
        .order_by(models.AvatarProfile.created_at.desc()).limit(100)))
    jobs = list(db.scalars(select(models.Job).where(models.Job.tenant_id == tenant,
        models.Job.project_id == project_id).order_by(models.Job.created_at.desc()).limit(100)))
    return {"capabilities": capabilities, "profiles": [AvatarProfileRead.model_validate(p) for p in profiles],
            "jobs": [service.job_read(db, j) for j in jobs if (j.payload or {}).get("ai_production")],
            "scenario_profiles": list_scenario_profiles()}


@router.post("/quotes", status_code=201)
def quote(project_id: str, payload: schemas.ProductionInput, key: str = Depends(request_key),
          tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    try:
        result = _write(db, lambda: service.create_quote(db, tenant, project_id, payload, key))
    except EditingError:
        raise
    except (ProviderRegistryError, ValueError, OSError) as exc:
        raise EditingError("ai_configuration_unavailable", "AI服务配置或价格尚未就绪", 409) from exc
    return result


@router.post("/jobs", status_code=201)
def submit(project_id: str, payload: schemas.ProductionConfirm, key: str = Depends(request_key),
           tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    try:
        result = _write(db, lambda: service.submit(db, tenant, project_id, payload.quote_id, key))
    except BudgetDataError as exc:
        db.rollback()
        raise EditingError(exc.code, "费用或预算不满足本次提交条件", 409) from exc
    return result


@router.get("/requests/{key}")
def receipt(project_id: str, key: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    service.project_for(db, tenant, project_id)
    return service.receipt(db, tenant, project_id, key)


@router.get("/jobs/{job_id}")
def task(project_id: str, job_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return service.job_read(db, service.job_for(db, tenant, project_id, job_id))


@router.post("/jobs/{job_id}/adopt", response_model=AssetRead)
def adopt(project_id: str, job_id: str, payload: schemas.ProductionAdopt, key: str = Depends(request_key),
          tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    return _write(db, lambda: service.adopt(db, tenant, project_id, job_id, key))
