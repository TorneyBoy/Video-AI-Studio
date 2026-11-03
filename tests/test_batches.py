"""Batch plans preserve independent outputs and atomically share analysis."""
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import func,select
from app import models
from app.services import batches
from app.services.worker import process_queued_auto_edit_jobs
from test_preparation_quotes import prepared as _prepared,edit_case as _edit_case
from test_artifact_reviews import mutate

edit_case=_edit_case
prepared=_prepared


def draft(case,count=10,*,fresh=False):
    return {'project_id':case['project'],'name':'十项商品批次','shared':{'template_id':case['template'],'asset_ids':case['fresh'][:2] if fresh else [row['id'] for row in case['assets']]},
        'items':[{'key':f'item_{i}','name':f'商品{i+1}','overrides':{}} for i in range(count)]}


def create(case,key='create-batch',**options):
    response=case['client'].post('/api/v1/batches',headers={**case['headers'],'Idempotency-Key':key},json=draft(case,**options));assert response.status_code==200,response.text;return response.json()


def quote(case,batch,key='batch-quote'):
    response=case['client'].post('/api/v1/batches/'+batch['id']+'/quotes',headers={**case['headers'],'Idempotency-Key':key},json={'base_revision':batch['revision']});assert response.status_code==200,response.text;return response.json()


def confirm(case,batch,price,key='batch-confirm'):
    return case['client'].post('/api/v1/batches/'+batch['id']+'/confirm',headers={**case['headers'],'Idempotency-Key':key},json={'base_revision':price['revision'],'quote_id':price['id']})


def counts(case):
    result={}
    def read(db):
        for table in (models.Job,models.UsageLedger,models.ProductionBatch,models.BatchItem):result[table.__name__]=db.scalar(select(func.count()).select_from(table))
    mutate(case,read);return result


def test_create_and_versioned_save_have_durable_distinct_receipts(prepared):
    case=prepared;row=create(case);assert len(row['items'])==10 and row['aggregate']['status']=='draft'
    assert create(case)['id']==row['id'] and counts(case)['BatchItem']==10
    changed=draft(case);changed.pop('project_id');changed['name']='第二版';changed['base_revision']=1
    path='/api/v1/batches/'+row['id']+'/configuration';headers={**case['headers'],'Idempotency-Key':'save-v2'}
    second=case['client'].post(path,headers=headers,json=changed);assert second.status_code==200,second.text
    assert second.json()['revision']==2
    assert case['client'].post(path,headers={**headers,'Idempotency-Key':'stale'},json=changed).status_code==409
    third=case['client'].post(path,headers={**headers,'Idempotency-Key':'third'},json={**changed,'base_revision':2,'name':'第三版'});assert third.json()['revision']==3
    replay=case['client'].post(path,headers=headers,json=changed).json();assert replay['revision']==2 and replay['head_revision']==3 and replay['snapshot']['name']=='第二版'
    receipt=case['client'].get(path+'/requests/save-v2',headers=case['headers']).json();assert receipt['found'] and receipt['revision']==2
    missing=case['client'].get(path+'/requests/not-written?base_revision=1',headers=case['headers']).json();assert not missing['found'] and missing['can_abandon']
    current=case['client'].get('/api/v1/batches/'+row['id'],headers=case['headers']).json();assert current['name']=='第三版' and current['items'][0]['id']==row['items'][0]['id']


def test_incomplete_draft_validates_exact_items_without_creating_jobs(prepared):
    case=prepared;body=draft(case,2);body['shared']={};body['items'][1]['overrides']={'template_id':case['template'],'asset_ids':[case['assets'][0]['id']]}
    row=case['client'].post('/api/v1/batches',headers={**case['headers'],'Idempotency-Key':'incomplete'},json=body).json();before=counts(case)
    checked=case['client'].post('/api/v1/batches/'+row['id']+'/validate',headers=case['headers'],json={'base_revision':1}).json()
    assert not checked['valid'] and {issue['key'] for issue in checked['issues']}=={'item_0'}
    response=case['client'].post('/api/v1/batches/'+row['id']+'/quotes',headers={**case['headers'],'Idempotency-Key':'invalid-quote'},json={'base_revision':1})
    assert response.status_code==422 and response.json()['detail']['details']['issues']
    assert counts(case)==before


