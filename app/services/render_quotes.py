"""Version/input/price-bound reservation quotes; creating one never dispatches work."""
from datetime import datetime,timedelta,timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from .. import models,schemas
from ..config import get_settings
from .budgets import (_decision,_sum_ledger,_RESERVATION_STATUSES,capability_reservation,get_budget,normalize_cost,period_key)
from .editing import (digest,document_for_tenant,revision_for_document,assert_render_sources)
from .editing_errors import EditingError
from .delivery_packages import utc
from .render_features import require_render_support
from timeline_engine import Timeline,TemplateDefinition,validate_timeline


def request_hash(timeline_id: str,binding: dict,payload: schemas.RenderRequest) -> str:
    return digest({'timeline_id':timeline_id,'binding':binding,'request':{
        **payload.model_dump(mode='json',exclude={'max_attempts','quote_id'}),
        'options':{**payload.options,'quality':payload.quality}}})


def quote_context(db: Session,row: models.Timeline,tenant: str,payload: schemas.RenderRequest):
    frozen=db.scalar(select(models.TimelineRevision).where(models.TimelineRevision.timeline_id==row.id,models.TimelineRevision.tenant_id==tenant))
    binding={};revision=None
    if frozen:
        document=document_for_tenant(db,tenant,frozen.document_id);revision=revision_for_document(db,document,frozen.id)
        frozen=assert_render_sources(db,document,revision)
        template=TemplateDefinition.model_validate(revision.template_json);counts={}
        for item in revision.plan_json.get('selected',[]):counts.setdefault(item['slot'],set()).add(item['asset_id'])
        if any(slot.required and len(counts.get(slot.name,set()))<max(1,slot.constraints.min_items) for slot in template.slots):
            raise EditingError('edit_required_slots_missing','必需镜头尚未补齐，请先调整计划',422)
        binding={'edit_document_id':document.id,'plan_revision_id':revision.id,'timeline_revision_id':revision.id,
                 'plan_hash':revision.content_hash,'timeline_hash':frozen.content_hash}
    try:canonical=Timeline.model_validate(frozen.timeline_json if frozen else row.timeline_json)
    except ValueError as exc:raise EditingError('quote_timeline_invalid','时间线尚未通过校验，请先保存有效版本',422) from exc
    if not validate_timeline(canonical).valid:raise EditingError('quote_timeline_invalid','时间线尚未通过校验，请先保存有效版本',422)
    from .derived_rights import assert_timeline_rights
    assert_timeline_rights(db,tenant_id=tenant,project_id=row.project_id,timeline=canonical)
    selected=require_render_support(canonical.model_dump(mode='json'),require_price=True)
    if not selected:raise EditingError('quote_price_unavailable','当前服务未提供已验收且带版本的报价，不能按零费用提交',422)
    limits=selected.get('limits') or {};bound=limits.get('max_output_duration_seconds') or limits.get('max_duration_seconds')
    sizes=limits.get('resolutions');formats=limits.get('formats')
    if ((bound is not None and canonical.duration>bound)
            or sizes and canonical.resolution.model_dump(mode='json') not in sizes
            or formats and payload.output.get('format','mp4') not in formats):
        raise EditingError('quote_input_limits','当前服务不支持该时长、画幅或输出格式，请修改后重新报价',422)
    return canonical,binding,selected,revision


def affordability(db: Session,tenant: str,project: str,currency: str,amount: int):
    budget=get_budget(db,tenant);period=period_key()
    stub=models.Job(id='quotation-not-a-job',tenant_id=tenant,project_id=project,type='render.quote')
    return _decision(budget=budget,amount_micros=amount,currency=currency,
        monthly_used_micros=_sum_ledger(db,tenant_id=tenant,period=period,statuses=('settled',)),
        reserved_micros=_sum_ledger(db,tenant_id=tenant,period=period,statuses=_RESERVATION_STATUSES),
        period=period,job_id=stub.id,db=db,job=stub)


