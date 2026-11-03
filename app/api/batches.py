from datetime import datetime,timezone
from fastapi import APIRouter,Depends,Header,Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .billing import reply
from .deps import RequestIdentity,authenticate_request,require_tenant_id
from .identity import PrivateValidationRoute
from .. import models
from ..batch_schemas import BatchCreate,BatchSave,BatchQuoteRequest,BatchConfirmRequest
from ..database import get_db
from ..services import batches as service
from ..services.editing_errors import EditingError
from ..services.delivery_packages import utc

router=APIRouter(tags=['production-batches'],route_class=PrivateValidationRoute)


def valid_key(key):
    if not key.strip():raise EditingError('batch_key_required','缺少本次操作标识',422)


@router.get('/batches')
def listing(project_id: str,skip: int=Query(0,ge=0),limit: int=Query(30,ge=1,le=100),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    rows=list(db.scalars(select(models.ProductionBatch).where(models.ProductionBatch.tenant_id==tenant,models.ProductionBatch.project_id==project_id).order_by(models.ProductionBatch.created_at.desc(),models.ProductionBatch.id.desc()).offset(skip).limit(limit+1)))
    return reply({'items':[service.batch_read(db,row,include_items=False) for row in rows[:limit]],'has_more':len(rows)>limit,'next_skip':skip+min(len(rows),limit)})


@router.post('/batches')
def create(payload: BatchCreate,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),identity: RequestIdentity=Depends(authenticate_request),db: Session=Depends(get_db)):
    valid_key(key)
    try:row=service.create_batch(db,tenant,identity.user_id or identity.principal_id,payload,key);db.commit()
    except IntegrityError:
        db.rollback();row=service.create_batch(db,tenant,identity.user_id or identity.principal_id,payload,key);db.commit()
    return reply(service.batch_read(db,row))


@router.get('/batches/requests/{key}')
def created(key: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    row=db.scalar(select(models.ProductionBatch).where(models.ProductionBatch.tenant_id==tenant,models.ProductionBatch.idempotency_key==key))
    return reply({'found':row is not None,'batch':service.batch_read(db,row) if row else None})


@router.get('/batches/{batch_id}')
def read(batch_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    return reply(service.batch_read(db,service.batch_for(db,tenant,batch_id)))


@router.post('/batches/{batch_id}/configuration')
def save(batch_id: str,payload: BatchSave,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),identity: RequestIdentity=Depends(authenticate_request),db: Session=Depends(get_db)):
    valid_key(key);batch=service.batch_for(db,tenant,batch_id,lock=True)
    row=service.save_batch(db,batch,identity.user_id or identity.principal_id,payload,key);db.commit();return reply(service.revision_receipt(batch,row))


@router.get('/batches/{batch_id}/configuration/requests/{key}')
def saved(batch_id: str,key: str,base_revision: int | None=Query(default=None,ge=1),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=service.batch_for(db,tenant,batch_id,lock=True);row=db.scalar(select(models.BatchRevision).where(models.BatchRevision.batch_id==batch.id,models.BatchRevision.idempotency_key==key))
    result=service.revision_receipt(batch,row);result['can_abandon']=not row and base_revision is not None and (batch.revision!=base_revision or batch.state!='draft')
    return reply(result)


@router.post('/batches/{batch_id}/validate')
def validate(batch_id: str,payload: BatchQuoteRequest,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=service.batch_for(db,tenant,batch_id)
    if batch.revision!=payload.base_revision:raise EditingError('batch_revision_conflict','批次版本已变化，请载入当前配置',409)
    report,_=service.resolve(db,batch);return reply(report)


@router.post('/batches/{batch_id}/quotes')
def quote(batch_id: str,payload: BatchQuoteRequest,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    valid_key(key);batch=service.batch_for(db,tenant,batch_id,lock=True)
    row=service.create_quote(db,batch,payload.base_revision,key);db.commit();return reply(service.quote_read(db,batch,row))


@router.get('/batches/{batch_id}/quotes/{quote_id}')
def read_quote(batch_id: str,quote_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=service.batch_for(db,tenant,batch_id);return reply(service.quote_read(db,batch,service.quote_for(db,batch,quote_id)))


@router.post('/batches/{batch_id}/confirm')
def confirm(batch_id: str,payload: BatchConfirmRequest,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    valid_key(key);batch=service.batch_for(db,tenant,batch_id,lock=True);row=service.quote_for(db,batch,payload.quote_id,lock=True)
    try:result=service.confirm_quote(db,batch,row,payload.base_revision,key);db.commit()
    except IntegrityError as exc:
        db.rollback();raise EditingError('batch_confirm_conflict','批次状态或操作标识已变化，请核对原提交',409) from exc
    return reply(result)


@router.get('/batches/{batch_id}/requests/{key}')
def confirmed(batch_id: str,key: str,quote_id: str | None=Query(default=None,min_length=1,max_length=64),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=service.batch_for(db,tenant,batch_id,lock=True);quote=db.scalar(select(models.BatchQuote).where(models.BatchQuote.batch_id==batch.id,models.BatchQuote.consumed_key==key))
    abandoned=False
    if quote is None and quote_id:
        requested=service.quote_for(db,batch,quote_id,lock=True)
        abandoned=not requested.consumed_key and (utc(requested.expires_at)<=datetime.now(timezone.utc) or batch.state!='draft' or batch.revision!=requested.revision)
    return reply({'found':quote is not None,'result':quote.result_json if quote else None,'can_abandon':abandoned})
