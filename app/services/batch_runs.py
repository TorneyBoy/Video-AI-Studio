"""Fixed-version render waves and original-task actions, never provider IO."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import re
from sqlalchemy import select,or_
from sqlalchemy.orm import aliased
from .. import models,schemas
from ..billing_schemas import JobActionRequest
from . import batches,render_quotes,preparation_quotes,billing_workbench
from .budgets import get_budget,preflight_job_budget
from .delivery_packages import utc
from .editing import digest,timeline_for_revision
from .editing_errors import EditingError
from timeline_engine import Timeline,TemplateDefinition,create_manifest


def payload(quality,quote_id=None):
    return schemas.RenderRequest(quality=quality,quote_id=quote_id,output={'format':'mp4'},options={'source':'studio-flow'},max_attempts=3)


def task_rows(db,ids,tenant):
    fields=('id','tenant_id','project_id','type','status','attempt_count','execution_generation','max_attempts','next_retry_at','policy_blocked','retryable','failure_code','created_at','updated_at')
    out={}
    for row in db.execute(select(*(getattr(models.Job,key) for key in fields)).where(models.Job.tenant_id==tenant,models.Job.id.in_(ids))):
        value=dict(row._mapping);code=value['failure_code'];value['failure_code']=code if not code or re.fullmatch(r'[a-zA-Z0-9_.-]{1,120}',code) else 'task_failed'
        out[row.id]=SimpleNamespace(**value,failure={'message':value['failure_code']} if value['failure_code'] else None)
    return out


def head_for(db,batch,item,revision_id=None):
    document=db.scalar(select(models.EditDocument).where(models.EditDocument.tenant_id==batch.tenant_id,models.EditDocument.project_id==batch.project_id,models.EditDocument.origin_job_id==item.plan_job_id))
    head=db.get(models.EditHead,document.id) if document else None
    if not head or revision_id and revision_id!=head.revision_id:raise EditingError('batch_render_version_changed','所选编辑版本已变化或尚未就绪，请刷新本项',409)
    revision=db.get(models.PlanRevision,head.revision_id);frozen=timeline_for_revision(db,revision)
    return document,revision,frozen


def render_binding(document,revision,frozen):
    return {'edit_document_id':document.id,'plan_revision_id':revision.id,'timeline_revision_id':revision.id,'plan_hash':revision.content_hash,'timeline_hash':frozen.content_hash}


def previous_job(db,batch,revision,request_hash,quality):
    return db.scalar(select(models.Job.id).where(models.Job.tenant_id==batch.tenant_id,models.Job.project_id==batch.project_id,
        models.Job.type=='render.'+quality,models.Job.payload['timeline_revision_id'].as_string()==revision.id,
        models.Job.payload['render_request_hash'].as_string()==request_hash).order_by(models.Job.created_at.desc(),models.Job.id.desc()).limit(1))


def unresolved(db,tenant,document_id):
    jobs=select(models.Job.id).where(models.Job.tenant_id==tenant,models.Job.type.like('render.%'),models.Job.payload['edit_document_id'].as_string()==document_id)
    active=db.scalar(jobs.where(models.Job.status.in_(['queued','running'])).limit(1))
    if active:return True
    if db.scalar(select(models.ProviderOperation.id).where(models.ProviderOperation.job_id.in_(jobs),models.ProviderOperation.state.in_(['submitting','unknown','pending'])).limit(1)):return True
    return bool(db.scalar(select(models.UsageLedger.id).where(models.UsageLedger.job_id.in_(jobs),models.UsageLedger.status=='pending_reconciliation').limit(1)))


def reuse_problem(job):
    if job is None:return '原生成任务不可用，请先核对'
    if job.policy_blocked:return '原生成仍受交付或费用策略限制'
    if job.status in {'failed','cancelled'}:return '请先处理或恢复原任务；重新生成需要明确的新报价'
    return None


def create_quote(db,batch,request,key):
    body=request.model_dump(mode='json');body['items']=sorted(body['items'],key=lambda item:item['key']);fingerprint=digest(body)
    previous=db.scalar(select(models.BatchRenderQuote).where(models.BatchRenderQuote.tenant_id==batch.tenant_id,models.BatchRenderQuote.idempotency_key==key))
    if previous:
        if previous.batch_id!=batch.id or previous.request_hash!=fingerprint:raise EditingError('batch_render_quote_conflict','原报价标识已用于不同版本或设置',409)
        return previous
    if batch.state!='submitted':raise EditingError('batch_render_plan_required','请先完成本批次的剪辑计划',409)
    rows={item.client_key:item for item in batches.items_for(db,batch)};jobs=task_rows(db,[row.plan_job_id for row in rows.values() if row.plan_job_id],batch.tenant_id)
    quote_id=models.new_id();lines=[];issues=[];expires=datetime.now(timezone.utc)+timedelta(minutes=10)
    for selection in body['items']:
        item=rows.get(selection['key']);job=jobs.get(item.plan_job_id) if item else None
        if not item:raise EditingError('batch_render_item_missing','有子项不属于本批次',404)
        if not job or job.status!='succeeded' or job.policy_blocked:
            issues.append({'key':item.client_key,'field':'render','message':'本项剪辑计划尚未成功或受策略限制'});continue
        try:
            document,revision,frozen=head_for(db,batch,item,selection['revision_id']);binding=render_binding(document,revision,frozen)
            row=db.get(models.Timeline,frozen.timeline_id);request_hash=render_quotes.request_hash(row.id,binding,payload(body['quality']))
            prior=previous_job(db,batch,revision,request_hash,body['quality'])
            line={'key':item.client_key,'item_id':item.id,'name':item.name or f'第{item.position+1}项','position':item.position,
                'document_id':document.id,'revision_id':revision.id,'revision_number':revision.number,'timeline_id':frozen.timeline_id,'binding':binding,'request_hash':request_hash}
            if prior and not body['force_new']:
                current=task_rows(db,[prior],batch.tenant_id).get(prior);lines.append({**line,'disposition':'reused','job_id':prior,'problem':reuse_problem(current)});continue
            problem='原生成仍在处理或结果待核对，请先处理原任务' if unresolved(db,batch.tenant_id,document.id) else None
            price=render_quotes.create_quote(db,row,batch.tenant_id,payload(body['quality']),'batch-render:'+quote_id+':'+item.id)
            price.summary_json={**price.summary_json,'batch_quote_id':quote_id};expires=min(utc(price.expires_at),expires)
            lines.append({**line,'disposition':'new','job_id':None,'render_quote_id':price.id,'amount_micros':price.amount_micros,'currency':price.currency,'problem':problem})
        except EditingError as exc:issues.append({'key':item.client_key,'field':'render','message':str(exc)})
    if issues:raise EditingError('batch_render_validation_failed','请处理标出的子项，或只选择已就绪项',422,{'issues':issues})
    result=models.BatchRenderQuote(id=quote_id,tenant_id=batch.tenant_id,batch_id=batch.id,idempotency_key=key,request_hash=fingerprint,request_json=body,snapshot_json={'items':lines},expires_at=expires)
    db.add(result);db.flush();return result


def quote_for(db,batch,id,*,lock=False):
    stmt=select(models.BatchRenderQuote).where(models.BatchRenderQuote.id==id,models.BatchRenderQuote.batch_id==batch.id,models.BatchRenderQuote.tenant_id==batch.tenant_id)
    if lock:stmt=stmt.with_for_update().execution_options(populate_existing=True)
    row=db.scalar(stmt)
    if not row:raise EditingError('batch_render_quote_missing','未找到本批次渲染报价',404)
    return row


def _budget(db,batch,lines):
    allowed,reasons=preparation_quotes.affordability(db,batch.tenant_id,batch.project_id,[{**line,'problem':None} for line in lines])
    if any(line.get('problem') for line in lines):reasons.append('original_render_unresolved');allowed=False
    return allowed,sorted(set(reasons))


def quote_read(db,batch,quote):
    stored=sorted(quote.snapshot_json['items'],key=lambda line:(line['position'],line['key']));jobs=task_rows(db,[item['job_id'] for item in stored if item.get('job_id')],batch.tenant_id);lines=[];totals={}
    for line in stored:
        value=dict(line);value['problem']=reuse_problem(jobs.get(line['job_id'])) if line['disposition']=='reused' else line.get('problem')
        if line['disposition']=='new':totals[line['currency']]=totals.get(line['currency'],0)+line['amount_micros']
        lines.append(value)
    allowed,reasons=_budget(db,batch,lines);expired=utc(quote.expires_at)<=datetime.now(timezone.utc)
    return {'id':quote.id,'batch_id':batch.id,'project_id':batch.project_id,'quality':quote.request_json['quality'],'force_new':quote.request_json['force_new'],
        'expires_at':utc(quote.expires_at),'status':'consumed' if quote.consumed_key else 'expired' if expired else 'ready',
        'items':[{key:line.get(key) for key in ('key','name','revision_id','revision_number','disposition','job_id','amount_micros','currency','problem')} for line in lines],
        'totals':[{'currency':ccy,'amount_micros':amount} for ccy,amount in sorted(totals.items())],'item_count':len(lines),
        'can_submit':not quote.consumed_key and not expired and allowed,'reasons':reasons,'result':quote.result_json,
        'note':'按选定版本生成MP4；已有同版任务优先复用，本次只为新增生成预占。输入、价格或额度变化需重新报价。恢复原任务不等于重新生成，预占不是最终账单。'}


def confirm_quote(db,batch,quote,key):
    if quote.consumed_key:
        if quote.consumed_key!=key:raise EditingError('batch_render_quote_used','报价已用于原提交，请核对原操作',409)
        return quote.result_json
    if utc(quote.expires_at)<=datetime.now(timezone.utc):raise EditingError('batch_render_quote_expired','报价已过期，请重新获取',409)
    lines=quote.snapshot_json['items'];ids=sorted({line['document_id'] for line in lines})
    list(db.scalars(select(models.EditDocument).where(models.EditDocument.tenant_id==batch.tenant_id,models.EditDocument.id.in_(ids)).order_by(models.EditDocument.id).with_for_update().execution_options(populate_existing=True)))
    items={item.id:item for item in batches.items_for(db,batch)};resolved=[]
    for line in lines:
        item=items.get(line['item_id'])
        if not item:raise EditingError('batch_render_item_missing','原子项不可用',409)
        document,revision,frozen=head_for(db,batch,item,line['revision_id']);binding=render_binding(document,revision,frozen)
        if binding!=line['binding']:raise EditingError('batch_render_version_changed','原编辑版本快照已变化，请重新报价',409)
        value=dict(line)
        if line['disposition']=='new' and not quote.request_json['force_new']:
            prior=previous_job(db,batch,revision,line['request_hash'],quote.request_json['quality'])
            if prior:value.update(disposition='reused',job_id=prior,problem=None)
        if value['disposition']=='reused':
            job=db.get(models.Job,value['job_id'],populate_existing=True)
            if (not job or job.tenant_id!=batch.tenant_id or (job.payload or {}).get('render_request_hash')!=line['request_hash'] or reuse_problem(job)):
                raise EditingError('batch_render_original_unresolved','原生成已变化或需要恢复，请先查看原任务',409)
            value['problem']=None
        else:
            if unresolved(db,batch.tenant_id,document.id):raise EditingError('batch_render_original_unresolved','原生成仍在处理或费用待核对，请先处理原任务',409)
            row=db.get(models.Timeline,frozen.timeline_id);canonical,current_binding,selected,_=render_quotes.quote_context(db,row,batch.tenant_id,payload(quote.request_json['quality']))
            price=db.get(models.RenderQuote,line['render_quote_id'])
            if current_binding!=binding or digest(selected)!=price.capability_hash:raise EditingError('batch_render_price_changed','服务或价格已变化，请重新报价',409)
            value.update(canonical=canonical.model_dump(mode='json'),capability=selected,template=revision.template_json)
        resolved.append(value)
    get_budget(db,batch.tenant_id,for_update=True)
    allowed,reasons=_budget(db,batch,resolved)
    if not allowed:raise EditingError('batch_render_budget_exceeded','当前额度不足，整批未提交',409,{'reasons':reasons})
    if db.scalar(select(models.Job.id).where(models.Job.tenant_id==batch.tenant_id,models.Job.idempotency_key==key)) or db.scalar(select(models.BatchRenderQuote.id).where(models.BatchRenderQuote.tenant_id==batch.tenant_id,models.BatchRenderQuote.consumed_key==key)):
        raise EditingError('batch_render_key_conflict','该标识已用于其他操作，请核对原提交',409)
    parent=models.Job(tenant_id=batch.tenant_id,project_id=batch.project_id,type='batch.render',status='running',idempotency_key=key,max_attempts=1,started_at=datetime.now(timezone.utc),payload={'batch_id':batch.id,'batch_render_quote_id':quote.id})
    db.add(parent);db.flush();run=models.BatchRun(tenant_id=batch.tenant_id,batch_id=batch.id,quote_id=quote.id,parent_job_id=parent.id,quality=quote.request_json['quality']);db.add(run);db.flush();result=[]
    parent.payload={**parent.payload,'batch_run_id':run.id}
    for line in resolved:
        job_id=line.get('job_id')
        if line['disposition']=='new':
            canonical=Timeline.model_validate(line['canonical']);request=payload(quote.request_json['quality'],line['render_quote_id']);job_key='batch-render:'+quote.id+':'+line['item_id']
            options={**request.options,'quality':request.quality};manifest=create_manifest(canonical,template=TemplateDefinition.model_validate(line['template']),output=request.output,options=options)
            render_quotes.consume_quote(db,db.get(models.Timeline,line['timeline_id']),batch.tenant_id,request,job_key,line['request_hash'],line['capability'],line['canonical'],batch_quote_id=quote.id)
            job=models.Job(tenant_id=batch.tenant_id,project_id=batch.project_id,type='render.'+request.quality,status='queued',idempotency_key=job_key,max_attempts=3,
                payload={'manifest':manifest.model_dump(mode='json'),'manifest_id':manifest.manifest_id,'render_request_hash':line['request_hash'],**line['binding'],
                    'quote_id':line['render_quote_id'],'capability_snapshot':line['capability'],'batch_id':batch.id,'batch_run_id':run.id,'batch_item_id':line['item_id'],'parent_job_id':parent.id})
            db.add(job);db.flush();decision=preflight_job_budget(db,job,selected_capability=line['capability'])
            if decision is not None and not decision.allowed:raise EditingError('batch_render_budget_exceeded','额度已变化，整批未提交',409)
            job_id=job.id
        db.add(models.BatchRunItem(run_id=run.id,batch_item_id=line['item_id'],revision_id=line['revision_id'],job_id=job_id,disposition=line['disposition']))
        result.append({'key':line['key'],'job_id':job_id,'revision_id':line['revision_id'],'disposition':line['disposition']})
    db.add(models.JobStep(tenant_id=batch.tenant_id,job_id=parent.id,name='batch.aggregate',sequence=0,status='running',progress=0,payload={},started_at=parent.started_at))
    quote.consumed_key=key;quote.result_json={'batch_id':batch.id,'run_id':run.id,'parent_job_id':parent.id,'items':result};db.flush();return quote.result_json


def run_for(db,batch,id,*,lock=False):
    stmt=select(models.BatchRun).where(models.BatchRun.id==id,models.BatchRun.batch_id==batch.id,models.BatchRun.tenant_id==batch.tenant_id)
    if lock:stmt=stmt.with_for_update().execution_options(populate_existing=True)
    row=db.scalar(stmt)
    if not row:raise EditingError('batch_run_missing','未找到本批次的生成记录',404)
    return row


def run_items(db,run):
    return list(db.execute(select(models.BatchRunItem,models.BatchItem,models.PlanRevision.number).join(models.BatchItem,models.BatchItem.id==models.BatchRunItem.batch_item_id)
        .join(models.PlanRevision,models.PlanRevision.id==models.BatchRunItem.revision_id).where(models.BatchRunItem.run_id==run.id).order_by(models.BatchItem.position,models.BatchItem.id)))


def run_read(db,batch,run):
    rows=run_items(db,run);jobs=task_rows(db,[row.job_id for row,_,_ in rows if row.job_id],batch.tenant_id);items=[]
    outputs={row.job_id:row for row in db.execute(select(models.Artifact.job_id,models.Artifact.id,models.Artifact.metadata_json['timeline_revision_id'].as_string().label('revision_id'))
        .where(models.Artifact.tenant_id==batch.tenant_id,models.Artifact.job_id.in_(list(jobs)),models.Artifact.output_role=='primary',models.Artifact.output_index==0))}
    for row,item,number in rows:
        job=jobs.get(row.job_id);output=outputs.get(row.job_id);items.append({'key':item.client_key,'name':item.name,'revision_id':row.revision_id,'revision_number':number,'disposition':row.disposition,
            'artifact_id':output.id if output and output.revision_id==row.revision_id else None,
            'job_id':row.job_id,'task':billing_workbench.job_view(job) if job else None,'status':'missing' if not job else 'blocked' if job.policy_blocked else job.status})
    aggregate=batches.aggregate([SimpleNamespace(plan_job_id=row.job_id) for row,_,_ in rows],jobs,'submitted')
    return {'id':run.id,'batch_id':batch.id,'quality':run.quality,'parent_job_id':run.parent_job_id,'aggregate':aggregate,'items':items,'created_at':utc(run.created_at),
        'note':'本轮固定版本独立保留。复用的是同一原任务，取消/恢复会影响其他引用它的生成记录；取消只停止本地执行，远端可能继续运行并产生费用，仍需对账。'}


def phase_tasks(db,batch,phase_id):
    if phase_id=='plan':return {row.client_key:row.plan_job_id for row in batches.items_for(db,batch)}
    run=run_for(db,batch,phase_id)
    return {item.client_key:row.job_id for row,item,_ in run_items(db,run)}


def apply_actions(db,batch,phase_id,actor,key,request):
    body=request.model_dump(mode='json');body['items']=sorted(body['items'],key=lambda item:item['key']);fingerprint=digest({'batch_id':batch.id,'phase_id':phase_id,'actor':actor or 'development_user','body':body})
    prior=db.scalar(select(models.BatchTaskAction).where(models.BatchTaskAction.tenant_id==batch.tenant_id,models.BatchTaskAction.idempotency_key==key))
    if prior:
        if prior.request_hash!=fingerprint:raise EditingError('batch_task_action_conflict','原操作标识已用于不同子项或条件',409)
        return prior
    mapping=phase_tasks(db,batch,phase_id)
    if any(mapping.get(item['key'])!=item['job_id'] for item in body['items']):raise EditingError('batch_task_missing','有原任务不属于所选批次阶段',404)
    receipt=models.BatchTaskAction(tenant_id=batch.tenant_id,batch_id=batch.id,phase_id=phase_id,idempotency_key=key,request_hash=fingerprint,actor_id=actor or 'development_user',result_json={});db.add(receipt);db.flush()
    ids=sorted(item['job_id'] for item in body['items'])
    jobs={job.id:job for job in db.scalars(select(models.Job).where(models.Job.tenant_id==batch.tenant_id,models.Job.project_id==batch.project_id,models.Job.id.in_(ids)).order_by(models.Job.id).with_for_update().execution_options(populate_existing=True))}
    if len(jobs)!=len(ids):raise EditingError('batch_task_missing','部分原任务已不可用',404)
    # Acquire every Job/Workflow before any cancellation reaches TenantBudget.
    # A per-item lock/budget loop would deadlock another completing child.
    list(db.scalars(select(models.WorkflowRun).where(models.WorkflowRun.job_id.in_(ids)).order_by(models.WorkflowRun.job_id,models.WorkflowRun.id).with_for_update()))
    results=[]
    for item in body['items']:
        job=jobs[item['job_id']];view=billing_workbench.job_view(job);reason=None
        if (job.status,job.attempt_count,job.execution_generation)!=(item['expected_status'],item['expected_attempt'],item['expected_generation']):reason='state_changed'
        elif not view['can_retry' if request.action=='retry' else 'can_cancel']:reason='not_actionable'
        if reason:results.append({'key':item['key'],'job_id':job.id,'applied':False,'reason':reason});continue
        condition=JobActionRequest(action=request.action,expected_status=job.status,expected_attempt=job.attempt_count,expected_generation=job.execution_generation)
        try:
            with db.begin_nested():
                action=billing_workbench.apply_action(db,job,actor,'batch-action:'+receipt.id+':'+job.id,condition);db.flush();fixed=dict(action.result_json)
            results.append({'key':item['key'],'job_id':job.id,'applied':True,'receipt':fixed})
        except EditingError:results.append({'key':item['key'],'job_id':job.id,'applied':False,'reason':'action_rejected'})
    receipt.result_json={'id':receipt.id,'batch_id':batch.id,'phase_id':phase_id,'action':request.action,'items':results,
        'note':'仅处理明确选中的原任务，成功项保留。复用任务的其他引用也会受影响。取消不表示退款；共用分析和已提交的远端任务可能继续，费用仍需对账。'};db.flush();return receipt


def reconcile_runs(db,*,limit=50):
    root=aliased(models.Job);child=aliased(models.Job)
    changed=select(models.BatchRunItem.id).join(child,child.id==models.BatchRunItem.job_id).where(models.BatchRunItem.run_id==models.BatchRun.id,
        or_(models.BatchRun.aggregate_watermark.is_(None),child.updated_at>models.BatchRun.aggregate_watermark)).exists()
    ids=list(db.scalars(select(models.BatchRun.id).join(root,root.id==models.BatchRun.parent_job_id).where(or_(root.status=='running',changed)).order_by(root.updated_at,models.BatchRun.id).limit(limit)))
    results=[]
    for id in ids:
        run=db.get(models.BatchRun,id);parent=db.scalar(select(models.Job).where(models.Job.id==run.parent_job_id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if not parent or parent.type!='batch.render' or parent.tenant_id!=run.tenant_id:continue
        rows=run_items(db,run);metadata=task_rows(db,[row.job_id for row,_,_ in rows if row.job_id],run.tenant_id)
        # Use the exact metadata snapshot used to derive the summary; a later
        # read could miss a completion occurring between these two reads.
        summary=batches.aggregate([SimpleNamespace(plan_job_id=row.job_id) for row,_,_ in rows],metadata,'submitted')
        run.aggregate_watermark=max((job.updated_at for job in metadata.values()),default=run.created_at)
        parent.status='failed' if summary['status'] in {'attention','partial_success'} else summary['status'];parent.updated_at=datetime.now(timezone.utc);parent.retryable=False
        parent.failure_code='batch_items_need_attention' if parent.status=='failed' else None;parent.completed_at=None if parent.status=='running' else parent.updated_at
        parent.payload={**parent.payload,'aggregate':summary};step=db.scalar(select(models.JobStep).where(models.JobStep.job_id==parent.id,models.JobStep.name=='batch.aggregate'))
        if step:step.status=parent.status;step.progress=summary['progress'];step.completed_at=parent.completed_at
        db.commit();results.append({'job_id':parent.id,'status':parent.status})
    return results
