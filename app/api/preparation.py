from fastapi import APIRouter,Depends,Header,Body
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .deps import require_tenant_id
from .identity import PrivateValidationRoute
from .billing import reply
from .. import models,schemas
from ..billing_schemas import AnalysisPreparationCreate
from ..database import get_db
from ..services import preparation_quotes as service
from ..services.editing_errors import EditingError

router=APIRouter(tags=['preparation-quotes'],route_class=PrivateValidationRoute)


def get_quote(db,tenant,id):
    row=db.scalar(select(models.PreparationQuote).where(models.PreparationQuote.id==id,models.PreparationQuote.tenant_id==tenant))
    if row is None or row.kind not in {'analysis','plan'}:raise EditingError('preparation_quote_missing','未找到当前空间的报价',404)
    return row


def create(db,tenant,kind,payload,key):
    if not key.strip():raise EditingError('quote_key_required','缺少本次报价标识',422)
    try:row=service.create_quote(db,tenant,kind,payload,key);db.commit()
    except IntegrityError:
        db.rollback();row=service.create_quote(db,tenant,kind,payload,key);db.commit()
    return reply(service.quote_read(db,row))


@router.post('/preparation/analysis-quotes')
def analyses(payload: AnalysisPreparationCreate,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    return create(db,tenant,'analysis',payload,key)


@router.post('/preparation/plan-quotes')
def planning(payload: schemas.AutoEditCreate,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    if payload.quote_id:raise EditingError('quote_request_invalid','获取新报价时不能指定旧报价',422)
    return create(db,tenant,'plan',payload,key)


@router.get('/preparation/quotes/{quote_id}')
def read(quote_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    return reply(service.quote_read(db,get_quote(db,tenant,quote_id)))


@router.post('/preparation/quotes/{quote_id}/confirm')
def confirm(quote_id: str,payload: dict=Body(default_factory=dict),key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    if payload or not key.strip():raise EditingError('preparation_confirm_invalid','确认只使用原报价与本次提交标识，不能附带其他设置',422)
    get_quote(db,tenant,quote_id)
    row=service.consume_quote(db,tenant,quote_id,key);db.commit();return reply(service.public_result(db,row))


@router.get('/preparation/quotes/{quote_id}/requests/{key}')
def receipt(quote_id: str,key: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    row=get_quote(db,tenant,quote_id)
    return reply({'found':row.consumed_key==key,'result':service.public_result(db,row) if row.consumed_key==key else None,'status':service.quote_read(db,row)['status']})
