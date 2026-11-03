"""Personal costs, project ceilings, bound quotes and original-task recovery."""
from fastapi import APIRouter,Depends,Header,Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .deps import RequestIdentity,authenticate_request,ensure_project,require_tenant_id
from .identity import PrivateValidationRoute
from .. import models,schemas
from ..billing_schemas import JobActionRequest,ProjectLimitUpdate
from ..database import get_db
from ..services import billing_workbench as service,project_spending,render_quotes
from ..services.editing import document_for_tenant,revision_for_document,timeline_for_revision
from ..services.editing_errors import EditingError

router=APIRouter(tags=['billing-workbench'],route_class=PrivateValidationRoute)


def reply(value):
    def money_strings(item):
        if isinstance(item,list):return [money_strings(row) for row in item]
        if isinstance(item,dict):return {key:str(val) if key.endswith('_micros') and isinstance(val,int) else money_strings(val) for key,val in item.items()}
        return item
    return JSONResponse(jsonable_encoder(money_strings(value)),headers={'Cache-Control':'no-store'})


def job_for(db,tenant,id):
    row=db.scalar(select(models.Job).where(models.Job.id==id,models.Job.tenant_id==tenant))
    if not row:raise EditingError('job_not_found','未找到当前空间的任务',404)
    return row


def timeline_for(db,tenant,id):
    row=db.scalar(select(models.Timeline).where(models.Timeline.id==id,models.Timeline.tenant_id==tenant))
    if not row:raise EditingError('timeline_not_found','未找到当前空间的时间线',404)
    return row


@router.get('/billing/overview')
def cost_overview(project_id: str | None=None,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db),identity: RequestIdentity=Depends(authenticate_request)):
    if project_id:ensure_project(db,project_id,tenant)
    return reply(service.overview(db,tenant,project_id,identity.principal_id))


@router.get('/billing/entries')
def entries(project_id: str | None=None,skip: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    if project_id:ensure_project(db,project_id,tenant)
    return reply(service.ledger_page(db,tenant,project_id,skip,limit))


@router.get('/billing/jobs')
def jobs(project_id: str | None=None,state: str='all',skip: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    if project_id:ensure_project(db,project_id,tenant)
    return reply(service.jobs_page(db,tenant,project_id,state,skip,limit))


@router.get('/billing/jobs/{job_id}')
def job_state(job_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    return reply(service.job_view(job_for(db,tenant,job_id)))


@router.put('/projects/{project_id}/spending-limit')
@router.post('/projects/{project_id}/spending-limit')
def spending_limit(project_id: str,payload: ProjectLimitUpdate,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db),identity: RequestIdentity=Depends(authenticate_request)):
    ensure_project(db,project_id,tenant);value=project_spending.update_limit(db,tenant,project_id,identity.principal_id,payload)
    db.commit();return reply(value)


@router.post('/billing/jobs/{job_id}/actions')
def task_action(job_id: str,payload: JobActionRequest,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),
                tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db),identity: RequestIdentity=Depends(authenticate_request)):
    job=job_for(db,tenant,job_id);row=service.apply_action(db,job,identity.principal_id,key,payload)
    db.commit();return reply(service.action_receipt(db,job,row))


@router.get('/billing/jobs/{job_id}/actions/{key}')
def task_receipt(job_id: str,key: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    job=job_for(db,tenant,job_id)
    row=db.scalar(select(models.JobAction).where(models.JobAction.job_id==job.id,models.JobAction.idempotency_key==key))
    return reply(service.action_receipt(db,job,row))


def new_quote(db,row,tenant,payload,key):
    if not key.strip():raise EditingError('quote_key_required','缺少本次报价标识',422)
    try:quote=render_quotes.create_quote(db,row,tenant,payload,key);db.commit()
    except IntegrityError:
        db.rollback();quote=render_quotes.create_quote(db,row,tenant,payload,key);db.commit()
    return reply(render_quotes.quote_read(db,quote))


@router.post('/timelines/{timeline_id}/render-quote')
def timeline_quote(timeline_id: str,payload: schemas.RenderRequest,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    return new_quote(db,timeline_for(db,tenant,timeline_id),tenant,payload,key)


@router.post('/editing/{document_id}/revisions/{revision_id}/render-quote')
def revision_quote(document_id: str,revision_id: str,payload: schemas.RenderRequest,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    document=document_for_tenant(db,tenant,document_id);revision=revision_for_document(db,document,revision_id)
    timeline=timeline_for_revision(db,revision)
    return new_quote(db,timeline_for(db,tenant,timeline.timeline_id),tenant,payload,key)


@router.post('/jobs/{job_id}/render-quote')
def plan_quote(job_id: str,payload: schemas.RenderRequest,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    job=job_for(db,tenant,job_id)
    if job.type!='auto_edit.plan' or job.status!='succeeded':raise EditingError('quote_plan_unavailable','请先完成并保存剪辑计划',409)
    return new_quote(db,timeline_for(db,tenant,str((job.payload or {}).get('timeline_id') or '')),tenant,payload,key)


@router.get('/billing/render-quotes/{quote_id}')
def read_quote(quote_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    quote=db.scalar(select(models.RenderQuote).where(models.RenderQuote.id==quote_id,models.RenderQuote.tenant_id==tenant))
    if not quote:raise EditingError('quote_not_found','未找到报价',404)
    return reply(render_quotes.quote_read(db,quote))