def quote_read(db: Session,row: models.RenderQuote) -> dict:
    expired=utc(row.expires_at)<=datetime.now(timezone.utc)
    decision=affordability(db,row.tenant_id,row.project_id,row.currency,row.amount_micros)
    job=db.scalar(select(models.Job.id).where(models.Job.tenant_id==row.tenant_id,models.Job.idempotency_key==row.consumed_key)) if row.consumed_key else None
    return {'id':row.id,'timeline_id':row.timeline_id,'input_hash':row.input_hash,'project_id':row.project_id,
        'summary':row.summary_json,'amount_micros':row.amount_micros,'currency':row.currency,
        'price_version':row.capability_json['price']['version'],'provider':row.capability_json['provider'],
        'model_id':row.capability_json['model_id'],'model_version':row.capability_json['model_version'],
        'duration_bound_seconds':capability_reservation(row.capability_json)['duration_bound_seconds'],
        'status':'consumed' if row.consumed_key else 'expired' if expired else 'ready',
        'expires_at':utc(row.expires_at),'job_id':job,'affordable':decision.allowed,'reasons':decision.audit['reasons'],
        'can_submit':not row.summary_json.get('batch_quote_id') and not expired and not row.consumed_key and decision.allowed,
        'basis':'server_capability_reservation','bill_confirmed':False,
        'note':'这是按服务允许的最长时长计算的预占上限，不是实付账单；后续账单可能不同，提交时重新核对版本、价格及预算。'}


def create_quote(db: Session,row: models.Timeline,tenant: str,payload: schemas.RenderRequest,key: str):
    canonical,binding,selected,revision=quote_context(db,row,tenant,payload)
    fingerprint=request_hash(row.id,binding,payload)
    previous=db.scalar(select(models.RenderQuote).where(models.RenderQuote.tenant_id==tenant,models.RenderQuote.idempotency_key==key))
    if previous:
        if previous.input_hash!=fingerprint or previous.capability_hash!=digest(selected):raise EditingError('quote_request_conflict','原报价操作已用于其他输入或价格，请明确刷新报价',409)
        return previous
    envelope=capability_reservation(selected);amount,currency=normalize_cost(envelope)
    result=models.RenderQuote(tenant_id=tenant,project_id=row.project_id,timeline_id=row.id,idempotency_key=key,
        input_hash=fingerprint,capability_hash=digest(selected),capability_json=selected,currency=currency,amount_micros=amount,
        summary_json={'quality':payload.quality,'duration':canonical.duration,'fps':canonical.fps,'timeline_hash':digest(canonical.model_dump(mode='json')),
                      'width':canonical.resolution.width,'height':canonical.resolution.height,'revision_number':revision.number if revision else None},
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=10))
    db.add(result);db.flush();return result


def consume_quote(db: Session,row: models.Timeline,tenant: str,payload: schemas.RenderRequest,key: str | None,
                  fingerprint: str,selected: dict | None,canonical: dict,*,batch_quote_id: str | None=None):
    if payload.quote_id is None and get_settings().auth_mode=='development_header':return None
    if not payload.quote_id or not key:raise EditingError('render_quote_required','请先获取报价，再确认本次提交',422)
    quote=db.scalar(select(models.RenderQuote).where(models.RenderQuote.id==payload.quote_id,models.RenderQuote.tenant_id==tenant).with_for_update().execution_options(populate_existing=True))
    if not quote or quote.timeline_id!=row.id or quote.project_id!=row.project_id:raise EditingError('render_quote_missing','未找到本次输出的报价',404)
    if quote.summary_json.get('batch_quote_id')!=batch_quote_id:raise EditingError('render_quote_scope','请从原批次确认本次整批报价',409)
    if (quote.input_hash!=fingerprint or quote.capability_hash!=digest(selected)
            or quote.summary_json.get('timeline_hash')!=digest(canonical)):
        raise EditingError('render_quote_changed','输入版本、导出设置或服务价格已变化，请重新获取报价',409)
    if quote.consumed_key or utc(quote.expires_at)<=datetime.now(timezone.utc):raise EditingError('render_quote_expired','报价已使用或过期，请核对原提交或重新获取报价',409)
    quote.consumed_key=key
    return quote.id
