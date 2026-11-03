"""Additional lifetime project ceilings; tenant policy and accounting stay intact."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .. import models
from .artifact_reviews import can_review
from .editing_errors import EditingError

ACTIVE = ("reserved", "pending_reconciliation", "settled")


def project_usage(db: Session, tenant: str, project: str, currency: str, *, exclude_job: str | None = None) -> dict:
    query=select(models.UsageLedger.status,func.coalesce(func.sum(models.UsageLedger.amount_micros),0)).join(
        models.Job,models.Job.id==models.UsageLedger.job_id).where(models.UsageLedger.tenant_id==tenant,
        models.Job.project_id==project,models.UsageLedger.currency==currency,models.UsageLedger.status.in_(ACTIVE))
    adjustments=select(func.coalesce(func.sum(models.UsageAdjustment.delta_micros),0)).join(
        models.Job,models.Job.id==models.UsageAdjustment.job_id).where(models.UsageAdjustment.tenant_id==tenant,
        models.Job.project_id==project,models.UsageAdjustment.currency==currency)
    if exclude_job:
        query=query.where(models.UsageLedger.job_id!=exclude_job);adjustments=adjustments.where(models.UsageAdjustment.job_id!=exclude_job)
    amounts=dict(db.execute(query.group_by(models.UsageLedger.status)).all())
    settled=int(amounts.get('settled',0))+int(db.scalar(adjustments) or 0)
    reserved=int(amounts.get('reserved',0))+int(amounts.get('pending_reconciliation',0))
    return {'settled_micros':settled,'reserved_micros':reserved,'committed_micros':settled+reserved}


def project_check(db: Session, job: models.Job, amount: int, currency: str) -> tuple[list[str],dict | None]:
    row=db.get(models.ProjectBudget,job.project_id) if job.project_id else None
    if row is None or row.limit_micros is None:return [],None
    values=project_usage(db,job.tenant_id,job.project_id,row.currency,exclude_job=job.id)
    reasons=[]
    if currency!=row.currency:reasons.append('project_currency_mismatch')
    if values['committed_micros']+amount>row.limit_micros:reasons.append('project_limit')
    return reasons,{'project_id':row.project_id,'revision':row.revision,'currency':row.currency,'limit_micros':row.limit_micros,**values}


def limit_read(db: Session,tenant: str,project: str,actor: str | None) -> dict:
    from .budgets import get_budget
    row=db.get(models.ProjectBudget,project);tenant_budget=get_budget(db,tenant)
    currency=row.currency if row else tenant_budget.currency if tenant_budget else 'USD'
    used=project_usage(db,tenant,project,currency)
    limit=row.limit_micros if row else None
    return {'project_id':project,'revision':row.revision if row else 0,'currency':currency,'limit_micros':limit,
        'remaining_micros':None if limit is None else max(0,limit-used['committed_micros']),**used,
        'can_manage':can_review(db,tenant,actor),'scope':'project_lifetime',
        'note':'项目上限是额外限制，不能提高空间总额度；既有预占及结算不能通过降低上限释放。'}


def update_limit(db: Session,tenant: str,project: str,actor: str | None,request) -> dict:
    from .budgets import get_budget
    if not can_review(db,tenant,actor):raise EditingError('project_budget_forbidden','仅空间所有者可设置项目预算',403)
    get_budget(db,tenant,for_update=True)
    row=db.get(models.ProjectBudget,project,populate_existing=True)
    if (row.revision if row else 0)!=request.base_revision:raise EditingError('project_budget_conflict','项目预算已在其他页面变更，请先刷新再决定',409)
    used=project_usage(db,tenant,project,request.currency)
    if request.limit_micros is not None:
        other=db.scalar(select(models.UsageLedger.id).join(models.Job,models.Job.id==models.UsageLedger.job_id).where(
            models.UsageLedger.tenant_id==tenant,models.Job.project_id==project,models.UsageLedger.currency!=request.currency,
            models.UsageLedger.status.in_(ACTIVE)).limit(1))
        if other:raise EditingError('project_budget_currency','项目已有其他币种费用，不能用单一币种上限覆盖',422)
        if request.limit_micros<used['committed_micros']:raise EditingError('project_budget_committed','上限不能低于已结算和保留中的费用',422)
    if row is None:
        row=models.ProjectBudget(project_id=project,tenant_id=tenant,currency=request.currency,actor_id=actor or 'development_user',revision=0);db.add(row)
    row.currency=request.currency;row.limit_micros=request.limit_micros;row.actor_id=actor or 'development_user';row.revision+=1
    db.flush();return limit_read(db,tenant,project,actor)
