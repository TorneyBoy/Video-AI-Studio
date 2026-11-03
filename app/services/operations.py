"""Read-only operational projections and explicitly evaluated local alerts.

No supplier calls, media IO, credentials, payloads or exception strings.
"""
from datetime import datetime,timedelta,timezone
import re
from typing import get_args
from sqlalchemy import and_,case,func,or_,select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sq_insert
from .. import models
from ..config import get_settings
from .dispatch import FAMILIES,UNRESOLVED,family_expression,remote_occupancy
from .runtime_logging import failure_code,fingerprint,opaque_id,job_family

ALERTS={
    'queue_aging':('warning','可执行队列长时间未推进，核对Worker、容量和依赖'),
    'workers_unobserved':('critical','有到期任务但没有新鲜Worker心跳，核对运行进程'),
    'worker_identity_collision':('warning','同一Worker标识有多个活跃实例，请为副本配置独立标识'),
    'workflow_lease_expired':('warning','工作流租约过期，先核对原远端再按原任务恢复'),
    'remote_outcome_unknown':('warning','远端结果长时间未知，保留原身份查询及费用证据'),
    'billing_reconciliation_aging':('warning','待对账预占超过阈值，不推定免费或退款'),
    'billing_adjustment_pending':('warning','存在未批准的冲突或无效费用证据，请核对账单'),
    'provider_failure_ratio':('warning','近期已判定的供应商关联任务失败较多，核对原任务与服务状态'),
}


def utc(value):return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def age(now,value):return max(0.,(now-utc(value)).total_seconds()) if value else 0.


def increment_counter(db,key):
    if key!='callback_duplicate':raise ValueError('unknown runtime counter')
    insert=pg_insert if db.get_bind().dialect.name=='postgresql' else sq_insert
    db.execute(insert(models.RuntimeCounter).values(key=key,value=1).on_conflict_do_update(index_elements=['key'],set_={'value':models.RuntimeCounter.value+1}))


