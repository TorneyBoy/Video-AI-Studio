from datetime import datetime,timezone
from fastapi import APIRouter,Depends,Header,Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .billing import reply
from .batches import valid_key
from .deps import RequestIdentity,authenticate_request,require_tenant_id
from .identity import PrivateValidationRoute
from .. import models
from ..batch_schemas import BatchRenderCreate,BatchRenderConfirm,BatchTaskRequest
from ..database import get_db
from ..services import batches,batch_runs as service
from ..services.delivery_packages import utc
from ..services.editing_errors import EditingError

router=APIRouter(tags=['batch-runs'],route_class=PrivateValidationRoute)


@router.post('/batches/{batch_id}/render-quotes')
def quote(batch_id: str,payload: BatchRenderCreate,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    valid_key(key);batch=batches.batch_for(db,tenant,batch_id,lock=True);row=service.create_quote(db,batch,payload,key);db.commit();return reply(service.quote_read(db,batch,row))


@router.get('/batches/{batch_id}/render-quotes/{quote_id}')
def read_quote(batch_id: str,quote_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=batches.batch_for(db,tenant,batch_id);return reply(service.quote_read(db,batch,service.quote_for(db,batch,quote_id)))


@router.post('/batches/{batch_id}/render-confirm')
def confirm(batch_id: str,payload: BatchRenderConfirm,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    valid_key(key);batch=batches.batch_for(db,tenant,batch_id,lock=True);row=service.quote_for(db,batch,payload.quote_id,lock=True)
    try:result=service.confirm_quote(db,batch,row,key);db.commit()
    except IntegrityError as exc:
        db.rollback();raise EditingError('batch_render_conflict','批次状态或提交标识已变化，请核对原操作',409) from exc
    return reply(result)


@router.get('/batches/{batch_id}/render-requests/{key}')
def confirmed(batch_id: str,key: str,quote_id: str | None=Query(default=None,min_length=1,max_length=64),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=batches.batch_for(db,tenant,batch_id,lock=True);quote=db.scalar(select(models.BatchRenderQuote).where(models.BatchRenderQuote.batch_id==batch.id,models.BatchRenderQuote.consumed_key==key));abandoned=False
    if not quote and quote_id:
        requested=service.quote_for(db,batch,quote_id,lock=True);abandoned=not requested.consumed_key and utc(requested.expires_at)<=datetime.now(timezone.utc)
    return reply({'found':quote is not None,'result':quote.result_json if quote else None,'can_abandon':abandoned})


@router.get('/batches/{batch_id}/runs')
def listing(batch_id: str,skip: int=Query(default=0,ge=0),limit: int=Query(default=20,ge=1,le=100),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=batches.batch_for(db,tenant,batch_id);rows=list(db.scalars(select(models.BatchRun).where(models.BatchRun.batch_id==batch.id,models.BatchRun.tenant_id==tenant).order_by(models.BatchRun.created_at.desc(),models.BatchRun.id.desc()).offset(skip).limit(limit+1)))
    return reply({'items':[service.run_read(db,batch,row) for row in rows[:limit]],'has_more':len(rows)>limit,'next_skip':skip+min(limit,len(rows))})


@router.get('/batches/{batch_id}/runs/{run_id}')
def read(batch_id: str,run_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=batches.batch_for(db,tenant,batch_id);return reply(service.run_read(db,batch,service.run_for(db,batch,run_id)))


@router.post('/batches/{batch_id}/phases/{phase_id}/actions')
def actions(batch_id: str,phase_id: str,payload: BatchTaskRequest,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),identity: RequestIdentity=Depends(authenticate_request),db: Session=Depends(get_db)):
    valid_key(key);batch=batches.batch_for(db,tenant,batch_id,lock=True)
    try:row=service.apply_actions(db,batch,phase_id,identity.user_id or identity.principal_id,key,payload);db.commit()
    except IntegrityError as exc:
        db.rollback();raise EditingError('batch_task_action_conflict','原操作标识已用于其他批次，请核对',409) from exc
    return reply(row.result_json)


@router.get('/batches/{batch_id}/phases/{phase_id}/actions/requests/{key}')
def acted(batch_id: str,phase_id: str,key: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch=batches.batch_for(db,tenant,batch_id);row=db.scalar(select(models.BatchTaskAction).where(models.BatchTaskAction.tenant_id==tenant,models.BatchTaskAction.batch_id==batch.id,models.BatchTaskAction.phase_id==phase_id,models.BatchTaskAction.idempotency_key==key))
    return reply({'found':row is not None,'result':row.result_json if row else None})
