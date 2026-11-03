"""Safe user-facing accounting and durable actions; never returns vendor payloads."""
from sqlalchemy import func,select
from sqlalchemy.orm import Session
from .. import models
from .budgets import budget_snapshot,get_budget,period_key
from .completion import finish_job,lock_job,ExecutionSuperseded
from .delivery_packages import utc
from .editing import digest
from .editing_errors import EditingError
from .jobs import InvalidJobTransition,request_job_retry
from .workflows import request_workflow_retry,WorkflowError
from .project_spending import limit_read

LABELS={'reserved':'预占中','pending_reconciliation':'待对账','settled':'已记录结算','released':'已释放','rejected':'未通过预算','blocked':'未通过预算'}


def job_view(job: models.Job) -> dict:
    failure=job.failure or {}
    return {'id':job.id,'project_id':job.project_id,'type':job.type,'status':job.status,'attempt_count':job.attempt_count,
        'execution_generation':job.execution_generation,'max_attempts':job.max_attempts,'next_retry_at':utc(job.next_retry_at) if job.next_retry_at else None,
        'can_retry':bool(not job.type.startswith('batch.') and job.status=='failed' and job.retryable and not job.policy_blocked and job.attempt_count<job.max_attempts),
        'can_cancel':not job.type.startswith('batch.') and job.status in {'queued','running'},'policy_blocked':job.policy_blocked,'failure_code':job.failure_code,
        'failure_message':failure.get('message'),'created_at':utc(job.created_at),'updated_at':utc(job.updated_at),
        'recovery_note':'恢复沿用原任务和执行身份，不是重新生成；如已产生远端费用，取消也不等于退款。'}


def overview(db: Session,tenant: str,project: str | None,actor: str | None) -> dict:
    period=period_key();budget=get_budget(db,tenant);currency=budget.currency if budget else 'USD'
    query=select(models.UsageLedger.currency,models.UsageLedger.status,func.sum(models.UsageLedger.amount_micros),func.count()).where(models.UsageLedger.tenant_id==tenant)
    adj=select(models.UsageAdjustment.currency,func.sum(models.UsageAdjustment.delta_micros)).where(models.UsageAdjustment.tenant_id==tenant)
    if project:
        query=query.join(models.Job,models.Job.id==models.UsageLedger.job_id).where(models.Job.project_id==project)
        adj=adj.join(models.Job,models.Job.id==models.UsageAdjustment.job_id).where(models.Job.project_id==project)
    else:query=query.where(models.UsageLedger.period_key==period);adj=adj.where(models.UsageAdjustment.period_key==period)
    totals={}
    for ccy,status,amount,count in db.execute(query.group_by(models.UsageLedger.currency,models.UsageLedger.status)):
        entry=totals.setdefault(ccy,{'currency':ccy,'reserved_micros':0,'pending_micros':0,'settled_micros':0,'released_micros':0,'pending_count':0,'adjustment_micros':0})
        field={'reserved':'reserved_micros','pending_reconciliation':'pending_micros','settled':'settled_micros','released':'released_micros'}.get(status)
        if field:entry[field]+=int(amount or 0)
        if status=='pending_reconciliation':entry['pending_count']+=count
    for ccy,amount in db.execute(adj.group_by(models.UsageAdjustment.currency)):
        entry=totals.setdefault(ccy,{'currency':ccy,'reserved_micros':0,'pending_micros':0,'settled_micros':0,'released_micros':0,'pending_count':0,'adjustment_micros':0})
        entry['adjustment_micros']+=int(amount or 0)
    return {'period_key':period,'scope':'project_lifetime' if project else 'tenant_month','totals':list(totals.values()),
        'tenant_policy':{'currency':currency,'enabled':budget.enabled if budget else False,
            'monthly_limit_micros':budget.monthly_limit_micros if budget else None,'task_limit_micros':budget.task_limit_micros if budget else None,
            **budget_snapshot(db,budget,tenant)},
        'project_policy':limit_read(db,tenant,project,actor) if project else None,
        'note':'币种分别列示，不做汇率折算。已记录结算不等于云账单已确认；待对账金额未知时保留原预占，不显示为免费。'}