def operational_snapshot(db,*,now=None):
    now=now or datetime.now(timezone.utc);settings=get_settings();j=models.Job;family=case((j.type.like('batch.%'),'batch'),else_=func.coalesce(family_expression(),'other'))
    states=('queued','running','succeeded','failed','cancelled')
    status=case((j.status.in_(states),j.status),else_='other')
    jobs=[{'family':row[0],'status':row[1],'count':row[2]} for row in db.execute(select(family,status,func.count()).group_by(family,status))]
    ready=and_(j.status=='queued',~j.policy_blocked,family_expression().in_(FAMILIES),or_(j.next_retry_at.is_(None),j.next_retry_at<=now))
    count,oldest=db.execute(select(func.count(),func.min(func.coalesce(j.next_retry_at,j.updated_at))).where(ready)).one()
    # Missing legacy JSON fields make the compatibility predicate SQL NULL.
    unsubmitted_since=db.scalar(select(func.min(j.created_at)).where(ready,j.attempt_count==0,remote_occupancy().is_not(True)))
    # This is an age/due projection, not a second admission algorithm.
    queue={'due_count':count,'oldest_due_seconds':age(now,oldest),'oldest_unsubmitted_seconds':age(now,unsubmitted_since),
        'note':'到期候选及首次未执行任务年龄；依赖、启用能力、容量与远端检查仍由实际调度器决定'}
    remote_count,remote_oldest=db.execute(select(func.count(),func.min(j.created_at)).where(remote_occupancy())).one()
    unknown=select(models.ProviderOperation.job_id.label('job_id'),models.ProviderOperation.created_at.label('since')).where(models.ProviderOperation.state.in_(('submitting','unknown')))
    unknown_package=select(models.DeliveryPackage.job_id,func.coalesce(models.DeliveryPackage.remote_started_at,models.DeliveryPackage.created_at)).where(models.DeliveryPackage.remote_state=='unknown')
    unknown_sources=unknown.union_all(unknown_package).subquery()
    unknown_count,unknown_oldest=db.execute(select(func.count(func.distinct(unknown_sources.c.job_id)),func.min(unknown_sources.c.since))).one()
    remote={'unresolved_count':remote_count,'oldest_unresolved_seconds':age(now,remote_oldest),'unknown_count':unknown_count,'oldest_unknown_seconds':age(now,unknown_oldest)}
    w=models.WorkflowRun
    expired=db.scalar(select(func.count()).select_from(w).where(w.status=='running',w.lease_expires_at.is_not(None),w.lease_expires_at<now)) or 0
    p=models.WorkerPulse;fresh=now-timedelta(seconds=settings.observation_worker_stale_seconds)
    live=db.scalar(select(func.count()).select_from(p).where(p.stopped_at.is_(None),p.last_seen_at>=fresh)) or 0
    stale=db.scalar(select(func.count()).select_from(p).where(p.stopped_at.is_(None),p.last_seen_at<fresh)) or 0
    duplicate_tags=select(p.worker_tag).where(p.stopped_at.is_(None),p.last_seen_at>=fresh).group_by(p.worker_tag).having(func.count()>1).subquery()
    collisions=db.scalar(select(func.count()).select_from(duplicate_tags)) or 0
    workers={'live':live,'stale':stale,'identity_collisions':collisions,'expired_workflows':expired}
    ledger=models.UsageLedger
    pending,pending_since=db.execute(select(func.count(),func.min(ledger.updated_at)).where(ledger.status=='pending_reconciliation')).one()
    e=models.UsageEvidence
    issues=db.scalar(select(func.count()).select_from(e).where(e.disposition.in_(('adjustment_pending','invalid_amount')),
        ~select(models.UsageAdjustment.id).where(models.UsageAdjustment.evidence_id==e.id).exists())) or 0
    billing={'pending_count':pending,'oldest_pending_seconds':age(now,pending_since),'unresolved_evidence_count':issues}
    o=models.ProviderOperation;window=now-timedelta(hours=24)
    # Remote successes on a cancelled local job are not provider failures.
    outcome=case((j.status=='succeeded','succeeded'),(and_(j.status=='failed',~o.state.in_((*UNRESOLVED,'created'))),'failed'),else_='unclassified')
    rows=list(db.execute(select(o.provider_key,outcome,func.count()).join(j,j.id==o.job_id).where(o.updated_at>=window).group_by(o.provider_key,outcome)))
    totals={}
    for key,state,total in rows:totals[key]=totals.get(key,0)+total
    selected=set(sorted(totals,key=lambda key:(-totals[key],str(key)))[:16]);groups={}
    for key,state,total in rows:
        tag='provider_'+fingerprint(key) if key in selected and key else 'other'
        group=groups.setdefault(tag,{'provider_tag':tag,'succeeded':0,'failed':0,'unclassified':0});group[state]+=total
    providers=[]
    for tag,row in sorted(groups.items()):
        decided=row['succeeded']+row['failed'];row['success_rate']=row['succeeded']/decided if decided else None;providers.append(row)
    counters=dict(db.execute(select(models.RuntimeCounter.key,models.RuntimeCounter.value)).all())
    active=[]
    conditions={
        'queue_aging':count>0 and max(queue['oldest_due_seconds'],queue['oldest_unsubmitted_seconds'])>=settings.observation_queue_warning_seconds,
        'workers_unobserved':count>0 and live==0,
        'worker_identity_collision':collisions>0,
        'workflow_lease_expired':expired>0,
        'remote_outcome_unknown':unknown_count>0 and remote['oldest_unknown_seconds']>=settings.observation_remote_warning_seconds,
        'billing_reconciliation_aging':pending>0 and billing['oldest_pending_seconds']>=settings.observation_billing_warning_seconds,
        'billing_adjustment_pending':issues>0,
        'provider_failure_ratio':any(row['succeeded']+row['failed']>=5 and row['failed']/(row['succeeded']+row['failed'])>=.5 for row in providers),
    }
    for code,triggered in conditions.items():
        if triggered:active.append({'code':code,'severity':ALERTS[code][0],'message':ALERTS[code][1]})
    return {'schema':'operations/1','generated_at':now,'window_seconds':86400,'jobs':jobs,'queue':queue,'remote':remote,'workers':workers,'billing':billing,'providers':providers,
        'thresholds':{'queue_seconds':settings.observation_queue_warning_seconds,'remote_unknown_seconds':settings.observation_remote_warning_seconds,
            'billing_pending_seconds':settings.observation_billing_warning_seconds,'worker_stale_seconds':settings.observation_worker_stale_seconds,'provider_minimum_decided':5,'provider_failure_ratio':.5},
        'callback_duplicates':counters.get('callback_duplicate',0),'alerts':active,
        'provider_rate_scope':'近24小时按最后变动归类的逻辑操作所关联任务；排除尚未判定和本地取消，不是供应商SLA或请求次数',
        'source':'database_snapshot_not_cross_query_transactional_checkpoint'}


