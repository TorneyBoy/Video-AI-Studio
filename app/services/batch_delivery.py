"""Selected-version group reviews and immutable delivery sets; metadata only."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from fastapi.encoders import jsonable_encoder
from sqlalchemy import LargeBinary,Text,cast,func,select,and_,or_
from .. import models
from ..config import get_settings
from . import batch_runs
from .artifact_reviews import can_review,review_read,review_report,submit_review
from .delivery_packages import utc,delivery_file
from .editing import digest
from .editing_errors import EditingError
from .storage_gateway import StorageGatewayError
from .work_center import work_lookup,STATES

MAX_METADATA=16*1024*1024
MAX_PACKAGE_TEXT=512*1024
FILTERS=STATES


def configuration():
    settings=get_settings()
    return {'available':bool(settings.delivery_package_enabled and settings.storage_gateway_url and settings.storage_gateway_api_key and settings.storage_attestation_secret),
        'max_items':100,'max_video_bytes':settings.delivery_package_max_bytes,'max_text_bytes':MAX_PACKAGE_TEXT,
        'note':'打包复用已批准原成片，不重新渲染。打包/下载涉及的存储用量按已有服务计费，预览清单不搬运媒体。'}


def wave_rows(db,batch,run):
    return batch_runs.run_read(db,batch,run)['items']


def _selected(db,batch,run,keys):
    rows=wave_rows(db,batch,run);chosen=[row for row in rows if row['key'] in set(keys)]
    if len(chosen)!=len(keys):raise EditingError('batch_delivery_item_missing','所选子项不属于本生成轮次',404)
    return chosen


def _bound_metadata(db,rows):
    jobs=[row['job_id'] for row in rows if row['job_id']];revisions=[row['revision_id'] for row in rows]
    def bytes(column):return func.octet_length(cast(column,Text)) if db.get_bind().dialect.name=='postgresql' else func.length(cast(cast(column,Text),LargeBinary))
    size=db.scalar(select(func.coalesce(func.sum(bytes(models.Job.payload)),0)).where(models.Job.id.in_(jobs)))
    size+=db.scalar(select(func.coalesce(func.sum(bytes(models.PlanRevision.sources_json)+bytes(models.PlanRevision.plan_json)),0)).where(models.PlanRevision.id.in_(revisions)))
    size+=db.scalar(select(func.coalesce(func.sum(bytes(models.TimelineRevision.timeline_json)),0)).where(models.TimelineRevision.id.in_(revisions)))
    if size>MAX_METADATA:raise EditingError('batch_delivery_metadata_limit','本次所选版本信息过多，请缩小选择后处理',422)


def delivery_view(db,batch,run,state='all'):
    if state not in FILTERS:raise EditingError('batch_delivery_filter_invalid','成片筛选无效',422)
    rows=wave_rows(db,batch,run);lookup=work_lookup(db,tenant_id=batch.tenant_id,project_id=batch.project_id,job_ids=[row['job_id'] for row in rows if row['job_id']]);items=[];counts={}
    for row in rows:
        work=lookup.get(row['job_id'])
        if work and work['artifact_id']!=row['artifact_id']:
            work={**work,'state':'unavailable','can_open':False,'reason':'输出与本轮固定版本不符'}
        work=work or {'state':'failed','reason':'原任务或输出已不可用','can_open':False}
        current={**row,'work':work};counts[work['state']]=counts.get(work['state'],0)+1
        if (state=='all' or work['state']==state or state=='completed' and work.get('job_status')=='succeeded'
                or state=='attention' and work['state'] in {'failed','returned','stale','unavailable'}):items.append(current)
    return {'batch_id':batch.id,'batch_name':batch.name,'project_id':batch.project_id,'run_id':run.id,'created_at':utc(run.created_at),'quality':run.quality,'items':items,'counts':counts,'total':len(rows),'configuration':configuration(),
        'note':'仅显示本轮固定版本；预览与正式领取仍会重新核对当前归档、来源和审核。'}


def prepare_reviews(db,batch,run,keys,actor):
    rows=_selected(db,batch,run,keys);_bound_metadata(db,rows);items=[]
    for row in rows:
        report=review_report(db,tenant_id=batch.tenant_id,artifact_id=row['artifact_id'],actor_id=actor) if row['artifact_id'] else None
        items.append({'key':row['key'],'name':row['name'],'revision_number':row['revision_number'],'artifact_id':row['artifact_id'],'report':report})
    return {'batch_id':batch.id,'run_id':run.id,'items':items,'can_review':can_review(db,batch.tenant_id,actor)}


def apply_reviews(db,batch,run,actor,key,request):
    body=request.model_dump(mode='json');body['items']=sorted(body['items'],key=lambda row:row['key'])
    fingerprint=digest({'batch_id':batch.id,'run_id':run.id,'actor':actor,'body':body})
    previous=db.scalar(select(models.BatchReviewAction).where(models.BatchReviewAction.tenant_id==batch.tenant_id,models.BatchReviewAction.idempotency_key==key))
    if previous:
        if previous.request_hash!=fingerprint:raise EditingError('batch_review_request_conflict','原审核标识已用于不同选择或意见',409)
        return previous.result_json
    if not can_review(db,batch.tenant_id,actor):raise EditingError('review_permission_denied','当前账号不可批准或退回成片',403)
    rows={row['key']:row for row in _selected(db,batch,run,[row['key'] for row in body['items']])};_bound_metadata(db,list(rows.values()))
    if any(rows[item['key']]['artifact_id']!=item['artifact_id'] for item in body['items']):raise EditingError('batch_review_output_mismatch','成片不属于所选固定版本',404)
    ids=sorted(item['artifact_id'] for item in body['items'])
    list(db.scalars(select(models.Artifact).where(models.Artifact.tenant_id==batch.tenant_id,models.Artifact.id.in_(ids)).order_by(models.Artifact.id).with_for_update().execution_options(populate_existing=True)))
    db.expire_all();results=[]
    receipt=models.BatchReviewAction(tenant_id=batch.tenant_id,batch_id=batch.id,run_id=run.id,actor_id=actor or 'development_user',idempotency_key=key,request_hash=fingerprint,result_json={});db.add(receipt);db.flush()
    for item in request.items:
        try:
            with db.begin_nested():
                review=submit_review(db,tenant_id=batch.tenant_id,artifact_id=item.artifact_id,actor_id=actor,key='batch-review:'+receipt.id+':'+item.artifact_id,request=item.review)
                result=jsonable_encoder(review_read(review))
            results.append({'key':item.key,'artifact_id':item.artifact_id,'applied':True,'review':result})
        except (EditingError,StorageGatewayError) as error:results.append({'key':item.key,'artifact_id':item.artifact_id,'applied':False,'reason':error.code})
    receipt.result_json={'id':receipt.id,'batch_id':batch.id,'run_id':run.id,'items':results,'note':'逐项审核固定输出；状态变化项已跳过，其他结果保留。批准不自动下载或重新生成。'};db.flush();return receipt.result_json


def manifest_text(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n'


def delivery_preview(db,batch,run,selection):
    rows=_selected(db,batch,run,selection.keys);_bound_metadata(db,rows)
    positions={row.client_key:row.position for row in db.execute(select(models.BatchItem.client_key,models.BatchItem.position).where(models.BatchItem.batch_id==batch.id))}
    items=[];files=[];total=0;text_size=0;expiry=datetime.now(timezone.utc)+timedelta(days=1)
    for row in rows:
        line={key:row[key] for key in ('key','name','job_id','revision_id','revision_number','artifact_id')};line.update(included=False,reason='本项尚无可交付成片')
        if row['artifact_id']:
            report=review_report(db,tenant_id=batch.tenant_id,artifact_id=row['artifact_id'])
            if report['can_download']:
                archive=db.get(models.ArtifactStorage,row['artifact_id']);position=positions[row['key']]
                filename=f'{position+1:03d}-{row["artifact_id"]}.mp4';line.update(included=True,reason=None,binding_hash=report['binding_hash'],review_id=report['latest']['id'],
                    output_sha256=archive.sha256,size_bytes=archive.size_bytes,filename=filename)
                files.append({'kind':'video','key':row['key'],'artifact_id':row['artifact_id'],'binding_hash':report['binding_hash'],'review_id':report['latest']['id'],
                    'filename':filename,'object_uri':archive.object_uri,'sha256':archive.sha256,'size_bytes':archive.size_bytes})
                total+=archive.size_bytes;expiry=min(expiry,utc(archive.expires_at));sidecars=[]
                if selection.include_subtitles:
                    for kind in ('srt','vtt'):
                        try:exported=delivery_file(db,tenant_id=batch.tenant_id,artifact_id=row['artifact_id'],kind=kind,visibility='public',expected_binding=report['binding_hash'])
                        except EditingError as error:
                            if error.code!='subtitle_export_empty':raise
                            line['subtitle_note']='当前版本没有可导出的非静音字幕';break
                        name=filename[:-4]+'.'+kind;raw=exported['content'].encode('utf-8');text_size+=len(raw)
                        files.append({'kind':'text','filename':name,'content':exported['content'],'sha256':exported['sha256'],'size_bytes':len(raw)})
                        sidecars.append({'filename':name,'sha256':exported['sha256'],'size_bytes':len(raw)})
                line['sidecars']=sidecars
            else:line['reason']=report['reason'] if not report['quality_ready'] else '当前输出尚未批准，请先审核'
        items.append(line)
    manifest={'schema_version':'batch-delivery/1','batch_id':batch.id,'run_id':run.id,'quality':run.quality,'selected_count':len(items),
        'included_count':sum(row['included'] for row in items),'excluded_count':sum(not row['included'] for row in items),
        'video_bytes':total,'include_subtitles':selection.include_subtitles,'items':items}
    content=manifest_text(manifest);raw=content.encode('utf-8');text_size+=len(raw);hash=hashlib.sha256(raw).hexdigest();config=configuration();reasons=[]
    if not config['available']:reasons.append('package_service_unavailable')
    if not manifest['included_count']:reasons.append('package_no_approved_outputs')
    if total>config['max_video_bytes']:reasons.append('package_video_limit')
    if text_size>MAX_PACKAGE_TEXT:reasons.append('package_text_limit')
    transport={'manifest':content,'files':[{key:value for key,value in file.items() if key in {'kind','artifact_id','filename','object_uri','sha256','size_bytes','content'}} for file in files]}
    if len(json.dumps(transport,ensure_ascii=False).encode('utf-8'))>2*1024*1024-1024:reasons.append('package_text_limit')
    if len(raw)>MAX_METADATA:raise EditingError('batch_manifest_limit','所选清单过大，请缩小选择',422)
    return {'batch_id':batch.id,'run_id':run.id,'manifest_hash':hash,'manifest':manifest,
        'file':{'filename':f'batch-delivery-{run.id}.json','content':content,'sha256':hash,'encoding':'UTF-8'},'can_package':not reasons,'reasons':reasons,'configuration':config},files,expiry


def create_package(db,batch,run,actor,key,request):
    body=request.model_dump(mode='json');body['keys']=sorted(body['keys']);fingerprint=digest({'batch_id':batch.id,'run_id':run.id,'actor':actor,'body':body})
    previous=db.scalar(select(models.DeliveryPackageRequest).where(models.DeliveryPackageRequest.tenant_id==batch.tenant_id,models.DeliveryPackageRequest.idempotency_key==key))
    if previous:
        if previous.request_hash!=fingerprint:raise EditingError('package_request_conflict','原打包标识已用于不同文件选择',409)
        return previous.result_json
    rows=_selected(db,batch,run,request.keys);ids=sorted({row['artifact_id'] for row in rows if row['artifact_id']})
    list(db.scalars(select(models.Artifact).where(models.Artifact.tenant_id==batch.tenant_id,models.Artifact.id.in_(ids)).order_by(models.Artifact.id).with_for_update().execution_options(populate_existing=True)))
    db.expire_all();preview,files,expiry=delivery_preview(db,batch,run,request)
    if preview['manifest_hash']!=request.manifest_hash:raise EditingError('package_selection_changed','文件或审核状态已变化，请重新核对交付清单',409)
    if not preview['can_package']:raise EditingError('package_not_ready','当前不能打包，请核对交付清单和服务限制',409,{'reasons':preview['reasons']})
    existing=list(db.scalars(select(models.DeliveryPackage).join(models.Job,models.Job.id==models.DeliveryPackage.job_id).where(
        models.DeliveryPackage.tenant_id==batch.tenant_id,models.DeliveryPackage.batch_id==batch.id,models.DeliveryPackage.run_id==run.id,
        models.DeliveryPackage.manifest_hash==request.manifest_hash,models.DeliveryPackage.expires_at>datetime.now(timezone.utc),
        or_(models.Job.status.in_(('queued','running','succeeded')),and_(models.Job.status=='failed',models.Job.retryable.is_(True),models.Job.attempt_count<models.Job.max_attempts)),
        models.Job.policy_blocked.is_(False)).order_by(models.DeliveryPackage.created_at.desc()).limit(20)))
    for original in existing:
        try:validate_package(db,original)
        except (EditingError,StorageGatewayError):continue
        result={'package_id':original.id,'job_id':original.job_id,'manifest_hash':original.manifest_hash,'disposition':'reused'}
        db.add(models.DeliveryPackageRequest(tenant_id=batch.tenant_id,batch_id=batch.id,run_id=run.id,package_id=original.id,idempotency_key=key,request_hash=fingerprint,result_json=result));db.flush();return result
    unresolved=db.scalar(select(models.DeliveryPackage).where(models.DeliveryPackage.tenant_id==batch.tenant_id,models.DeliveryPackage.batch_id==batch.id,
        models.DeliveryPackage.run_id==run.id,models.DeliveryPackage.manifest_hash==request.manifest_hash,models.DeliveryPackage.remote_state.in_(('unknown','queued','running'))).limit(1))
    if unresolved:raise EditingError('package_original_unresolved','同一清单的原打包远端结果尚未结束，请先核对原任务',409,{'package_id':unresolved.id,'job_id':unresolved.job_id})
    id=models.new_id();job=models.Job(tenant_id=batch.tenant_id,project_id=batch.project_id,type='delivery.package',status='queued',max_attempts=3,
        idempotency_key='delivery-package:'+id,payload={'package_id':id,'batch_id':batch.id,'batch_run_id':run.id})
    db.add(job);db.flush();row=models.DeliveryPackage(id=id,tenant_id=batch.tenant_id,project_id=batch.project_id,batch_id=batch.id,run_id=run.id,job_id=job.id,
        actor_id=actor or 'development_user',idempotency_key=key,request_hash=fingerprint,request_json=body,manifest_json=preview['manifest'],manifest_hash=request.manifest_hash,files_json=files,expires_at=expiry)
    db.add(row);db.flush();result={'package_id':row.id,'job_id':job.id,'manifest_hash':row.manifest_hash,'disposition':'new'}
    db.add(models.DeliveryPackageRequest(tenant_id=batch.tenant_id,batch_id=batch.id,run_id=run.id,package_id=row.id,idempotency_key=key,request_hash=fingerprint,result_json=result));db.flush();return result


def package_for(db,batch,run,id,lock=False):
    stmt=select(models.DeliveryPackage).where(models.DeliveryPackage.id==id,models.DeliveryPackage.tenant_id==batch.tenant_id,models.DeliveryPackage.batch_id==batch.id,models.DeliveryPackage.run_id==run.id)
    if lock:stmt=stmt.with_for_update().execution_options(populate_existing=True)
    row=db.scalar(stmt)
    if not row:raise EditingError('package_missing','未找到本轮打包任务',404)
    return row


def validate_package(db,row):
    if utc(row.expires_at)<=datetime.now(timezone.utc):raise EditingError('package_expired','交付包或其成片已超过保留期限',410)
    for file in row.files_json:
        if file['kind']!='video':continue
        report=review_report(db,tenant_id=row.tenant_id,artifact_id=file['artifact_id'])
        archive=db.get(models.ArtifactStorage,file['artifact_id'])
        if (not report['can_download'] or report['binding_hash']!=file['binding_hash'] or report['latest']['id']!=file['review_id'] or not archive
                or (archive.object_uri,archive.sha256,archive.size_bytes)!=(file['object_uri'],file['sha256'],file['size_bytes'])):
            raise EditingError('package_approval_changed','包内成片的批准、来源或归档已变化，请重新核对后打包',409)


def package_read(db,row,*,validate=True):
    if row is None:raise EditingError('package_missing','交付包已不可用',404)
    job=db.get(models.Job,row.job_id);valid=True;reason=None
    try:
        if validate:validate_package(db,row)
        elif utc(row.expires_at)<=datetime.now(timezone.utc):raise EditingError('package_expired','交付包已过期',410)
    except (EditingError,StorageGatewayError) as error:valid=False;reason=error.code
    return {'id':row.id,'batch_id':row.batch_id,'run_id':row.run_id,'job_id':row.job_id,'status':job.status if job else 'missing',
        'included_count':row.manifest_json['included_count'],'excluded_count':row.manifest_json['excluded_count'],'video_bytes':row.manifest_json['video_bytes'],
        'manifest_hash':row.manifest_hash,'size_bytes':row.size_bytes,'sha256':row.sha256,'created_at':utc(row.created_at),'expires_at':utc(row.expires_at),
        'can_download':bool(valid and job and job.status=='succeeded' and row.remote_state=='succeeded' and row.object_uri and row.sha256),'reason':reason,
        'requires_validation':not validate,
        'note':'包固定包含创建时明确列出的已批准成片；领取前再次核对。取消只停止本地处理，网关文件操作可能仍需结束。'}


def package_access(db,row,gateway):
    current=package_read(db,row)
    if not current['can_download']:raise EditingError(current['reason'] or 'package_not_ready','当前交付包尚不可领取，请核对任务和原成片批准',409)
    id,uri,hash,size,manifest=row.id,row.object_uri,row.sha256,row.size_bytes,row.manifest_hash
    ttl=min(300,int((utc(row.expires_at)-datetime.now(timezone.utc)).total_seconds()))
    if ttl<1:raise EditingError('package_expired','交付包已过期',410)
    db.commit();grant=gateway.artifact_access(artifact_id=id,tenant_id=row.tenant_id,object_uri=uri,purpose='download',ttl_seconds=ttl)
    db.expire_all();row=db.get(models.DeliveryPackage,id);current=package_read(db,row)
    if (not current['can_download'] or (row.object_uri,row.sha256,row.size_bytes,row.manifest_hash)!=(uri,hash,size,manifest)
            or grant['expires_at']>utc(row.expires_at)):
        raise EditingError('package_access_changed','签名期间交付包或审核状态已变化，请重新核对',409)
    return {'package_id':id,'url':grant['url'],'expires_at':grant['expires_at'],'filename':f'batch-{id}.zip','sha256':hash,'size_bytes':size,'manifest_hash':manifest}
