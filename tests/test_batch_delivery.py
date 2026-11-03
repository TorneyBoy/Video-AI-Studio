"""Fixed-wave reviews and real-package coordination with no supplier calls."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
from zipfile import ZipFile
import pytest
from sqlalchemy import func,select
from app import models
from app.config import get_settings
from app.services import batch_delivery,package_worker
from app.services.completion import render_revision_binding,finish_job
from app.services.jobs import request_job_retry
from app.services.storage_attestation import sign_attestation
from app.services.storage_gateway import StorageGatewayError
from test_batch_runs import wave_case as _wave_case,prepared as _prepared,edit_case as _edit_case,priced,selections,submitted
from test_artifact_reviews import mutate,report,review as send

wave_case=_wave_case
prepared=_prepared
edit_case=_edit_case
DATA=b'Explicit fixture bytes, not a real rendered acceptance video.'*20
SHA=hashlib.sha256(DATA).hexdigest()
SECRET='package-test-attestation-secret-32-characters'


@pytest.fixture
def delivery_case(wave_case,monkeypatch):
    case=wave_case;settings=get_settings()
    for key,value in {'delivery_package_enabled':True,'storage_gateway_url':'https://fixture.invalid','storage_gateway_api_key':'invalid-package-test-only-key-32-characters','storage_attestation_secret':SECRET,'storage_public_hosts':('fixture.invalid',)}.items():monkeypatch.setattr(settings,key,value)
    price=priced(case,items=selections(case,3),quality='high').json();result=submitted(case,price['id']).json();case['run']=result['run_id'];case['run_result']=result;case['outputs']=[]
    def complete(db):
        for index,item in enumerate(result['items']):
            job=db.get(models.Job,item['job_id'])
            if index==2:job.status='failed';job.failure_code='fixture_render_failed';job.retryable=True;continue
            job.status='succeeded';manifest=job.payload['manifest'];uri=f'oss://fixture-bucket/tenants/{case["tenant"]}/renders/{job.id}/output'
            qc={'schema_version':'media-quality/1','status':'passed','object_uri':uri,'output_sha256':SHA,'size_bytes':len(DATA),
                'width':manifest['resolution']['width'],'height':manifest['resolution']['height'],'duration_seconds':manifest['duration'],'fps':manifest['fps'],
                'has_audio':False,'video_codec':'h264','container':'mp4','inspector':'metadata-fixture-not-acceptance','inspector_version':'1'}
            row=models.Artifact(tenant_id=case['tenant'],project_id=case['project'],job_id=job.id,kind='video.rendered',uri=uri,
                metadata_json={'qa_fixture':True,'quality':'high','content_hash':manifest['content_hash'],'quality_control':qc,'output_sha256':SHA,**render_revision_binding(job)})
            db.add(row);db.flush();db.add(models.ArtifactStorage(artifact_id=row.id,tenant_id=case['tenant'],object_uri=f'oss://fixture-bucket/tenants/{case["tenant"]}/artifacts/{row.id}/output',
                sha256=SHA,size_bytes=len(DATA),expires_at=datetime.now(timezone.utc)+timedelta(days=1),evidence_json={'fixture':True}))
            case['outputs'].append({'key':item['key'],'artifact_id':row.id})
    mutate(case,complete);case['base']='/api/v1/batches/'+case['batch']['id']+'/runs/'+case['run'];return case


def request(case,path,body,key=None):
    return case['client'].post(case['base']+path,headers={**case['headers'],**({'Idempotency-Key':key} if key else {})},json=body)


def prepare(case):return request(case,'/review-prepare',{'keys':[row['key'] for row in case['outputs']]}).json()


def review_body(prepared,decision='approved',note=''):
    return {'items':[{'key':row['key'],'artifact_id':row['artifact_id'],'review':{'binding_hash':row['report']['binding_hash'],
        'base_review_id':row['report']['latest']['id'] if row['report']['latest'] else None,'decision':decision,'note':note,'issues':[]}} for row in prepared['items']]}


def approve(case):
    response=request(case,'/reviews',review_body(prepare(case)),'approve-group');assert response.status_code==200,response.text;return response.json()


def preview(case,**changes):
    response=request(case,'/delivery-preview',{'keys':[row['key'] for row in case['batch']['items']],**changes});assert response.status_code==200,response.text;return response.json()


def create_package(case,value=None,key='package'):
    value=value or preview(case);return request(case,'/packages',{'keys':[row['key'] for row in case['batch']['items']],'manifest_hash':value['manifest_hash'],'include_subtitles':value['manifest']['include_subtitles']},key)


class PackageGateway:
    def __init__(self):self.builds=[];self.responses={};self.lose=False;self.hook=None;self.sign_hook=None;self.cancelled=[]
    def delivery_package(self,*,package_id,tenant_id,manifest_hash,specification=None,cancel=False):
        if cancel:
            self.cancelled.append(package_id);return {'package_id':package_id,'manifest_hash':manifest_hash,'status':'failed','error_code':'package_cancelled'}
        if specification is not None:
            self.builds.append((package_id,deepcopy(specification)));buffer=io.BytesIO()
            with ZipFile(buffer,'w') as zip:
                zip.writestr('manifest.json',specification['manifest'])
                for file in specification['files']:zip.writestr(file['filename'],DATA if file['kind']=='video' else file['content'])
            raw=buffer.getvalue();self.responses[package_id]={'package_id':package_id,'manifest_hash':manifest_hash,'status':'succeeded',
                'object_uri':f'oss://fixture-bucket/tenants/{tenant_id}/packages/{package_id}/bundle.zip','sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}
            if self.hook:self.hook()
            if self.lose:self.lose=False;raise StorageGatewayError('storage_gateway_unreachable','lost result')
        if package_id not in self.responses:raise StorageGatewayError('package_not_found','not received',status_code=404)
        result=dict(self.responses[package_id]);expiry=datetime.now(timezone.utc)+timedelta(seconds=60);nonce='package-fixture-proof-nonce'
        result['attestation']={'expires_at':expiry.isoformat(),'nonce':nonce,'signature':sign_attestation(SECRET,asset_id=package_id,object_uri=result['object_uri'],size_bytes=result['size_bytes'],sha256=result['sha256'],expires_at=expiry,nonce=nonce)}
        return result
    def artifact_access(self,**args):
        if self.sign_hook:self.sign_hook()
        return {'artifact_id':args['artifact_id'],'url':'https://fixture.invalid/bundle.zip?signature=short-lived','purpose':'download','expires_at':datetime.now(timezone.utc)+timedelta(seconds=min(60,args['ttl_seconds']))}


def process(case,gateway):
    out=[];mutate(case,lambda db:out.extend(package_worker.process_queued_package_jobs(db,gateway=gateway)));return out


def test_delivery_list_and_preview_preserve_failed_items_without_approving_them(delivery_case):
    case=delivery_case;view=case['client'].get(case['base']+'/delivery',headers=case['headers']);assert view.status_code==200,view.text
    assert view.json()['total']==3 and view.json()['counts']=={'pending_review':2,'failed':1}
    value=preview(case);assert not value['can_package'] and value['manifest']['included_count']==0 and value['manifest']['excluded_count']==3
    assert 'oss://' not in json.dumps(value) and 'file:///' not in json.dumps(value)
    assert hashlib.sha256(value['file']['content'].encode()).hexdigest()==value['manifest_hash']
    assert case['client'].get(case['base']+'/delivery?state=attention',headers=case['headers']).json()['items'][0]['status']=='failed'


def test_bulk_review_skips_one_changed_head_and_replays_fixed_receipt(delivery_case):
    case=delivery_case;body=review_body(prepare(case));first=case['outputs'][0]['artifact_id'];state=report(case,first)
    assert send(case,state,'single-competing','changes_requested',artifact=first,note='Fix fixture').status_code==201
    response=request(case,'/reviews',body,'group-original');assert response.status_code==200,response.text;result=response.json()
    assert sum(row['applied'] for row in result['items'])==1 and any(row.get('reason')=='review_revision_conflict' for row in result['items'])
    assert request(case,'/reviews',body,'group-original').json()==result
    found=case['client'].get(case['base']+'/review-requests/group-original',headers=case['headers']).json();assert found['found'] and found['result']==result
    changed=deepcopy(body);changed['items'][0]['review']['note']='Different';assert request(case,'/reviews',changed,'group-original').status_code==409


def test_wrong_output_or_tenant_is_rejected_before_any_review(delivery_case):
    case=delivery_case;body=review_body(prepare(case));body['items'][0]['artifact_id']=case['outputs'][1]['artifact_id'];body['items']=body['items'][:1]
    assert request(case,'/reviews',body,'forged').status_code==404
    owner=case['client'].post('/api/v1/tenants',json={'slug':'batch-delivery-other','name':'Other'}).json()['id']
    assert case['client'].get(case['base']+'/delivery',headers={'X-Tenant-ID':owner}).status_code==404
    counts=[];mutate(case,lambda db:counts.append(db.scalar(select(func.count()).select_from(models.ArtifactReview))));assert counts==[0]


def test_review_permission_is_not_granted_by_bulk_route(delivery_case,monkeypatch):
    case=delivery_case;body=review_body(prepare(case));monkeypatch.setattr(batch_delivery,'can_review',lambda *args:False)
    assert request(case,'/reviews',body,'no-review-role').status_code==403


def test_preview_confirms_exact_approved_subset_and_no_new_render_or_ledger(delivery_case):
    case=delivery_case;approve(case);value=preview(case,include_subtitles=True)
    assert value['can_package'] and value['manifest']['included_count']==2 and value['manifest']['excluded_count']==1
    assert value['manifest']['video_bytes']==2*len(DATA) and all('sidecars' in row for row in value['manifest']['items'] if row['included'])
    assert 'oss://' not in json.dumps(value)
    before=[];mutate(case,lambda db:before.extend([db.scalar(select(func.count()).select_from(models.Job)),db.scalar(select(func.count()).select_from(models.UsageLedger))]))
    response=create_package(case,value);assert response.status_code==200,response.text;result=response.json()
    assert create_package(case,value).json()==result
    receipt=case['client'].get(case['base']+'/package-requests/package',headers=case['headers']).json();assert receipt['found'] and receipt['result']==result
    after=[];mutate(case,lambda db:after.extend([db.scalar(select(func.count()).select_from(models.Job)),db.scalar(select(func.count()).select_from(models.UsageLedger))]))
    assert after==[before[0]+1,before[1]]


def test_same_confirmed_manifest_reuses_package_across_distinct_keys(delivery_case):
    case=delivery_case;approve(case);value=preview(case)
    first=create_package(case,value,'first-package').json()
    again=create_package(case,value,'another-device').json()
    assert first['disposition']=='new' and again['disposition']=='reused'
    assert first['package_id']==again['package_id'] and first['job_id']==again['job_id']
    assert create_package(case,value,'first-package').json()==first
    assert case['client'].get(case['base']+'/package-requests/another-device',headers=case['headers']).json()['result']==again
    rows=[];mutate(case,lambda db:rows.extend([db.scalar(select(func.count()).select_from(models.DeliveryPackage)),db.scalar(select(func.count()).select_from(models.DeliveryPackageRequest))]))
    assert rows==[1,2]


def test_expired_package_cannot_be_claimed_or_reused(delivery_case):
    case=delivery_case;approve(case);value=preview(case);first=create_package(case,value,'expired').json()
    mutate(case,lambda db:setattr(db.get(models.DeliveryPackage,first['package_id']),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    state=case['client'].get(case['base']+'/packages/'+first['package_id'],headers=case['headers']).json()
    assert not state['can_download'] and state['reason']=='package_expired'
    next=create_package(case,value,'fresh').json();assert next['package_id']!=first['package_id']


def test_unknown_original_package_is_recovered_without_fanout_after_local_failure(delivery_case):
    case=delivery_case;approve(case);value=preview(case);first=create_package(case,value,'first').json()
    def unknown(db):
        row=db.get(models.DeliveryPackage,first['package_id']);row.remote_state='unknown'
        job=db.get(models.Job,first['job_id']);job.status='failed';job.retryable=True;job.attempt_count=1;job.failure_code='package_poll_timeout'
    mutate(case,unknown);same=create_package(case,value,'other-device').json()
    assert same['package_id']==first['package_id'] and same['disposition']=='reused'
    def recover_then_cancel(db):
        job=db.get(models.Job,first['job_id']);request_job_retry(db,job,force=False);finish_job(db,job,'cancelled')
    mutate(case,recover_then_cancel)
    blocked=create_package(case,value,'after-cancel');assert blocked.status_code==409 and blocked.json()['detail']['code']=='package_original_unresolved'
    gateway=PackageGateway();mutate(case,lambda db:package_worker.reconcile_package_operations(db,gateway=gateway))
    assert gateway.cancelled==[first['package_id']]
    fresh=create_package(case,value,'after-confirmed-cancel').json();assert fresh['package_id']!=first['package_id']


def test_expired_unknown_package_reconciles_with_a_cancel_tombstone(delivery_case):
    case=delivery_case;approve(case);first=create_package(case).json()
    def expired(db):
        row=db.get(models.DeliveryPackage,first['package_id']);row.remote_state='unknown';row.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        job=db.get(models.Job,first['job_id']);job.status='failed';job.retryable=False;job.policy_blocked=True;job.failure_code='package_expired'
    mutate(case,expired);gateway=PackageGateway();mutate(case,lambda db:package_worker.reconcile_package_operations(db,gateway=gateway))
    assert gateway.cancelled==[first['package_id']]
    rows=[];mutate(case,lambda db:rows.append((db.get(models.Job,first['job_id']).status,db.get(models.DeliveryPackage,first['package_id']).remote_state)))
    assert rows==[('failed','failed')]


def test_changed_approval_rejects_confirmation_and_stops_a_queued_package(delivery_case):
    case=delivery_case;approve(case);value=preview(case);created=create_package(case,value).json();id=case['outputs'][0]['artifact_id']
    assert send(case,report(case,id),'approval-withdrawn','changes_requested',artifact=id,note='Please revise').status_code==201
    assert create_package(case,value,'different-key').status_code==409
    gateway=PackageGateway();result=process(case,gateway);assert result[0]['status']=='failed' and result[0]['failure_code']=='package_approval_changed' and not gateway.builds
    current=case['client'].get(case['base']+'/packages/'+created['package_id'],headers=case['headers']).json();assert not current['can_download']


def test_lost_build_result_queries_original_package_and_signing_rechecks_review(delivery_case):
    case=delivery_case;approve(case);created=create_package(case).json();gateway=PackageGateway();gateway.lose=True
    assert process(case,gateway)[0]['status']=='package_query_wait'
    def due(db):
        job=db.get(models.Job,created['job_id']);assert job.status=='queued' and job.attempt_count==0;job.next_retry_at=datetime.now(timezone.utc)-timedelta(seconds=1)
    mutate(case,due);assert process(case,gateway)[0]['status']=='succeeded' and len(gateway.builds)==1
    id=case['outputs'][0]['artifact_id'];gateway.sign_hook=lambda:send(case,report(case,id),'signing-race','changes_requested',artifact=id,note='Hold delivery')
    def access(db):
        row=db.get(models.DeliveryPackage,created['package_id'])
        with pytest.raises(Exception,match='变化'):batch_delivery.package_access(db,row,gateway)
    mutate(case,access)


def test_cancellation_during_build_never_publishes_and_reconciles_original_remote(delivery_case):
    case=delivery_case;approve(case);created=create_package(case).json();gateway=PackageGateway()
    def cancel():mutate(case,lambda db:finish_job(db,db.get(models.Job,created['job_id']),'cancelled'))
    gateway.hook=cancel;assert process(case,gateway)[0]['status']=='superseded'
    out=[];mutate(case,lambda db:out.extend(package_worker.reconcile_package_operations(db,gateway=gateway)));assert out[0]['remote_state']=='failed' and gateway.cancelled==[created['package_id']]
    response=case['client'].get(case['base']+'/packages/'+created['package_id'],headers=case['headers']).json();assert response['status']=='cancelled' and not response['can_download']