def evaluate_alerts(db,*,now=None):
    """One internal evaluator at a time; emit returned transitions only after commit."""
    insert=pg_insert if db.get_bind().dialect.name=='postgresql' else sq_insert
    db.execute(insert(models.RuntimeCounter).values(key='alert_evaluation',value=0).on_conflict_do_nothing(index_elements=['key']))
    db.scalar(select(models.RuntimeCounter).where(models.RuntimeCounter.key=='alert_evaluation').with_for_update())
    snapshot=operational_snapshot(db,now=now);now=snapshot['generated_at'];current={row['code'] for row in snapshot['alerts']}
    existing={row.code:row for row in db.scalars(select(models.OperationalAlert).with_for_update())};transitions=[]
    for code in ALERTS:
        row=existing.get(code)
        if code in current:
            if row is None:
                row=models.OperationalAlert(code=code,active=True,severity=ALERTS[code][0],first_seen_at=now,last_seen_at=now,occurrence_count=1);db.add(row)
                transitions.append({'event':'operational_alert','code':code,'severity':row.severity,'count':1})
            else:
                if not row.active:
                    row.first_seen_at=now;row.resolved_at=None;row.occurrence_count+=1
                    transitions.append({'event':'operational_alert','code':code,'severity':row.severity,'count':row.occurrence_count})
                row.active=True;row.last_seen_at=now
        elif row and row.active:
            row.active=False;row.resolved_at=now;row.last_seen_at=now
            transitions.append({'event':'operational_alert_recovered','code':code,'severity':row.severity,'count':row.occurrence_count})
    db.flush();return snapshot,transitions


def job_diagnostic(db,id):
    """Bounded support projection: never load Job payload or provider result bodies."""
    j=models.Job
    columns=(j.id,j.tenant_id,j.project_id,j.status,j.type,j.attempt_count,j.max_attempts,j.execution_generation,j.failure_code,j.retryable,j.policy_blocked,j.created_at,j.updated_at)
    job=db.execute(select(*columns).where(j.id==id)).mappings().first()
    if not job:return None
    job=dict(job);job['failure_tag']=fingerprint(job['failure_code']) if job['failure_code'] else None;job['failure_code']=failure_code(job['failure_code'])
    job['family']=job_family(job.pop('type'))
    origin=db.get(models.JobTrace,id);job['origin_request_id']=origin.request_id if origin else None
    w=models.WorkflowRun
    workflow=db.execute(select(w.id,w.runtime,w.status,w.attempt_count,w.lease_expires_at).where(w.job_id==id)).mappings().first()
    o=models.ProviderOperation
    operation=db.execute(select(o.id,o.state,o.provider_key,o.capability,o.external_job_id,o.created_at,o.updated_at).where(o.job_id==id)).mappings().first()
    if operation:
        operation=dict(operation);provider=operation.pop('provider_key');operation['provider_tag']='provider_'+fingerprint(provider) if provider else 'other'
        # Capability names and remote IDs can originate in an external payload.
        from .providers import Capability
        capability=operation.pop('capability');operation['capability']=capability if capability in get_args(Capability) else 'other'
        operation['capability_tag']=fingerprint(capability);external=operation.pop('external_job_id');operation['external_job_tag']=fingerprint(external) if external else None
    ledger=[dict(row) for row in db.execute(select(models.UsageLedger.id,models.UsageLedger.status,models.UsageLedger.currency,models.UsageLedger.amount_micros,models.UsageLedger.updated_at).where(models.UsageLedger.job_id==id).limit(20)).mappings()]
    for row in ledger:
        row['amount_micros']=str(row['amount_micros']);row['currency']=row['currency'] if row['currency'] in {'USD','CNY','EUR','JPY','GBP'} else 'other'
        row['status']=row['status'] if row['status'] in {'reserved','pending_reconciliation','settled','released','rejected','blocked'} else 'other'
    a=models.Artifact
    artifacts=[dict(row) for row in db.execute(select(a.id,a.delivery_status,models.ArtifactStorage.sha256,models.ArtifactStorage.size_bytes,models.ArtifactStorage.expires_at)
        .outerjoin(models.ArtifactStorage,models.ArtifactStorage.artifact_id==a.id).where(a.job_id==id).order_by(a.created_at).limit(100)).mappings()]
    b=models.BatchRun
    waves=[dict(row) for row in db.execute(select(b.batch_id,b.id.label('run_id'),b.parent_job_id,models.BatchRunItem.revision_id).join(models.BatchRunItem,models.BatchRunItem.run_id==b.id).where(models.BatchRunItem.job_id==id).limit(100)).mappings()]
    packages=list(db.scalars(select(models.DeliveryPackage.id).where(models.DeliveryPackage.job_id==id).limit(1)))
    callback_disposition=case((models.ProviderEvent.disposition.in_(('received','applied','late_evidence')),models.ProviderEvent.disposition),else_='other')
    callbacks=dict(db.execute(select(callback_disposition,func.count()).where(models.ProviderEvent.job_id==id).group_by(callback_disposition)).all())
    evidence_disposition=case((models.UsageEvidence.disposition.in_(('adjustment_pending','invalid_amount','amount_unknown','reservation_rollover','observed','duplicate')),models.UsageEvidence.disposition),else_='other')
    evidence=dict(db.execute(select(evidence_disposition,func.count()).where(models.UsageEvidence.job_id==id).group_by(evidence_disposition)).all())
    revision_id=db.scalar(select(func.substr(j.payload['plan_revision_id'].as_string(),1,64)).where(j.id==id))
    r=models.PlanRevision;d=models.EditDocument
    revision=db.execute(select(r.id,r.document_id,r.number,r.content_hash).join(d,d.id==r.document_id).where(d.tenant_id==job['tenant_id'],d.project_id==job['project_id'],
        r.id==revision_id if revision_id else d.origin_job_id==id).order_by(r.number.desc()).limit(1)).mappings().first()
    # IDs are opaque even for malformed legacy records; no user names or notes.
    for key in ('id','tenant_id','project_id'):job[key]=opaque_id(job[key])
    job['status']=job['status'] if job['status'] in {'queued','running','failed','cancelled','succeeded'} else 'other'
    if workflow:
        workflow=dict(workflow);workflow['status']=workflow['status'] if workflow['status'] in {'queued','running','failed','cancelled','succeeded'} else 'other'
        workflow['runtime']=workflow['runtime'] if workflow['runtime'] in {'local','temporal'} else 'other'
    if operation:operation['state']=operation['state'] if operation['state'] in {*UNRESOLVED,'created','completed','rejected','result_received','terminal_received'} else 'other'
    for row in artifacts:
        if row['sha256'] and not re.fullmatch('[0-9a-f]{64}',row['sha256']):row['sha256']=None
        row['delivery_status']=row['delivery_status'] if row['delivery_status'] in {'active','revoked','deleted','archived_duplicate'} else 'other'
    revision=dict(revision) if revision else None
    if revision and not re.fullmatch('[0-9a-f]{64}',revision['content_hash'] or ''):revision['content_hash']=None
    for row in [*ledger,*artifacts,*waves,*([workflow] if workflow else []),*([operation] if operation else []),*([revision] if revision else [])]:
        for key in ('id','document_id','batch_id','run_id','parent_job_id','revision_id'):
            if key in row:row[key]=opaque_id(row[key])
    packages=[opaque_id(id) for id in packages]
    return {'schema':'job-diagnostic/1','job':job,'workflow':workflow,'operation':operation,'ledger':ledger,'artifacts':artifacts,'waves':waves,'revision':revision,'package_ids':packages,'callbacks':callbacks,'usage_evidence':evidence,
        'limits':{'ledger':20,'artifacts':100,'waves':100},'note':'只读定位，不重试、重新生成、取消、签名或改账；恢复使用原任务工作台'}


