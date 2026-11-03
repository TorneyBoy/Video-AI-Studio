"""Recoverable metadata-only coordinator for bounded gateway ZIP jobs."""
from datetime import datetime,timezone
import re
from sqlalchemy import select
from .. import models,schemas
from ..config import get_settings
from .batch_delivery import manifest_text,validate_package
from .completion import ExecutionSuperseded,execution_ticket,lock_execution,finish_job
from .dispatch import claims,defer_execution
from .editing_errors import EditingError
from .storage_attestation import verify_attestation,StorageAttestationError
from .storage_gateway import StorageGateway,StorageGatewayError

WAIT_CODES={'storage_gateway_unreachable','storage_gateway_request_failed','storage_gateway_response_invalid','package_gateway_output_invalid','package_gateway_identity_invalid'}


def specification(row):
    return {'manifest':manifest_text(row.manifest_json),'files':[{key:value for key,value in file.items() if key in {'kind','artifact_id','filename','object_uri','sha256','size_bytes','content'}} for file in row.files_json]}


def verify_result(row,value,settings):
    try:
        if value.get('package_id')!=row.id or value.get('manifest_hash')!=row.manifest_hash:raise ValueError('identity')
        proof=schemas.RemoteUploadAttestation.model_validate(value.get('attestation'))
        if not isinstance(value.get('size_bytes'),int) or isinstance(value['size_bytes'],bool) or not 1<=value['size_bytes']<=settings.delivery_package_max_bytes+1024*1024:raise ValueError('size')
        if not re.fullmatch(r'[0-9a-f]{64}',value.get('sha256','')):raise ValueError('hash')
        uri=value.get('object_uri','')
        from urllib.parse import urlparse
        parsed=urlparse(uri)
        if parsed.scheme!='oss' or parsed.path!=f'/tenants/{row.tenant_id}/packages/{row.id}/bundle.zip' or parsed.query or parsed.fragment:raise ValueError('namespace')
        verify_attestation(settings.storage_attestation_secret,proof.signature,asset_id=row.id,object_uri=uri,size_bytes=value['size_bytes'],
            sha256=value['sha256'],expires_at=proof.expires_at,nonce=proof.nonce)
    except (ValueError,TypeError,StorageAttestationError) as error:raise StorageGatewayError('package_gateway_output_invalid','打包结果缺少有效的文件证明') from error


def _store_result(row,value,settings):
    verify_result(row,value,settings);row.remote_state='succeeded';row.object_uri=value['object_uri'];row.sha256=value['sha256'];row.size_bytes=value['size_bytes']


