"""Analysis/plan envelopes: immutable inputs, bounded prices and atomic confirmation.

This module reads metadata only. Quote creation never creates media jobs or
reservations. Consumption locks assets in ID order, never locks reused Jobs,
and preserves the Job -> Asset -> Tenant order used by analysis completion.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from types import SimpleNamespace
from sqlalchemy import select
from sqlalchemy.orm import Session
from .. import models,schemas
from ..billing_schemas import AnalysisPreparationCreate
from ..config import get_settings
from .analysis_contract import require_planning_analysis
from .budgets import (_decision,_sum_ledger,_RESERVATION_STATUSES,capability_reservation,get_budget,normalize_cost,period_key,preflight_job_budget)
from .capabilities import capability_catalog
from .delivery_packages import utc
from .editing import digest
from .editing_errors import EditingError
from .provider_registry import ProviderRegistryError
from .providers import ProviderExecutionError
from timeline_engine import TemplateDefinition

MAX_SNAPSHOT=16*1024*1024
def obj(value):return value if isinstance(value,dict) else {}


def source_hash(asset) -> str:
    meta=obj(asset.metadata_json);storage=obj(meta.get('storage'));upload=obj(meta.get('upload'))
    return digest({'id':asset.id,'tenant':asset.tenant_id,'project':asset.project_id,'kind':asset.kind,'uri':asset.uri,
        'storage':{key:storage.get(key) for key in ('mode','status','size_bytes','sha256','version_id','object_version')},
        'upload_status':upload.get('status')})


def analysis_options(payload) -> dict:
    if obj(getattr(payload,'options',{})):raise EditingError('analysis_options_unsupported','当前报价仅支持已列出的分析设置，不接受额外执行参数',422)
    return {'shot_analysis':bool(payload.shot_analysis),'transcribe':bool(payload.transcribe or getattr(payload,'run_transcription',False)),
        'whisper_model':payload.whisper_model,'timeout_seconds':payload.timeout_seconds}


def analysis_request(asset_id: str,payload: schemas.AssetAnalyzeCreate) -> dict:
    return {'asset_ids':[asset_id],**analysis_options(payload),'max_attempts':payload.max_attempts,'force_new':payload.force_new}


def normalize(kind: str,request) -> dict:
    body=request.model_dump(mode='json',exclude={'quote_id','idempotency_key'}) if hasattr(request,'model_dump') else deepcopy(request)
    if kind in {'analysis','batch_analysis'}:body=AnalysisPreparationCreate.model_validate(body).model_dump(mode='json')
    else:body=schemas.AutoEditCreate.model_validate(body).model_dump(mode='json',exclude={'quote_id','idempotency_key'})
    body['asset_ids']=sorted(set(body['asset_ids']));return body


def _assets(db,tenant,ids,project=None,lock=False):
    query=select(models.Asset).where(models.Asset.tenant_id==tenant)
    if ids:query=query.where(models.Asset.id.in_(ids))
    elif project:query=query.where(models.Asset.project_id==project)
    else:raise EditingError('preparation_assets_required','请至少选择一个素材',422)
    query=query.order_by(models.Asset.id)
    if lock:query=query.with_for_update().execution_options(populate_existing=True)
    rows=list(db.scalars(query.limit(101)))
    if not rows or len(rows)>100 or ids and len(rows)!=len(ids):raise EditingError('preparation_assets_invalid','素材不存在、数量超过100或不属于当前空间',422)
    if project is not None and any(row.project_id!=project for row in rows):raise EditingError('preparation_project_mismatch','请选择当前项目的素材',404)
    if len({row.project_id for row in rows})>1:raise EditingError('preparation_project_mismatch','单次报价的素材须属于同一项目',422)
    for row in rows:
        storage=obj(obj(row.metadata_json).get('storage'));upload=obj(obj(row.metadata_json).get('upload'))
        if storage.get('mode')=='external_presigned' and storage.get('status')!='ready' or upload.get('status')=='created':
            raise EditingError('preparation_upload_incomplete','请先完成素材上传核验',409)
    return rows


def _template(db,tenant,body):
    project=db.scalar(select(models.Project).where(models.Project.id==body['project_id'],models.Project.tenant_id==tenant))
    template=db.scalar(select(models.Template).where(models.Template.id==body['template_id'],models.Template.tenant_id==tenant))
    if not project or not template or template.project_id not in {None,project.id}:raise EditingError('preparation_template_missing','未找到本项目可用模板',404)
    try:
        value=TemplateDefinition.model_validate(template.content)
        if body.get('target_duration') is not None:value.duration=body['target_duration']
        if body.get('aspect_ratio'):value.metadata={**value.metadata,'resolution':{'9:16':{'width':1080,'height':1920},'16:9':{'width':1920,'height':1080},'1:1':{'width':1080,'height':1080}}[body['aspect_ratio']]}
        return value.model_dump(mode='json')
    except ValueError as exc:raise EditingError('preparation_template_invalid','模板设置不可用，请先修订',422) from exc


def _ready(asset,options,planning):
    analysis=obj(obj(asset.metadata_json).get('analysis'))
    if not analysis:return False
    if get_settings().app_environment=='production':
        try:require_planning_analysis(analysis,source_uri=asset.uri,kind=asset.kind)
        except ProviderExecutionError:return False
    if options['shot_analysis'] and asset.kind.startswith('video') and not analysis.get('shots'):return False
    if options['transcribe'] and obj(analysis.get('transcription')).get('status')!='ok':return False
    return bool(analysis.get('shots') or obj(analysis.get('media')).get('status')=='ok')


def _matches(job,asset,options,planning):
    payload=obj(job.payload);old=obj(payload.get('options'))
    if payload.get('uri')!=asset.uri or payload.get('analysis_source_hash') not in {None,source_hash(asset)}:return False
    if options['shot_analysis'] and not old.get('shot_analysis',True):return False
    if planning:return True  # Existing ASR work can also supply required shots.
    return (bool(old.get('shot_analysis',True))==options['shot_analysis']
        and bool(old.get('transcribe') or old.get('run_transcription'))==options['transcribe']
        and old.get('whisper_model','small')==options['whisper_model'] and old.get('timeout_seconds',30)==options['timeout_seconds']
        and not set(old)-{'shot_analysis','transcribe','run_transcription','whisper_model','timeout_seconds'})


def _job_query():
    return select(models.Job.id,models.Job.tenant_id,models.Job.status,models.Job.policy_blocked,
        models.Job.payload['uri'].as_string().label('uri'),models.Job.payload['options'].label('options'),
        models.Job.payload['analysis_source_hash'].as_string().label('source_hash'))


def _job_metadata(row):
    return SimpleNamespace(id=row.id,tenant_id=row.tenant_id,status=row.status,policy_blocked=row.policy_blocked,
        payload={'uri':row.uri,'options':row.options,'analysis_source_hash':row.source_hash}) if row else None


def _job_meta(db,id):return _job_metadata(db.execute(_job_query().where(models.Job.id==id)).first())


def _previous(db,tenant,asset,options,planning):
    # Tenant/asset/URI narrows the query; new hashes and legacy safe options
    # are checked without trusting an arbitrary client estimated cost.
    rows=db.execute(_job_query().where(models.Job.tenant_id==tenant,models.Job.type=='asset.analyze',
        models.Job.payload['asset_id'].as_string()==asset.id,models.Job.payload['uri'].as_string()==asset.uri).order_by(models.Job.created_at.desc(),models.Job.id.desc()))
    return next((job for row in rows if _matches(job:=_job_metadata(row),asset,options,planning)),None)


def _reuse_problem(db,job):
    if job.status in {'failed','cancelled'}:return '原分析任务未完成，请先查看或恢复原任务'
    if job.policy_blocked:return '原分析任务仍受费用或交付策略限制'
    return None


def _new_blocker(db,job):
    if not job:return None
    operation=db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id==job.id))
    if job.status in {'queued','running'} or operation and operation.state in {'submitting','unknown','pending'}:
        return '原分析仍在处理或提交结果未知，请先查询原任务'
    return None


def _active_source_job(db,tenant,asset):
    # Different ASR settings must not bypass an uncertain submission of the
    # same object. Complete or reconcile that operation before buying more.
    rows=db.execute(_job_query().where(models.Job.tenant_id==tenant,models.Job.type=='asset.analyze',
        models.Job.payload['asset_id'].as_string()==asset.id,models.Job.payload['uri'].as_string()==asset.uri))
    return next((job for row in rows if _new_blocker(db,job:=_job_metadata(row))),None)


def analysis_capability(asset,options,preferred=None):
    try:entries=capability_catalog()['capabilities']
    except (ProviderRegistryError,ValueError,OSError) as exc:raise EditingError('analysis_price_unavailable','分析服务尚未提供可用的已验收报价',422) from exc
    kind=asset.kind.split('/')[0];choices=[]
    for entry in entries:
        if not entry['available'] or entry['capability']!='media_analysis':continue
        if preferred and any(entry.get(key)!=preferred.get(key) for key in ('provider','model_id','model_version')):continue
        declared={value.replace('remote_','').split('/')[0] for value in entry.get('input_types',[]) if value!='json'}
        if declared and kind not in declared:continue
        limits=obj(entry.get('limits'));features=set(limits.get('features') or [])
        if features and options['shot_analysis'] and not features & {'abrupt_shots','shots','shot_analysis'}:continue
        if features and options['transcribe'] and not features & {'timestamped_asr','asr','transcription'}:continue
        meta=obj(asset.metadata_json);storage=obj(meta.get('storage'));media=obj(obj(meta.get('analysis')).get('media'))
        duration=media.get('duration_seconds') or meta.get('duration');size=storage.get('size_bytes')
        if limits.get('max_duration_seconds') and isinstance(duration,(int,float)) and duration>limits['max_duration_seconds']:continue
        if limits.get('max_input_bytes') and isinstance(size,(int,float)) and size>limits['max_input_bytes']:continue
        choices.append(entry)
    if not choices:raise EditingError('analysis_price_unavailable','没有已验收服务覆盖所选素材类型、时长或分析设置',422)
    return sorted(choices,key=lambda row:row['id'])[0]


def _item(db,tenant,asset,options,planning,force):
    base={'asset_id':asset.id,'name':asset.name,'kind':asset.kind,'source_hash':source_hash(asset),'options':options}
    previous=_previous(db,tenant,asset,options,planning)
    if not force and _ready(asset,options,planning):
        analysis=deepcopy(obj(asset.metadata_json)['analysis'])
        return {**base,'disposition':'cached','analysis_snapshot':analysis,'analysis_hash':digest(analysis),'job_id':None,'problem':None}
    if previous and not force:
        problem=_reuse_problem(db,previous)
        if previous.status=='succeeded' and not _ready(asset,options,planning):problem='原分析未满足所需结果，请查看结果或明确重新分析'
        return {**base,'disposition':'reused','job_id':previous.id,'problem':problem}
    active=_active_source_job(db,tenant,asset)
    problem=_new_blocker(db,active) if active else None
    selected=analysis_capability(asset,options);envelope=capability_reservation(selected);amount,currency=normalize_cost(envelope)
    return {**base,'disposition':'new','job_id':None,'problem':problem,'capability':selected,'capability_hash':digest(selected),
            'amount_micros':amount,'currency':currency}


def _fingerprint(body,assets,template):
    return digest({'request':{key:value for key,value in body.items() if key!='max_attempts'},
                   'sources':{row.id:source_hash(row) for row in assets},'template':template})


def affordability(db,tenant,project,items):
    budget=get_budget(db,tenant);period=period_key();groups={};reasons=[]
    for item in items:
        if item['disposition']=='new':groups.setdefault(item['currency'],[]).append(item['amount_micros'])
        if item.get('problem'):reasons.append('original_analysis_unresolved')
    for currency,amounts in groups.items():
        if budget and budget.enabled and currency!=budget.currency:reasons.append('currency_mismatch');continue
        if budget and budget.enabled and budget.task_limit_micros is not None and any(amount>budget.task_limit_micros for amount in amounts):reasons.append('task_limit')
        policy=SimpleNamespace(currency=budget.currency,enabled=budget.enabled,monthly_limit_micros=budget.monthly_limit_micros,task_limit_micros=None) if budget else None
        stub=models.Job(id='preparation-not-a-job',tenant_id=tenant,project_id=project,type='preparation.quote')
        decision=_decision(budget=policy,amount_micros=sum(amounts),currency=currency,
            monthly_used_micros=_sum_ledger(db,tenant_id=tenant,period=period,statuses=('settled',)),
            reserved_micros=_sum_ledger(db,tenant_id=tenant,period=period,statuses=_RESERVATION_STATUSES),
            period=period,job_id=stub.id,db=db,job=stub)
        reasons.extend(decision.audit['reasons'])
    return not reasons,sorted(set(reasons))


def create_quote(db: Session,tenant: str,kind: str,request,key: str):
    body=normalize(kind,request);template=_template(db,tenant,body) if kind=='plan' else None
    assets=_assets(db,tenant,body['asset_ids'],body.get('project_id'));body['asset_ids']=[row.id for row in assets]
    if kind in {'plan','batch_analysis'}:
        from .storyboards import output_assets
        output_assets(db, assets)
    if kind=='plan' and any(not row.kind.startswith(('video','image')) for row in assets):raise EditingError('preparation_asset_kind','规划仅支持视频和图片素材',422)
    options={'shot_analysis':True,'transcribe':False,'whisper_model':'small','timeout_seconds':30} if kind=='plan' else analysis_options(SimpleNamespace(**body))
    items=[_item(db,tenant,row,options,kind in {'plan','batch_analysis'},bool(body.get('force_new'))) for row in assets]
    fingerprint=_fingerprint(body,assets,template);snapshot={'template':template,'items':items}
    if len(json.dumps(snapshot,ensure_ascii=False).encode())>MAX_SNAPSHOT:raise EditingError('preparation_snapshot_limit','分析结果快照超过16MiB，请缩小本次素材范围',422)
    previous=db.scalar(select(models.PreparationQuote).where(models.PreparationQuote.tenant_id==tenant,models.PreparationQuote.idempotency_key==key))
    if previous:
        if previous.kind!=kind or previous.input_hash!=fingerprint or digest(previous.snapshot_json)!=digest(snapshot):raise EditingError('preparation_intent_conflict','原报价输入或分析状态已变化，请明确重新报价',409)
        return previous
    row=models.PreparationQuote(tenant_id=tenant,project_id=assets[0].project_id,kind=kind,idempotency_key=key,input_hash=fingerprint,
        request_json=body,snapshot_json=snapshot,expires_at=datetime.now(timezone.utc)+timedelta(minutes=10))
    db.add(row);db.flush();return row


def public_result(db,row):
    result=obj(row.result_json)
    return {'quote_id':row.id,'kind':row.kind,'job_id':result.get('job_id'),
        'items':[{key:item.get(key) for key in ('asset_id','job_id','disposition')} for item in result.get('items',[])]} if row.consumed_key else None


def quote_read(db,row):
    items=[];totals={};stored=obj(row.snapshot_json).get('items',[])
    for item in stored:
        problem=item.get('problem');job=_job_meta(db,item['job_id']) if item.get('job_id') else None
        if item['disposition']=='reused' and job:problem=_reuse_problem(db,job) or problem
        cap=obj(item.get('capability'));price=obj(cap.get('price'))
        amount=item.get('amount_micros') if item['disposition']=='new' else None
        if amount is not None:totals[item['currency']]=totals.get(item['currency'],0)+amount
        items.append({'asset_id':item['asset_id'],'name':item['name'],'kind':item['kind'],'disposition':item['disposition'],
            'job_id':item.get('job_id'),'job_status':job.status if job else None,'problem':problem,
            'amount_micros':amount,'currency':item.get('currency'),'price_version':price.get('version'),
            'provider':cap.get('provider'),'model_id':cap.get('model_id'),'model_version':cap.get('model_version'),
            'duration_bound_seconds':capability_reservation(cap)['duration_bound_seconds'] if cap else None})
    affordable,reasons=affordability(db,row.tenant_id,row.project_id,[{**old,'problem':new['problem']} for old,new in zip(stored,items)])
    expired=utc(row.expires_at)<=datetime.now(timezone.utc)
    return {'id':row.id,'kind':row.kind,'project_id':row.project_id,'input_hash':row.input_hash,'expires_at':utc(row.expires_at),
        'status':'consumed' if row.consumed_key else 'expired' if expired else 'ready','items':items,
        'totals':[{'currency':currency,'amount_micros':amount} for currency,amount in sorted(totals.items())],
        'can_submit':not row.consumed_key and not expired and affordable,'reasons':reasons,'result':public_result(db,row),
        'note':'仅对新增分析预占；复用不代表历史任务免费。规划本身不调用媒体服务，渲染另行报价。预占不是最终账单。'}


def consume_quote(db: Session,tenant: str,quote_id: str,key: str,*,expected_kind=None,expected_request=None):
    row=db.scalar(select(models.PreparationQuote).where(models.PreparationQuote.id==quote_id,models.PreparationQuote.tenant_id==tenant).with_for_update().execution_options(populate_existing=True))
    if row is None:raise EditingError('preparation_quote_missing','未找到当前空间的报价',404)
    if expected_kind and (row.kind!=expected_kind or normalize(row.kind,expected_request)!=row.request_json):raise EditingError('preparation_input_changed','输入设置与报价不同，请重新报价',409)
    if row.consumed_key:
        if row.consumed_key!=key:raise EditingError('preparation_quote_used','报价已用于其他提交，请核对原操作',409)
        return row
    if utc(row.expires_at)<=datetime.now(timezone.utc):raise EditingError('preparation_quote_expired','报价已过期，请重新报价',409)
    body=row.request_json;template=_template(db,tenant,body) if row.kind=='plan' else None
    assets=_assets(db,tenant,body['asset_ids'],row.project_id,lock=True);by_id={asset.id:asset for asset in assets}
    if row.kind in {'plan','batch_analysis'}:
        from .storyboards import output_assets
        output_assets(db, assets)
    if row.input_hash!=_fingerprint(body,assets,template):raise EditingError('preparation_input_changed','素材对象、模板或输入设置已变化，请重新报价',409)
    resolved=[];cache={}
    for item in row.snapshot_json['items']:
        asset=by_id[item['asset_id']];current=item
        if item['disposition']=='cached':
            if digest(obj(asset.metadata_json).get('analysis'))!=item['analysis_hash']:raise EditingError('preparation_analysis_changed','已有分析结果已变化，请重新报价',409)
        elif item['disposition']=='reused':
            job=_job_meta(db,item['job_id'])
            if not job or job.tenant_id!=tenant or not _matches(job,asset,item['options'],row.kind in {'plan','batch_analysis'}) or _reuse_problem(db,job):
                raise EditingError('preparation_original_unresolved','原分析已变化或尚需恢复，请先查询原任务',409)
        else:
            current=_item(db,tenant,asset,item['options'],row.kind in {'plan','batch_analysis'},bool(body.get('force_new')))
            if current.get('problem'):raise EditingError('preparation_original_unresolved',current['problem'],409)
            if current['disposition']=='new' and current['capability_hash']!=item['capability_hash']:raise EditingError('preparation_price_changed','服务或报价版本已变化，请重新报价',409)
        if current['disposition']=='cached':cache[asset.id]=deepcopy(current['analysis_snapshot'])
        resolved.append(deepcopy(current))
    get_budget(db,tenant,for_update=True)
    if db.scalar(select(models.PreparationQuote.id).where(models.PreparationQuote.tenant_id==tenant,models.PreparationQuote.consumed_key==key)):
        raise EditingError('preparation_key_conflict','提交标识已用于其他报价，请核对原操作',409)
    allowed,reasons=affordability(db,tenant,row.project_id,resolved)
    if not allowed:raise EditingError('preparation_budget_exceeded','当前预算不足或原分析尚待处理，未创建收费任务',409,{'reasons':reasons})
    if db.scalar(select(models.Job.id).where(models.Job.tenant_id==tenant,models.Job.idempotency_key==key)):
        raise EditingError('preparation_key_conflict','提交标识已用于其他任务，请核对原操作',409)
    for item in resolved:
        if item['disposition']!='new':continue
        asset=by_id[item['asset_id']]
        job=models.Job(tenant_id=tenant,project_id=asset.project_id,type='asset.analyze',status='queued',
            idempotency_key='preparation:'+row.id+':'+asset.id,max_attempts=body['max_attempts'],
            payload={'asset_id':asset.id,'uri':asset.uri,'options':item['options'],'capability_snapshot':item['capability'],
                'analysis_input':{'asset_id':asset.id,'asset_uri':asset.uri,'uri':asset.uri,'kind':asset.kind,'sha256':obj(obj(asset.metadata_json).get('storage')).get('sha256')},
                'preparation_quote_id':row.id,'analysis_source_hash':source_hash(asset),'analysis_options_hash':digest(item['options'])})
        db.add(job);db.flush();decision=preflight_job_budget(db,job,selected_capability=item['capability'])
        if decision is not None and not decision.allowed:raise EditingError('preparation_budget_exceeded','预算已变化，整批未提交',409)
        item['job_id']=job.id
    parent=None
    if row.kind=='plan':
        request_hash=digest({key:value for key,value in body.items() if key!='max_attempts'})
        parent=models.Job(tenant_id=tenant,project_id=row.project_id,type='auto_edit.plan',status='queued',idempotency_key=key,max_attempts=body['max_attempts'],
            payload={'project_id':row.project_id,'template_id':body['template_id'],'asset_ids':body['asset_ids'],'quality':body['quality'],
                'template_snapshot':template,'analysis_job_ids':[item['job_id'] for item in resolved if item.get('job_id')],
                'preparation_quote_id':row.id,'planning_request_hash':request_hash,
                'plan':{'template_id':template['id'],'template_version':template['version'],'selected':[],'warnings':['分析就绪后生成固定版本计划']}})
        db.add(parent);db.flush()
    row.consumed_key=key;row.result_json={'job_id':parent.id if parent else None,
        'items':[{name:item.get(name) for name in ('asset_id','job_id','disposition','source_hash','options')} for item in resolved],
        'cache_snapshots':cache};db.flush();return row


def plan_assets(db,job,assets):
    from .storyboards import output_assets
    output_assets(db, assets)
    quote_id=obj(job.payload).get('preparation_quote_id')
    if not quote_id:return assets
    quote=db.get(models.PreparationQuote,quote_id)
    batch_bound=False
    if quote and obj(job.payload).get('batch_quote_id'):
        from .batches import valid_plan_binding
        batch_bound=valid_plan_binding(db,job,quote)
    if not quote or quote.tenant_id!=job.tenant_id or not (batch_bound or obj(quote.result_json).get('job_id')==job.id):raise EditingError('preparation_snapshot_missing','原规划输入快照不可用',409)
    result=quote.result_json;rows={item['asset_id']:item for item in result['items']};out=[]
    for asset in assets:
        item=rows.get(asset.id)
        if not item or source_hash(asset)!=item['source_hash']:raise EditingError('preparation_source_changed','规划引用的素材对象已变化',409)
        if item['disposition']=='cached':analysis=result['cache_snapshots'].get(asset.id)
        else:
            dependency=db.get(models.Job,item['job_id'])
            if not dependency or dependency.status!='succeeded' or dependency.policy_blocked or not _matches(dependency,asset,item['options'],True):raise EditingError('preparation_analysis_incomplete','原分析任务未成功，不能用其他结果静默替代',409)
            analysis=obj(dependency.payload).get('analysis')
        if not analysis:raise EditingError('preparation_analysis_incomplete','所需分析结果尚未就绪',409)
        metadata={**obj(asset.metadata_json),'analysis':deepcopy(analysis)}
        out.append(SimpleNamespace(id=asset.id,tenant_id=asset.tenant_id,project_id=asset.project_id,name=asset.name,kind=asset.kind,uri=asset.uri,metadata_json=metadata))
    return out


def validate_analysis_source(job,asset):
    if obj(job.payload).get('preparation_quote_id') and (source_hash(asset)!=job.payload.get('analysis_source_hash') or digest(obj(job.payload).get('options'))!=job.payload.get('analysis_options_hash')):
        raise ProviderExecutionError('Analysis inputs changed after price confirmation',code='preparation_source_changed',capability='media_analysis',retryable=False)


def validate_analysis_price(db,job):
    if not obj(job.payload).get('preparation_quote_id'):return
    asset=db.get(models.Asset,job.payload['asset_id'],populate_existing=True)
    if not asset or asset.tenant_id!=job.tenant_id:raise ProviderExecutionError('Quoted analysis source is unavailable',code='preparation_source_changed',retryable=False)
    validate_analysis_source(job,asset);pinned=job.payload.get('capability_snapshot')
    try:current=analysis_capability(asset,job.payload['options'],pinned)
    except EditingError as exc:raise ProviderExecutionError('Analysis price is no longer available',code='preparation_price_changed',retryable=False) from exc
    if current!=pinned:raise ProviderExecutionError('Analysis price changed before submission',code='preparation_price_changed',retryable=False)