def test_shared_analysis_quote_is_metadata_only_and_confirm_creates_each_plan_once(prepared):
    case=prepared;row=create(case,fresh=True);before=counts(case);price=quote(case,row)
    assert price['item_count']==10 and price['unique_asset_count']==2 and price['totals']==[{'currency':'CNY','amount_micros':'1000000'}]
    assert price['items'][0]['allocated_totals']==price['totals'] and not price['items'][1]['allocated_totals']
    assert counts(case)==before and quote(case,row)['id']==price['id']
    response=confirm(case,row,price);assert response.status_code==200,response.text;result=response.json()
    assert len({item['job_id'] for item in result['items']})==10 and counts(case)['Job']==before['Job']+13 and counts(case)['UsageLedger']==2
    assert confirm(case,row,price).json()==result
    assert confirm(case,row,price,'different-key').status_code==409
    mutate(case,lambda db:setattr(db.get(models.BatchQuote,price['id']),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    assert confirm(case,row,price).json()==result
    receipt=case['client'].get('/api/v1/batches/'+row['id']+'/requests/batch-confirm',headers=case['headers']).json();assert receipt['found'] and receipt['result']==result
    def graph(db):
        for item in result['items']:
            job=db.get(models.Job,item['job_id']);assert job.payload['parent_job_id']==result['parent_job_id'] and job.payload['batch_item_id']==item['item_id']
        internal=db.get(models.BatchQuote,price['id']).analysis_quote_id;case['internal']=internal
    mutate(case,graph)
    assert case['client'].get('/api/v1/preparation/quotes/'+case['internal'],headers=case['headers']).status_code==404
    assert case['client'].post('/api/v1/preparation/quotes/'+case['internal']+'/confirm',headers={**case['headers'],'Idempotency-Key':'bypass'},json={}).status_code==404


@pytest.mark.parametrize('change',['budget','source','template','expiry','configuration'])
def test_batch_revalidates_all_inputs_without_partial_submission(prepared,change):
    case=prepared;row=create(case,fresh=True);price=quote(case,row);before=counts(case)
    def update(db):
        if change=='budget':db.scalar(select(models.TenantBudget)).monthly_limit_micros=999_999
        elif change=='source':db.get(models.Asset,case['fresh'][0]).uri='https://fixture.invalid/replaced'
        elif change=='template':
            template=db.get(models.Template,case['template']);template.content={**template.content,'duration':12}
        elif change=='expiry':db.get(models.BatchQuote,price['id']).expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        else:db.get(models.ProductionBatch,row['id']).revision+=1
    mutate(case,update);result=confirm(case,row,price);assert result.status_code==409,result.text;assert counts(case)==before


def test_cached_batch_builds_ten_editable_independent_versions_and_keeps_partial_success(prepared):
    case=prepared;row=create(case);price=quote(case,row);assert not price['totals']
    result=confirm(case,row,price).json();before=counts(case)
    def execute(db):
        failed=db.get(models.Job,result['items'][-1]['job_id']);failed.status='failed';failed.retryable=True;failed.failure_code='fixture_transient'
        db.commit();assert len(process_queued_auto_edit_jobs(db,limit=20))==9
        batches.reconcile_batches(db);parent=db.get(models.Job,result['parent_job_id']);assert parent.status=='failed'
        summaries=batches.batch_read(db,db.get(models.ProductionBatch,row['id']));assert summaries['aggregate']['status']=='partial_success' and summaries['aggregate']['succeeded']==9
        assert len({item['document_id'] for item in summaries['items'] if item['document_id']})==9
        from app.services.jobs import request_job_retry
        request_job_retry(db,failed);db.commit();assert process_queued_auto_edit_jobs(db)[0]['status']=='succeeded';batches.reconcile_batches(db)
        assert db.get(models.Job,result['parent_job_id']).status=='succeeded'
        assert len(set(db.scalars(select(models.EditDocument.id).where(models.EditDocument.origin_job_id.in_([item['job_id'] for item in result['items']])))))==10
    mutate(case,execute);assert counts(case)==before
    root=result['parent_job_id'];assert case['client'].post('/api/v1/jobs/'+root+'/cancel',headers=case['headers']).status_code==409
    assert case['client'].post('/api/v1/jobs/'+root+'/retry',headers=case['headers'],json={'force':True}).status_code==409


def test_unconsumed_expired_confirmation_can_be_safely_abandoned(prepared):
    case=prepared;row=create(case);price=quote(case,row);path='/api/v1/batches/'+row['id']+'/requests/not-confirmed?quote_id='+price['id']
    assert not case['client'].get(path,headers=case['headers']).json()['can_abandon']
    mutate(case,lambda db:setattr(db.get(models.BatchQuote,price['id']),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    receipt=case['client'].get(path,headers=case['headers']).json();assert not receipt['found'] and receipt['can_abandon']
    assert confirm(case,row,price,key='not-confirmed').status_code==409


def test_other_tenant_cannot_see_batch_configuration_or_quotes(prepared):
    case=prepared;row=create(case);price=quote(case,row);tenant=case['client'].post('/api/v1/tenants',json={'slug':'foreign-batches','name':'Other'}).json()['id'];headers={'X-Tenant-ID':tenant}
    for suffix in ('','/quotes/'+price['id'],'/requests/batch-confirm','/configuration/requests/save'):
        assert case['client'].get('/api/v1/batches/'+row['id']+suffix,headers=headers).status_code==404
    assert case['client'].get('/api/v1/batches/requests/create-batch',headers=headers).json()['found'] is False


def test_submitted_batch_inputs_and_item_job_identity_are_immutable(prepared):
    case=prepared;row=create(case);price=quote(case,row);result=confirm(case,row,price).json();body=draft(case);body.pop('project_id');body['base_revision']=1
    response=case['client'].post('/api/v1/batches/'+row['id']+'/configuration',headers={**case['headers'],'Idempotency-Key':'post-confirm-change'},json=body)
    assert response.status_code==409
    current=case['client'].get('/api/v1/batches/'+row['id'],headers=case['headers']).json();assert [item['job_id'] for item in current['items']]==[item['job_id'] for item in result['items']]


def test_each_item_uses_its_own_template_sources_and_output_settings(prepared):
    case=prepared;other=case['client'].post('/api/v1/templates',headers=case['headers'],json={'name':'Alternate','project_id':case['project'],'version':'1','content':{'schema_version':'1.0','id':'alternate','version':'1','duration':6,'slots':[{'name':'hook','type':'video','required':True}]}}).json()['id']
    body=draft(case,2);body['shared']['target_duration']=10
    body['items'][0]['overrides']={'template_id':other,'asset_ids':[case['assets'][0]['id']],'target_duration':None,'aspect_ratio':'1:1'}
    body['items'][1]['overrides']={'asset_ids':[case['assets'][1]['id'],case['assets'][2]['id']],'aspect_ratio':'16:9'}
    response=case['client'].post('/api/v1/batches',headers={**case['headers'],'Idempotency-Key':'overrides'},json=body);assert response.status_code==200,response.text
    batch=response.json();result=confirm(case,batch,quote(case,batch)).json()
    def execute(db):
        assert all(row['status']=='succeeded' for row in process_queued_auto_edit_jobs(db,limit=10))
        for index,item in enumerate(result['items']):
            job=db.get(models.Job,item['job_id']);assert job.payload['template_snapshot']['duration']==(6 if index==0 else 10)
            assert job.payload['template_snapshot']['id']==('alternate' if index==0 else 'pair')
            timeline=db.get(models.Timeline,job.payload['timeline_id']).timeline_json
            assert timeline['resolution']=={'width':1080 if index==0 else 1920,'height':1080}
            assigned=set(body['items'][index]['overrides']['asset_ids'])
            assert set(job.payload['asset_ids'])==assigned and all(row['asset_id'].split('#shot-',1)[0] in assigned for row in job.payload['plan']['selected'])
    mutate(case,execute)
