"""Bounded admission and durable hierarchical round robin, without media IO.

Admission locks Clock -> Job -> Workflow and commits before yielding work.
Provider admission locks Job -> Workflow -> Tenant/Budget -> ProviderGate and commits before IO.
Completion never locks Clock or ProviderGate, and neither gate locks another Job.
"""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
import hashlib
import json
import math
from sqlalchemy import JSON,case,cast,func,literal,or_,select,and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased
from .. import models
from ..config import get_settings
from .jobs import claim_job

FAMILIES=('analysis','plan','provider','render','package')
UNRESOLVED=('submitting','unknown','pending')


class DispatchDeferred(Exception):
    """No provider submission occurred; wait without consuming a retry."""


def family_expression():
    return case((models.Job.type=='delivery.package','package'),(models.Job.type=='asset.analyze','analysis'),(models.Job.type=='auto_edit.plan','plan'),
        (models.Job.type.like('render.%'),'render'),
        (or_(models.Job.type.like('voice.%'),models.Job.type.like('avatar.%'),models.Job.type=='video.generate',models.Job.type.like('enhancement.%')),'provider'),else_=None)


def family_for(job):
    if job.type=='delivery.package':return 'package'
    if job.type=='asset.analyze':return 'analysis'
    if job.type=='auto_edit.plan':return 'plan'
    if job.type.startswith('render.'):return 'render'
    return 'provider'


def _insert_once(db,table,values):
    insert=pg_insert if db.get_bind().dialect.name=='postgresql' else sqlite_insert
    db.execute(insert(table).values(**values).on_conflict_do_nothing())


def batch_expression():
    # Validate the ownership of payload references, including pre-0017 plans
    # whose server-owned parent ID predates the explicit batch_id field.
    batch=models.ProductionBatch;quote=models.BatchQuote
    direct=select(func.min(batch.id)).where(batch.tenant_id==models.Job.tenant_id,batch.project_id==models.Job.project_id,
        or_(batch.id==models.Job.payload['batch_id'].as_string(),batch.parent_job_id==models.Job.payload['parent_job_id'].as_string())).correlate(models.Job).scalar_subquery()
    analysis=select(func.min(quote.batch_id)).join(batch,batch.id==quote.batch_id).where(batch.tenant_id==models.Job.tenant_id,
        batch.project_id==models.Job.project_id,quote.analysis_quote_id==models.Job.payload['preparation_quote_id'].as_string(),quote.consumed_key.is_not(None)).correlate(models.Job).scalar_subquery()
    return func.coalesce(direct,analysis,'')


def remote_occupancy():
    operation=models.ProviderOperation
    unresolved=select(operation.id).where(operation.job_id==models.Job.id,operation.state.in_(UNRESOLVED)).exists()
    absent=~select(operation.id).where(operation.job_id==models.Job.id).exists()
    ended=select(models.ProviderEvent.id).where(models.ProviderEvent.job_id==models.Job.id,
        models.ProviderEvent.status.in_(('succeeded','failed','cancelled'))).exists()
    legacy=or_(func.length(func.trim(models.Job.payload['provider_job_id'].as_string()))>0,
        func.length(func.trim(models.Job.payload['provider_failure']['details']['provider_job_id'].as_string()))>0)
    packaging=select(models.DeliveryPackage.id).where(models.DeliveryPackage.job_id==models.Job.id,models.DeliveryPackage.remote_state.in_(('queued','running','unknown'))).exists()
    return or_(unresolved,and_(absent,legacy,~ended,models.Job.status!='succeeded'),packaging)


def _ready_dependencies(db):
    source=models.Job.payload['analysis_job_ids']
    if db.get_bind().dialect.name=='postgresql':
        safe=case((func.json_typeof(source)=='array',source),else_=cast(literal('[]'),JSON))
        deps=func.json_array_elements_text(safe).table_valued('value').render_derived(name='dependencies')
    else:
        safe=case((func.json_type(source)=='array',source),else_=literal('[]'))
        deps=func.json_each(safe).table_valued('value').alias('dependencies')
    other=aliased(models.Job)
    blocked=select(literal(1)).select_from(deps).join(other,other.id==deps.c.value).where(other.tenant_id==models.Job.tenant_id,
        other.status.in_(('queued','running','processing'))).correlate(models.Job).exists()
    return or_(models.Job.type!='auto_edit.plan',~blocked)


def active_rows():
    return select(models.Job.id,models.Job.tenant_id,func.coalesce(models.Job.project_id,'').label('project_key'),
        batch_expression().label('batch_key')).where(family_expression().is_not(None),or_(models.Job.status=='running',remote_occupancy())).cte('active_media')


