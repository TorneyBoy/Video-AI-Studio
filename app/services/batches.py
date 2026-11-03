"""Versioned batch configuration and atomic, deduplicated planning submission.

Each submitted item owns a normal editable plan Job. Shared analysis uses one
internal preparation envelope, not a loop of independently committed requests.
This module never calls a provider and leaves commit/rollback to its caller.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime,timezone
import json
from sqlalchemy import select,or_
from sqlalchemy.orm import aliased
from .. import models,schemas
from ..batch_schemas import BatchCreate,BatchSave
from ..billing_schemas import AnalysisPreparationCreate
from . import preparation_quotes as preparation
from .delivery_packages import utc
from .editing import digest
from .editing_errors import EditingError


def batch_for(db,tenant,id,*,lock=False):
    stmt=select(models.ProductionBatch).where(models.ProductionBatch.id==id,models.ProductionBatch.tenant_id==tenant)
    if lock:stmt=stmt.with_for_update().execution_options(populate_existing=True)
    row=db.scalar(stmt)
    if not row:raise EditingError('batch_missing','未找到当前空间的批次',404)
    return row


def items_for(db,batch):
    return list(db.scalars(select(models.BatchItem).where(models.BatchItem.batch_id==batch.id,models.BatchItem.tenant_id==batch.tenant_id).order_by(models.BatchItem.position,models.BatchItem.id).execution_options(populate_existing=True)))


def draft_body(payload):
    return {'name':payload.name.strip(),'shared':payload.shared.model_dump(mode='json'),
        'items':[{'key':row.key,'name':row.name.strip(),'overrides':row.overrides.model_dump(mode='json',exclude_unset=True)} for row in payload.items]}


def draft_snapshot(db,batch):
    return {'name':batch.name,'shared':batch.shared_json,'items':[{'key':row.client_key,'name':row.name,'overrides':row.overrides_json} for row in items_for(db,batch)]}


def _write_items(db,batch,body):
    existing={row.client_key:row for row in items_for(db,batch)}
    for position,item in enumerate(body['items']):
        row=existing.pop(item['key'],None)
        if row is None:
            row=models.BatchItem(batch_id=batch.id,tenant_id=batch.tenant_id,client_key=item['key']);db.add(row)
        row.position=position;row.name=item['name'];row.overrides_json=deepcopy(item['overrides'])
    for row in existing.values():db.delete(row)
    db.flush()


def create_batch(db,tenant,actor,payload: BatchCreate,key):
    body=draft_body(payload);fingerprint=digest({'project_id':payload.project_id,**body})
    prior=db.scalar(select(models.ProductionBatch).where(models.ProductionBatch.tenant_id==tenant,models.ProductionBatch.idempotency_key==key))
    if prior:
        if prior.request_hash!=fingerprint:raise EditingError('batch_create_conflict','原标识已用于不同批次设置，请核对原创建',409)
        return prior
    project=db.scalar(select(models.Project.id).where(models.Project.id==payload.project_id,models.Project.tenant_id==tenant))
    if not project:raise EditingError('batch_project_missing','未找到当前空间的项目',404)
    if len(json.dumps(body,ensure_ascii=False).encode())>1024*1024:raise EditingError('batch_config_limit','批次配置超过1MiB，请减少本次项数或素材引用',422)
    row=models.ProductionBatch(tenant_id=tenant,project_id=project,name=body['name'],shared_json=body['shared'],revision=1,
        idempotency_key=key,request_hash=fingerprint,actor_id=actor or 'development_user')
    db.add(row);db.flush();_write_items(db,row,body)
    db.add(models.BatchRevision(batch_id=row.id,number=1,idempotency_key=key,request_hash=fingerprint,snapshot_json=body,actor_id=row.actor_id));db.flush();return row


def save_batch(db,batch,actor,payload: BatchSave,key):
    body=draft_body(payload);fingerprint=digest({'base_revision':payload.base_revision,**body})
    prior=db.scalar(select(models.BatchRevision).where(models.BatchRevision.batch_id==batch.id,models.BatchRevision.idempotency_key==key))
    if prior:
        if prior.request_hash!=fingerprint:raise EditingError('batch_save_conflict','原保存标识已用于不同配置，请核对原提交',409)
        return prior
    if batch.state!='draft':raise EditingError('batch_immutable','已提交批次保留固定配置；另建批次才能更改输入',409)
    if batch.revision!=payload.base_revision:raise EditingError('batch_revision_conflict','批次已在另一页面更新，请保留输入并载入当前版本核对',409,{'head_revision':batch.revision})
    if len(json.dumps(body,ensure_ascii=False).encode())>1024*1024:raise EditingError('batch_config_limit','批次配置超过1MiB，请缩小范围',422)
    batch.name=body['name'];batch.shared_json=body['shared'];batch.revision+=1;_write_items(db,batch,body)
    row=models.BatchRevision(batch_id=batch.id,number=batch.revision,idempotency_key=key,request_hash=fingerprint,snapshot_json=body,actor_id=actor or 'development_user');db.add(row);db.flush();return row


def revision_receipt(batch,row):
    return {'found':row is not None,'batch_id':batch.id,'revision':row.number if row else None,'head_revision':batch.revision,
        'snapshot':row.snapshot_json if row else None}


def resolve(db,batch):
    rows=items_for(db,batch);effective=[{**batch.shared_json,**row.overrides_json} for row in rows];issues=[];resolved=[]
    union=sorted({id for settings in effective for id in settings['asset_ids']})
    if not rows:issues.append({'key':None,'field':'items','message':'请至少新增一项任务'})
    if len(union)>100:issues.append({'key':None,'field':'asset_ids','message':'本次批次最多引用100个不同素材，请拆分批次'})
    assets=list(db.scalars(select(models.Asset).where(models.Asset.tenant_id==batch.tenant_id,models.Asset.project_id==batch.project_id,models.Asset.id.in_(union)))) if union and len(union)<=100 else []
    by_id={row.id:row for row in assets};templates={}
    for row,settings in zip(rows,effective):
        ids=sorted(set(settings['asset_ids']));start=len(issues);settings={**settings,'asset_ids':ids}
        if not settings['template_id']:issues.append({'key':row.client_key,'field':'template_id','message':'请选择共用模板或为本项选择模板'})
        if not ids:issues.append({'key':row.client_key,'field':'asset_ids','message':'请选择共用素材或为本项分配素材'})
        for id in ids:
            asset=by_id.get(id)
            if not asset:issues.append({'key':row.client_key,'field':'asset_ids','message':'有素材不存在或不属于当前项目'});break
            storage=preparation.obj(preparation.obj(asset.metadata_json).get('storage'));upload=preparation.obj(preparation.obj(asset.metadata_json).get('upload'))
            if not asset.kind.startswith(('video','image')) or asset.uri.startswith('upload://') or storage.get('mode')=='external_presigned' and storage.get('status')!='ready' or upload.get('status')=='created':
                issues.append({'key':row.client_key,'field':'asset_ids','message':'存在未就绪素材或非视频/图片素材'});break
        if len(issues)>start:continue
        config=schemas.AutoEditCreate(project_id=batch.project_id,**settings).model_dump(mode='json',exclude={'quote_id','idempotency_key'})
        template_key=digest({key:config[key] for key in ('template_id','target_duration','aspect_ratio')})
        try:
            if template_key not in templates:templates[template_key]=preparation._template(db,batch.tenant_id,config)
        except EditingError:
            issues.append({'key':row.client_key,'field':'template_id','message':'模板不属于当前项目或设置无效'});continue
        resolved.append({'item_id':row.id,'key':row.client_key,'position':row.position,'name':row.name or f'第{row.position+1}项',
            'config':config,'template':deepcopy(templates[template_key])})
    return {'valid':not issues,'revision':batch.revision,'item_count':len(rows),'unique_asset_count':len(union),'issues':issues},resolved


def _metadata(db,batch):
    return {row.id:row for row in db.execute(select(models.Job.id,models.Job.status,models.Job.policy_blocked,models.Job.failure_code,models.Job.updated_at,
        models.Job.payload['edit_document_id'].as_string().label('document_id'),models.Job.payload['plan_revision_id'].as_string().label('revision_id'))
        .join(models.BatchItem,models.BatchItem.plan_job_id==models.Job.id).where(models.BatchItem.batch_id==batch.id,models.Job.tenant_id==batch.tenant_id))}


def aggregate(items,jobs,state):
    counts=Counter()
    for item in items:
        job=jobs.get(item.plan_job_id)
        status='draft' if state=='draft' else 'missing' if not job else 'blocked' if job.policy_blocked else job.status
        counts[status]+=1
    total=len(items);active=sum(counts[key] for key in ('queued','running'));successful=counts['succeeded'];attention=sum(counts[key] for key in ('failed','blocked','missing'));cancelled=counts['cancelled']
    status='draft' if state=='draft' else 'running' if active else 'succeeded' if successful==total and total else 'cancelled' if cancelled==total and total else 'partial_success' if successful else 'attention'
    return {'status':status,'counts':dict(counts),'total':total,'finished':successful+attention+cancelled,'succeeded':successful,'attention':attention,'cancelled':cancelled,
        'progress':0 if state=='draft' or not total else (successful+attention+cancelled)/total}


def batch_read(db,batch,*,include_items=True):
    rows=items_for(db,batch);jobs=_metadata(db,batch);summary=aggregate(rows,jobs,batch.state)
    out={'id':batch.id,'project_id':batch.project_id,'name':batch.name,'state':batch.state,'revision':batch.revision,'shared':batch.shared_json,
        'parent_job_id':batch.parent_job_id,'aggregate':summary,'created_at':utc(batch.created_at),'updated_at':utc(batch.updated_at)}
    if include_items:
        from .dispatch import batch_status
        out['execution']=batch_status(db,batch)
        from .batch_runs import task_rows
        from .billing_workbench import job_view
        tasks=task_rows(db,[row.plan_job_id for row in rows if row.plan_job_id],batch.tenant_id)
        heads={row.origin_job_id:row for row in db.execute(select(models.EditDocument.origin_job_id,models.EditDocument.id.label('document_id'),models.EditHead.revision_id,models.PlanRevision.number)
            .join(models.EditHead,models.EditHead.document_id==models.EditDocument.id).join(models.PlanRevision,models.PlanRevision.id==models.EditHead.revision_id)
            .where(models.EditDocument.tenant_id==batch.tenant_id,models.EditDocument.project_id==batch.project_id,models.PlanRevision.tenant_id==batch.tenant_id,
                models.EditDocument.origin_job_id.in_([row.plan_job_id for row in rows if row.plan_job_id])))}
        out['items']=[]
        for row in rows:
            job=jobs.get(row.plan_job_id);head=heads.get(row.plan_job_id);task=tasks.get(row.plan_job_id)
            out['items'].append({'id':row.id,'key':row.client_key,'position':row.position,'name':row.name,'overrides':row.overrides_json,
                'effective':{**batch.shared_json,**row.overrides_json},'job_id':row.plan_job_id,
                'status':'draft' if batch.state=='draft' else 'missing' if not job else 'blocked' if job.policy_blocked else job.status,
                'failure_code':task.failure_code if task else None,'document_id':head.document_id if head else None,'revision_id':head.revision_id if head else None,
                'revision_number':head.number if head else None,'task':job_view(task) if task else None})
    return out


def create_quote(db,batch,revision,key):
    prior=db.scalar(select(models.BatchQuote).where(models.BatchQuote.tenant_id==batch.tenant_id,models.BatchQuote.idempotency_key==key))
    if prior:
        if prior.batch_id!=batch.id or prior.revision!=revision:raise EditingError('batch_quote_conflict','原报价标识已用于不同批次版本',409)
        return prior
    if batch.state!='draft' or batch.revision!=revision:raise EditingError('batch_revision_conflict','批次已更新或已提交，请载入当前版本',409)
    report,resolved=resolve(db,batch)
    if not report['valid']:raise EditingError('batch_validation_failed','请补齐所列任务的配置',422,report)
    ids=sorted({id for row in resolved for id in row['config']['asset_ids']})
    if len(json.dumps(resolved,ensure_ascii=False).encode())>16*1024*1024:raise EditingError('batch_snapshot_limit','模板快照超过16MiB，请拆分本次批次',422)
    quote=preparation.create_quote(db,batch.tenant_id,'batch_analysis',AnalysisPreparationCreate(asset_ids=ids,transcribe=False),'batch-analysis-quote:'+batch.id+':'+digest(key))
    row=models.BatchQuote(tenant_id=batch.tenant_id,batch_id=batch.id,revision=revision,idempotency_key=key,
        analysis_quote_id=quote.id,snapshot_json={'items':resolved,'draft_hash':digest(draft_snapshot(db,batch))},expires_at=quote.expires_at)
    db.add(row);db.flush();return row


def quote_for(db,batch,id,*,lock=False):
    stmt=select(models.BatchQuote).where(models.BatchQuote.id==id,models.BatchQuote.batch_id==batch.id,models.BatchQuote.tenant_id==batch.tenant_id)
    if lock:stmt=stmt.with_for_update().execution_options(populate_existing=True)
    row=db.scalar(stmt)
    if not row:raise EditingError('batch_quote_missing','未找到本批次报价',404)
    return row


def quote_read(db,batch,row):
    analysis=preparation.quote_read(db,db.get(models.PreparationQuote,row.analysis_quote_id));used=set();items=[];sources={line['asset_id']:line for line in analysis['items']}
    for item in row.snapshot_json['items']:
        allocated={};counts=Counter()
        for id in item['config']['asset_ids']:
            line=sources[id];counts[line['disposition']]+=1
            if id not in used and line['disposition']=='new':allocated[line['currency']]=allocated.get(line['currency'],0)+int(line['amount_micros'])
            used.add(id)
        items.append({'key':item['key'],'name':item['name'],'asset_count':len(item['config']['asset_ids']),'analysis_counts':dict(counts),
            'allocated_totals':[{'currency':ccy,'amount_micros':amount} for ccy,amount in sorted(allocated.items())]})
    current=batch.state=='draft' and batch.revision==row.revision;status='consumed' if row.consumed_key else 'changed' if not current else analysis['status']
    return {'id':row.id,'batch_id':batch.id,'project_id':batch.project_id,'revision':row.revision,'item_count':len(items),'unique_asset_count':len(sources),
        'status':status,'expires_at':utc(row.expires_at),'items':items,'analysis':analysis['items'],'totals':analysis['totals'],
        'can_submit':current and not row.consumed_key and analysis['can_submit'],'reasons':analysis['reasons']+([] if current or row.consumed_key else ['batch_changed']),
        'result':row.result_json,'note':'共用素材的新增分析只计一次，逐项预占按首次引用分配。确认后每项生成独立剪辑计划；渲染另行报价。预占不是最终账单。'}


def confirm_quote(db,batch,row,revision,key):
    if row.revision!=revision:raise EditingError('batch_revision_conflict','确认版本与原报价不同',409)
    if row.consumed_key:
        if row.consumed_key!=key:raise EditingError('batch_quote_used','报价已用于原提交，请核对原操作',409)
        return row.result_json
    if batch.state!='draft' or batch.revision!=revision or row.snapshot_json['draft_hash']!=digest(draft_snapshot(db,batch)):
        raise EditingError('batch_revision_conflict','批次配置已变化，请重新报价',409)
    if utc(row.expires_at)<=datetime.now(timezone.utc):raise EditingError('batch_quote_expired','报价已过期，请重新获取',409)
    for item in row.snapshot_json['items']:
        if preparation._template(db,batch.tenant_id,item['config'])!=item['template']:raise EditingError('batch_template_changed','模板已变化，请重新报价',409)
    analysis=preparation.consume_quote(db,batch.tenant_id,row.analysis_quote_id,'batch-analysis:'+row.id)
    if db.scalar(select(models.BatchQuote.id).where(models.BatchQuote.tenant_id==batch.tenant_id,models.BatchQuote.consumed_key==key)) or db.scalar(select(models.Job.id).where(models.Job.tenant_id==batch.tenant_id,models.Job.idempotency_key==key)):
        raise EditingError('batch_confirm_conflict','该提交标识已用于其他操作，请核对原提交',409)
    parent=models.Job(tenant_id=batch.tenant_id,project_id=batch.project_id,type='batch.plan',status='running',idempotency_key=key,max_attempts=1,
        started_at=datetime.now(timezone.utc),payload={'batch_id':batch.id,'batch_quote_id':row.id,'batch_revision':revision})
    db.add(parent);db.flush();items={item.id:item for item in items_for(db,batch)};source_jobs={item['asset_id']:item.get('job_id') for item in analysis.result_json['items']};results=[]
    for fixed in row.snapshot_json['items']:
        item=items[fixed['item_id']];config=fixed['config']
        job=models.Job(tenant_id=batch.tenant_id,project_id=batch.project_id,type='auto_edit.plan',status='queued',max_attempts=3,
            idempotency_key='batch-plan:'+batch.id+':'+item.id,payload={'project_id':batch.project_id,'template_id':config['template_id'],
                'asset_ids':config['asset_ids'],'quality':'draft','template_snapshot':fixed['template'],'analysis_job_ids':[source_jobs[id] for id in config['asset_ids'] if source_jobs.get(id)],
                'preparation_quote_id':analysis.id,'batch_quote_id':row.id,'batch_id':batch.id,'batch_item_id':item.id,'parent_job_id':parent.id,
                'planning_request_hash':digest({key:value for key,value in config.items() if key!='max_attempts'}),
                'plan':{'template_id':fixed['template']['id'],'template_version':fixed['template']['version'],'selected':[],'warnings':['分析就绪后生成固定版本计划']}})
        db.add(job);db.flush();item.plan_job_id=job.id;item.config_snapshot=deepcopy(fixed);results.append({'key':item.client_key,'item_id':item.id,'job_id':job.id})
    db.add(models.JobStep(tenant_id=batch.tenant_id,job_id=parent.id,name='batch.aggregate',sequence=0,status='running',progress=0,payload={},started_at=parent.started_at))
    batch.state='submitted';batch.parent_job_id=parent.id;batch.submission_quote_id=row.id;row.consumed_key=key
    row.result_json={'batch_id':batch.id,'parent_job_id':parent.id,'revision':revision,'items':results};db.flush();return row.result_json


def valid_plan_binding(db,job,analysis_quote):
    quote=db.get(models.BatchQuote,(job.payload or {}).get('batch_quote_id'))
    item=db.get(models.BatchItem,(job.payload or {}).get('batch_item_id'))
    batch=db.get(models.ProductionBatch,quote.batch_id) if quote else None
    return bool(quote and item and batch and quote.tenant_id==job.tenant_id and item.tenant_id==job.tenant_id
        and quote.analysis_quote_id==analysis_quote.id and quote.consumed_key and batch.submission_quote_id==quote.id
        and item.batch_id==batch.id and item.plan_job_id==job.id and batch.parent_job_id==(job.payload or {}).get('parent_job_id')
        and item.config_snapshot and item.config_snapshot['config']['asset_ids']==(job.payload or {}).get('asset_ids')
        and item.config_snapshot['template']==(job.payload or {}).get('template_snapshot'))


def reconcile_batches(db,*,limit=50):
    """Derived parents have no provider calls or independently retryable work."""
    child=aliased(models.Job);root=aliased(models.Job)
    changed=select(models.BatchItem.id).join(child,child.id==models.BatchItem.plan_job_id).where(models.BatchItem.batch_id==models.ProductionBatch.id,
        or_(models.ProductionBatch.aggregate_watermark.is_(None),child.updated_at>models.ProductionBatch.aggregate_watermark)).exists()
    ids=list(db.scalars(select(models.ProductionBatch.id).join(root,root.id==models.ProductionBatch.parent_job_id).where(models.ProductionBatch.state=='submitted',
        or_(root.status=='running',changed)).order_by(root.updated_at,models.ProductionBatch.id).limit(limit)))
    results=[]
    for id in ids:
        batch=db.get(models.ProductionBatch,id);parent=db.scalar(select(models.Job).where(models.Job.id==batch.parent_job_id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if not parent or parent.type!='batch.plan' or parent.tenant_id!=batch.tenant_id:continue
        metadata=_metadata(db,batch);summary=aggregate(items_for(db,batch),metadata,batch.state)
        batch.aggregate_watermark=max((job.updated_at for job in metadata.values()),default=batch.created_at)
        parent.status='failed' if summary['status'] in {'partial_success','attention'} else summary['status'];parent.updated_at=datetime.now(timezone.utc)
        parent.retryable=False;parent.failure_code='batch_items_need_attention' if parent.status=='failed' else None
        parent.completed_at=None if parent.status=='running' else parent.updated_at;parent.payload={**parent.payload,'aggregate':summary}
        step=db.scalar(select(models.JobStep).where(models.JobStep.job_id==parent.id,models.JobStep.name=='batch.aggregate'))
        if step:step.status=parent.status;step.progress=summary['progress'];step.completed_at=parent.completed_at
        db.commit();results.append({'job_id':parent.id,'status':parent.status})
    return results