def process_queued_package_jobs(db,*,limit=10,gateway=None,_claims=None):
    settings=get_settings()
    if not settings.delivery_package_enabled:return []
    from .worker import _claim_job_workflow,_start_workflow_step,_get_or_create_step
    from .jobs import transition_step
    admitted=_claims if _claims is not None else claims(db,families=('package',),limit=limit,workflow_claim=_claim_job_workflow,settings=settings)
    results=[]
    for job,run in admitted:
        ticket=execution_ticket(job,run)
        try:
            lock_execution(db,job,ticket);row=db.scalar(select(models.DeliveryPackage).where(models.DeliveryPackage.job_id==job.id).with_for_update().execution_options(populate_existing=True))
            if not row:raise EditingError('package_missing','打包记录已不可用',404)
            validate_package(db,row)
            if job.attempt_count>row.attempt_round:
                row.attempt_round=job.attempt_count;row.poll_count=0;row.remote_started_at=datetime.now(timezone.utc)
            if row.poll_count>=240 or (datetime.now(timezone.utc)-(row.remote_started_at.replace(tzinfo=timezone.utc) if row.remote_started_at.tzinfo is None else row.remote_started_at)).total_seconds()>1200:
                raise EditingError('package_poll_timeout','打包结果仍待核对，可从原任务恢复查询',409)
            _start_workflow_step(db,run);step=_get_or_create_step(db,job,name='delivery.package',sequence=0);transition_step(db,step,'running')
            service=gateway or StorageGateway(settings);id=row.id;tenant=row.tenant_id;hash=row.manifest_hash;body=specification(row)
            known=row.remote_state is not None;row.remote_state='unknown';row.poll_count+=1;db.commit()
            value=None
            if known:
                try:value=service.delivery_package(package_id=id,tenant_id=tenant,manifest_hash=hash)
                except StorageGatewayError as error:
                    if error.code!='package_not_found':raise
            if value is None or value['status']=='queued' or value['status']=='running' and value.get('resumable') or value['status']=='failed' and value.get('retryable'):
                value=service.delivery_package(package_id=id,tenant_id=tenant,manifest_hash=hash,specification=body)
            lock_execution(db,job,ticket);row=db.get(models.DeliveryPackage,id,populate_existing=True)
            if value['status'] in {'queued','running'}:
                row.remote_state=value['status'];db.commit();defer_execution(db,job,ticket,delay=5,reason='package_running');results.append({'job_id':job.id,'status':'package_running'});continue
            if value['status']=='failed':
                row.remote_state='failed';db.commit()
                code=value.get('error_code','package_failed');code=code if isinstance(code,str) and re.fullmatch(r'[a-z0-9_.-]{1,100}',code) else 'package_failed'
                raise EditingError(code,'打包未完成，已保留原文件集合和成片',409)
            _store_result(row,value,settings);validate_package(db,row)
            job.retryable=False;finish_job(db,job,'succeeded',result_json={'package_id':row.id,'manifest_hash':row.manifest_hash,'sha256':row.sha256,'size_bytes':row.size_bytes});db.commit()
            results.append({'job_id':job.id,'status':'succeeded'})
        except ExecutionSuperseded:
            db.rollback();results.append({'job_id':ticket.job_id,'status':'superseded'})
        except Exception as error:
            db.rollback()
            try:
                lock_execution(db,job,ticket);row=db.scalar(select(models.DeliveryPackage).where(models.DeliveryPackage.job_id==job.id).with_for_update().execution_options(populate_existing=True))
                code=error.code if isinstance(error,(EditingError,StorageGatewayError)) else 'package_worker_unavailable'
                if code in WAIT_CODES and row and row.poll_count<240:
                    row.remote_state='unknown';db.commit();defer_execution(db,job,ticket,delay=5,reason='package_query_wait');results.append({'job_id':job.id,'status':'package_query_wait'});continue
                if row and code in {'package_quota_exceeded','package_disabled','package_gateway_rejected','package_manifest_invalid','package_text_invalid','package_filename_duplicate','package_source_unmanaged','package_size_limit'}:row.remote_state='failed'
                job.retryable=code not in {'package_approval_changed','package_expired','package_identity_conflict','package_source_hash_mismatch','package_source_size_mismatch','package_cancelled','package_quota_exceeded','package_disabled','package_manifest_invalid','package_text_invalid','package_filename_duplicate','package_source_unmanaged','package_size_limit'}
                job.policy_blocked=code in {'package_approval_changed','package_expired','package_quota_exceeded'}
                finish_job(db,job,'failed',error='打包未完成，请核对原任务与交付清单',failure_code=code);db.commit();results.append({'job_id':job.id,'status':'failed','failure_code':code})
            except ExecutionSuperseded:db.rollback();results.append({'job_id':ticket.job_id,'status':'superseded'})
    return results


def reconcile_package_operations(db,*,limit=10,gateway=None):
    from .recovery_guard import database_active
    if not database_active(db):return []
    settings=get_settings()
    if not settings.storage_gateway_url or not settings.storage_gateway_api_key:return []
    ids=list(db.scalars(select(models.DeliveryPackage.id).join(models.Job,models.Job.id==models.DeliveryPackage.job_id).where(
        models.Job.status.in_(('failed','cancelled')),models.DeliveryPackage.remote_state.in_(('unknown','queued','running'))).order_by(models.DeliveryPackage.updated_at).limit(limit)))
    if not ids:return []
    service=gateway or StorageGateway(settings);results=[]
    for id in ids:
        row=db.get(models.DeliveryPackage,id);job=db.get(models.Job,row.job_id)
        # Irrecoverable/expired jobs cannot submit again: fence any delayed POST too.
        cancel=job.status=='cancelled' or job.policy_blocked or not job.retryable or job.attempt_count>=job.max_attempts
        tenant,hash=row.tenant_id,row.manifest_hash;db.commit()
        try:value=service.delivery_package(package_id=id,tenant_id=tenant,manifest_hash=hash,cancel=cancel)
        except StorageGatewayError:continue
        # Preserve Job's terminal state; remote evidence only resolves capacity.
        db.refresh(job,with_for_update=True);row=db.get(models.DeliveryPackage,id,populate_existing=True)
        if job.status not in {'failed','cancelled'}:db.rollback();continue
        try:
            if value['status']=='succeeded':_store_result(row,value,settings)
            else:row.remote_state=value['status']
            db.commit();results.append({'package_id':id,'remote_state':row.remote_state})
        except StorageGatewayError:db.rollback()
    return results