def _candidates(db,families,excluded,settings):
    job=models.Job;turn=models.DispatchTurn;now=datetime.now(timezone.utc);active=active_rows()
    tenant_active=select(active.c.tenant_id,func.count().label('n')).group_by(active.c.tenant_id).subquery()
    project_active=select(active.c.tenant_id,active.c.project_key,func.count().label('n')).group_by(active.c.tenant_id,active.c.project_key).subquery()
    batch_active=select(active.c.tenant_id,active.c.batch_key,func.count().label('n')).where(active.c.batch_key!='').group_by(active.c.tenant_id,active.c.batch_key).subquery()
    foreign=select(models.WorkflowRun.id).where(models.WorkflowRun.job_id==job.id,models.WorkflowRun.runtime!='local').exists()
    base=select(job.id,job.tenant_id,func.coalesce(job.project_id,'').label('project_key'),batch_expression().label('batch_key'),
        family_expression().label('family'),case((remote_occupancy(),'continuation'),else_='new').label('mode'),job.created_at).where(
        job.status=='queued',family_expression().in_(families),or_(job.next_retry_at.is_(None),job.next_retry_at<=now),
        ~foreign,_ready_dependencies(db),job.id.not_in(excluded)).cte('ready_media')
    q=base.c
    def history(*keys):return select(*(getattr(turn,key) for key in keys),func.max(turn.last_turn).label('served')).group_by(*(getattr(turn,key) for key in keys)).subquery()
    tenant_turn=history('tenant_id');project_turn=history('tenant_id','project_key');batch_turn=history('tenant_id','project_key','batch_key');family_turn=history('tenant_id','project_key','batch_key','family')
    stmt=select(q.id,q.tenant_id,q.project_key,q.batch_key,q.family,q.mode).select_from(base)
    for table in (tenant_active,project_active,batch_active,tenant_turn,project_turn,batch_turn,family_turn):
        keys=[key for key in ('tenant_id','project_key','batch_key','family') if key in table.c]
        stmt=stmt.outerjoin(table,and_(*(getattr(table.c,key)==getattr(q,key) for key in keys)))
    stmt=stmt.outerjoin(turn,and_(*(getattr(turn,key)==getattr(q,key) for key in ('tenant_id','project_key','batch_key','family','mode'))))
    stmt=stmt.outerjoin(models.JobDispatchStamp,models.JobDispatchStamp.job_id==q.id)
    capacity=and_(select(func.count()).select_from(active).scalar_subquery()<settings.dispatch_global_limit,
        func.coalesce(tenant_active.c.n,0)<settings.dispatch_tenant_limit,func.coalesce(project_active.c.n,0)<settings.dispatch_project_limit,
        or_(q.batch_key=='',func.coalesce(batch_active.c.n,0)<settings.dispatch_batch_limit))
    return stmt.where(or_(q.id.in_(select(active.c.id)),capacity)).order_by(
        *(func.coalesce(table.c.served,0) for table in (tenant_turn,project_turn,batch_turn,family_turn)),
        func.coalesce(turn.last_turn,0),func.coalesce(models.JobDispatchStamp.last_turn,0),q.created_at,q.id).limit(1)