def prometheus_operations(snapshot):
    lines=[];seen=set()
    def metric(name,value,labels=''):
        if name not in seen:
            seen.add(name);kind='counter' if name=='callback_duplicates_total' else 'gauge'
            lines.extend((f'# HELP video_ai_{name} Operational database projection: {name}.',f'# TYPE video_ai_{name} {kind}'))
        lines.append(f'video_ai_{name}'+('{'+labels+'}' if labels else '')+' '+str(value))
    for row in snapshot['jobs']:metric('jobs_by_family',row['count'],f'family="{row["family"]}",status="{row["status"]}"')
    metric('queue_due',snapshot['queue']['due_count']);metric('queue_oldest_due_seconds',snapshot['queue']['oldest_due_seconds'])
    metric('queue_oldest_unsubmitted_seconds',snapshot['queue']['oldest_unsubmitted_seconds'])
    for key,value in snapshot['remote'].items():metric('remote_'+key.replace('_count','_jobs'),value)
    for key,value in snapshot['workers'].items():metric('workers_'+key,value)
    for key,value in snapshot['billing'].items():metric('billing_'+{'pending_count':'pending_jobs','unresolved_evidence_count':'unresolved_evidence'}.get(key,key),value)
    metric('callback_duplicates_total',snapshot['callback_duplicates'])
    for row in snapshot['providers']:
        for state in ('succeeded','failed','unclassified'):metric('provider_terminal_jobs_24h',row[state],f'provider_tag="{row["provider_tag"]}",status="{state}"')
    active={row['code'] for row in snapshot['alerts']}
    for code,(severity,_) in ALERTS.items():metric('alert_active',int(code in active),f'code="{code}",severity="{severity}"')
    return '\n'.join(lines)+'\n'