def ledger_page(db: Session,tenant: str,project: str | None,skip: int,limit: int):
    query=select(models.UsageLedger,models.Job.type,models.Job.status,models.Job.project_id).join(models.Job,models.Job.id==models.UsageLedger.job_id).where(models.UsageLedger.tenant_id==tenant)
    if project:query=query.where(models.Job.project_id==project)
    rows=list(db.execute(query.order_by(models.UsageLedger.created_at.desc(),models.UsageLedger.id.desc()).offset(skip).limit(limit+1)))
    items=[]
    for row,kind,status,project_id in rows[:limit]:
        delta=db.scalar(select(func.coalesce(func.sum(models.UsageAdjustment.delta_micros),0)).where(models.UsageAdjustment.job_id==row.job_id)) or 0
        items.append({'id':row.id,'job_id':row.job_id,'project_id':project_id,'job_type':kind,'job_status':status,'status':row.status,
            'status_label':LABELS.get(row.status,'预算记录'),'amount_micros':row.amount_micros,'currency':row.currency,
            'adjustment_micros':delta,'net_settled_micros':row.amount_micros+delta if row.status=='settled' else None,
            'period_key':row.period_key,'created_at':utc(row.created_at),'decision':row.decision,
            'price_version':((row.metadata_json or {}).get('reservation') or {}).get('price_version'),
            'is_estimate':row.status in {'reserved','pending_reconciliation'},'bill_confirmed':False})
    return {'items':items,'skip':skip,'next_skip':skip+len(items),'has_more':len(rows)>limit}


def jobs_page(db: Session,tenant: str,project: str | None,state: str,skip: int,limit: int):
    query=select(models.Job).where(models.Job.tenant_id==tenant)
    if project:query=query.where(models.Job.project_id==project)
    if state=='active':query=query.where(models.Job.status.in_(('queued','running')))
    elif state=='attention':query=query.where(models.Job.status.in_(('failed','cancelled')))
    elif state!='all':raise EditingError('task_filter_invalid','任务筛选无效',422)
    rows=list(db.scalars(query.order_by(models.Job.created_at.desc(),models.Job.id.desc()).offset(skip).limit(limit+1)))
    return {'items':[job_view(row) for row in rows[:limit]],'next_skip':skip+min(limit,len(rows)),'has_more':len(rows)>limit}


def action_receipt(db: Session,job: models.Job,row: models.JobAction | None):
    return {'found':row is not None,'action':{'id':row.id,'action':row.action,'result':row.result_json,'created_at':utc(row.created_at)} if row else None,'current':job_view(job)}


def apply_action(db: Session,job: models.Job,actor: str | None,key: str,request):
    if job.type.startswith('batch.'):raise EditingError('batch_parent_derived','批次状态由子项汇总，请处理具体任务',409)
    lock_job(db,job)
    fingerprint=digest({'actor':actor or 'development_user','request':request.model_dump(mode='json')})
    prior=db.scalar(select(models.JobAction).where(models.JobAction.job_id==job.id,models.JobAction.idempotency_key==key))
    if prior:
        if prior.request_hash!=fingerprint:raise EditingError('task_action_conflict','原操作标识已用于其他任务设置',409)
        return prior
    if (job.status,job.attempt_count,job.execution_generation)!=(request.expected_status,request.expected_attempt,request.expected_generation):
        raise EditingError('task_state_changed','任务状态已变化，请先刷新并核对，不会自动重复操作',409)
    try:
        if request.action=='cancel':finish_job(db,job,'cancelled')
        else:
            request_job_retry(db,job,force=False)
            workflow=db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id==job.id))
            if workflow:request_workflow_retry(db,workflow,delay_seconds=0)
    except (InvalidJobTransition,WorkflowError,ExecutionSuperseded) as exc:
        raise EditingError('task_action_rejected','当前任务不能执行该操作，请刷新状态或查看费用明细',409) from exc
    row=models.JobAction(tenant_id=job.tenant_id,job_id=job.id,idempotency_key=key,request_hash=fingerprint,
        action=request.action,actor_id=actor or 'development_user',result_json={'status':job.status,'attempt_count':job.attempt_count,'execution_generation':job.execution_generation})
    db.add(row);db.flush();return row