def claim_next(db,*,families,workflow_claim,excluded=(),settings=None):
    """Claim one ready Job + Workflow and release the admission transaction."""
    from .recovery_guard import database_active
    if not database_active(db):return None
    settings=settings or get_settings();skipped=set(excluded)
    while True:
        _insert_once(db,models.DispatchClock,{'id':1,'turn':0})
        clock=db.scalar(select(models.DispatchClock).where(models.DispatchClock.id==1).with_for_update().execution_options(populate_existing=True))
        row=db.execute(_candidates(db,families,skipped,settings)).first()
        if row is None:db.rollback();return None
        job=db.scalar(select(models.Job).where(models.Job.id==row.id,models.Job.status=='queued').with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if job is None:skipped.add(row.id);continue
        deadline=job.next_retry_at
        if not claim_job(db,job):skipped.add(row.id);continue
        run=workflow_claim(db,job,retry_deadline=deadline)
        if run is None:db.rollback();skipped.add(row.id);continue
        clock.turn+=1
        identity={key:getattr(row,key) for key in ('tenant_id','project_key','batch_key','family','mode')}
        id=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest();lane=db.get(models.DispatchTurn,id)
        if lane is None:lane=models.DispatchTurn(id=id,**identity);db.add(lane)
        lane.last_turn=clock.turn;stamp=db.get(models.JobDispatchStamp,job.id)
        if stamp is None:stamp=models.JobDispatchStamp(job_id=job.id);db.add(stamp)
        stamp.last_turn=clock.turn;db.commit();return job,run


def claims(db,*,families,limit,workflow_claim,settings=None):
    seen=set()
    for _ in range(max(0,limit)):
        value=claim_next(db,families=families,workflow_claim=workflow_claim,excluded=seen,settings=settings)
        if value is None:break
        seen.add(value[0].id);yield value


def provider_gate(db,job,selected,capability,*,continuation=False):
    """Enforce the selected provider's actual capability cap, all tenants."""
    # Polling an existing identity does not admit a new operation and must not
    # depend on the current catalogue, limits or pricing remaining available.
    if continuation:return
    entries=getattr(selected,'capabilities',None)
    if entries is None:
        from .providers import ProviderExecutionError
        raise ProviderExecutionError('selected provider has no capability contract',code='provider_concurrency_invalid',retryable=False)
    limits=next((entry.limits for entry in entries if entry.name==capability),{})
    value=limits.get('max_concurrent',get_settings().dispatch_global_limit)
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 or int(value)!=value:
        from .providers import ProviderExecutionError
        raise ProviderExecutionError('invalid provider concurrency limit',code='provider_concurrency_invalid',retryable=False)
    id=hashlib.sha256(json.dumps([selected.name,capability]).encode()).hexdigest()
    _insert_once(db,models.ProviderDispatchGate,{'id':id,'provider':selected.name,'capability':capability})
    db.scalar(select(models.ProviderDispatchGate).where(models.ProviderDispatchGate.id==id).with_for_update().execution_options(populate_existing=True))
    count=db.scalar(select(func.count()).select_from(models.ProviderOperation).where(
        or_(models.ProviderOperation.provider_key==selected.name,models.ProviderOperation.provider_key.is_(None)),
        or_(models.ProviderOperation.capability==capability,models.ProviderOperation.capability.is_(None)),
        models.ProviderOperation.state.in_(UNRESOLVED),models.ProviderOperation.job_id!=job.id))
    # Pre-operation legacy retries can still represent a live remote task.
    # Count their original identity until a trusted terminal event arrives.
    p=models.Job.payload
    legacy_provider=func.coalesce(p['capability_snapshot']['provider'].as_string(),p['provider_failure']['provider'].as_string(),p['provider_request']['provider'].as_string())
    legacy_capability=func.coalesce(p['capability_snapshot']['capability'].as_string(),p['provider_failure']['capability'].as_string(),p['provider_request']['capability'].as_string(),
        case((models.Job.type=='asset.analyze','media_analysis'),(models.Job.type.like('render.%'),'video_editing'),else_=None))
    count+=db.scalar(select(func.count()).select_from(models.Job).where(models.Job.id!=job.id,family_expression().is_not(None),family_expression()!='package',remote_occupancy(),
        ~select(models.ProviderOperation.id).where(models.ProviderOperation.job_id==models.Job.id).exists(),
        or_(legacy_provider==selected.name,legacy_provider.is_(None)),or_(legacy_capability==capability,legacy_capability.is_(None))))
    if count>=int(value):raise DispatchDeferred('provider_capacity')


def defer_execution(db,job,ticket,*,delay=3,reason='provider_capacity'):
    """Capacity waiting is neither a failed provider call nor a new retry."""
    from .completion import lock_execution
    from .workflows import append_event
    run=lock_execution(db,job,ticket);now=datetime.now(timezone.utc)
    job.status='queued';job.attempt_count=max(0,job.attempt_count-1);job.next_retry_at=now+timedelta(seconds=delay);job.completed_at=None
    if not job.attempt_count:job.started_at=None
    run.status='queued';run.attempt_count=max(0,run.attempt_count-1);run.next_retry_at=job.next_retry_at;run.worker_id=None;run.lease_expires_at=None;run.heartbeat_at=now
    if not run.attempt_count:run.started_at=None
    for step in db.scalars(select(models.JobStep).where(models.JobStep.job_id==job.id,models.JobStep.status=='running')):
        step.status='queued';step.progress=0;step.started_at=None;step.completed_at=None
    for step in db.scalars(select(models.WorkflowStep).where(models.WorkflowStep.workflow_run_id==run.id,models.WorkflowStep.status=='running')):
        step.status='queued';step.attempt_count=max(0,step.attempt_count-1);step.started_at=None;step.worker_id=None;step.lease_expires_at=None;step.progress=0
    append_event(db,run,'workflow.dispatch_wait',payload={'reason':reason,'delay_seconds':delay});db.commit()


def batch_status(db,batch):
    active=active_rows();settings=get_settings()
    count=db.scalar(select(func.count()).select_from(active).where(active.c.tenant_id==batch.tenant_id,active.c.batch_key==batch.id))
    return {'active':count,'batch_limit':settings.dispatch_batch_limit,
        'note':'按项目与批次轮流推进；排队等待执行容量不消耗重试次数。已提交远端且尚未确认结束的任务继续占用容量。'}
