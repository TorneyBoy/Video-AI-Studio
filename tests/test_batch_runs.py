"""Selected fixed-version render waves share the existing pricing/Job contract."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from sqlalchemy import func,select
import pytest
from app import models
from app.api import routers
from app.services import render_quotes,batch_runs,completion
from app.services.worker import process_queued_auto_edit_jobs
from test_batches import prepared as _prepared,edit_case as _edit_case,create,quote,confirm
from test_artifact_reviews import mutate
from test_catalog_reservations import snapshot

edit_case=_edit_case
prepared=_prepared


@pytest.fixture
def wave_case(prepared,monkeypatch):
    case=prepared;selected=snapshot();selected.update(id='render:video_editing:1',capability='video_editing');case['render_price']=selected
    monkeypatch.setattr(render_quotes,'require_render_support',lambda *args,**kw:deepcopy(case['render_price']))
    monkeypatch.setattr(routers,'require_render_support',lambda *args,**kw:deepcopy(case['render_price']))
    batch=create(case,count=3);confirm(case,batch,quote(case,batch))
    mutate(case,lambda db:process_queued_auto_edit_jobs(db,limit=10))
    case['batch']=case['client'].get('/api/v1/batches/'+batch['id'],headers=case['headers']).json()
    assert all(item['revision_id'] for item in case['batch']['items'])
    return case


def selections(case,count=2):return [{'key':item['key'],'revision_id':item['revision_id']} for item in case['batch']['items'][:count]]


def priced(case,key='render-quote',**changes):
    return case['client'].post('/api/v1/batches/'+case['batch']['id']+'/render-quotes',headers={**case['headers'],'Idempotency-Key':key},json={'items':selections(case),'quality':'high',**changes})


def submitted(case,id,key='render-confirm'):
    return case['client'].post('/api/v1/batches/'+case['batch']['id']+'/render-confirm',headers={**case['headers'],'Idempotency-Key':key},json={'quote_id':id})


def totals(case):
    out={}
    def read(db):
        for table in (models.Job,models.UsageLedger,models.BatchRun,models.BatchRunItem):out[table.__name__]=db.scalar(select(func.count()).select_from(table))
    mutate(case,read);return out


def run(case,id):
    result=case['client'].get('/api/v1/batches/'+case['batch']['id']+'/runs/'+id,headers=case['headers']);assert result.status_code==200,result.text;return result.json()


def action_body(view,action='cancel'):
    return {'action':action,'items':[{'key':item['key'],'job_id':item['job_id'],'expected_status':item['task']['status'],
        'expected_attempt':item['task']['attempt_count'],'expected_generation':item['task']['execution_generation']} for item in view['items']]}


def act(case,phase,body,key='actions'):
    return case['client'].post('/api/v1/batches/'+case['batch']['id']+'/phases/'+phase+'/actions',headers={**case['headers'],'Idempotency-Key':key},json=body)


def test_quote_creates_no_jobs_and_one_confirm_keeps_exact_item_versions(wave_case):
    case=wave_case;before=totals(case);response=priced(case);assert response.status_code==200,response.text
    price=response.json();assert price['item_count']==2 and price['totals']==[{'currency':'CNY','amount_micros':'1000000'}] and price['can_submit']
    assert totals(case)==before and 'file:///' not in response.text and 'render_quote_id' not in response.text
    assert priced(case).json()['id']==price['id']
    response=submitted(case,price['id']);assert response.status_code==200,response.text;result=response.json()
    assert totals(case)=={'Job':before['Job']+3,'UsageLedger':before['UsageLedger']+2,'BatchRun':1,'BatchRunItem':2}
    assert submitted(case,price['id']).json()==result
    assert submitted(case,price['id'],'another-key').status_code==409
    view=run(case,result['run_id']);assert view['aggregate']['counts']['queued']==2 and all(item['task']['can_cancel'] for item in view['items'])
    assert {item['revision_id'] for item in view['items']}=={item['revision_id'] for item in selections(case)}
    mutate(case,lambda db:setattr(db.get(models.BatchRenderQuote,price['id']),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    case['render_price']['price']['version']='new-price'
    assert submitted(case,price['id']).json()==result
    receipt=case['client'].get('/api/v1/batches/'+case['batch']['id']+'/render-requests/render-confirm',headers=case['headers']).json();assert receipt['found'] and receipt['result']==result
    def binding(db):
        job=db.get(models.Job,result['items'][0]['job_id']);fixed=completion.render_revision_binding(job)
        assert fixed['batch_id']==case['batch']['id'] and fixed['batch_run_id']==result['run_id'] and fixed['timeline_revision_id']==result['items'][0]['revision_id']
    mutate(case,binding)


@pytest.mark.parametrize('change',['price','monthly','project','source','expiry','head'])
def test_changed_inputs_or_limits_reject_entire_render_wave(wave_case,change):
    case=wave_case;price=priced(case).json();before=totals(case)
    def update(db):
        if change=='monthly':db.scalar(select(models.TenantBudget)).monthly_limit_micros=999_999
        elif change=='project':db.add(models.ProjectBudget(tenant_id=case['tenant'],project_id=case['project'],currency='CNY',limit_micros=999_999,actor_id='fixture'))
        elif change=='source':db.get(models.Asset,case['assets'][0]['id']).uri='https://fixture.invalid/replaced'
        elif change=='expiry':db.get(models.BatchRenderQuote,price['id']).expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
    if change=='price':case['render_price']['price']['version']='changed'
    elif change=='head':
        item=case['batch']['items'][0];base=case['client'].get('/api/v1/editing/'+item['document_id'],headers=case['headers']).json()
        changed=case['client'].post('/api/v1/editing/'+item['document_id']+'/revisions',headers={**case['headers'],'Idempotency-Key':'edit-after-quote'},json={'base_revision_id':item['revision_id'],'operations':[{'op':'set_lock','selection_id':base['plan']['selected'][0]['selection_id'],'locked':True}]});assert changed.status_code==201,changed.text
    else:mutate(case,update)
    result=submitted(case,price['id']);assert result.status_code==409,result.text;assert totals(case)==before


def test_two_quotes_downgrade_to_the_original_jobs_and_do_not_reserve_twice(wave_case):
    case=wave_case;one=priced(case).json();two=priced(case,key='second').json();first=submitted(case,one['id']).json();before=totals(case)
    second=submitted(case,two['id'],key='second-submit');assert second.status_code==200,second.text
    assert [item['job_id'] for item in second.json()['items']]==[item['job_id'] for item in first['items']]
    assert all(item['disposition']=='reused' for item in second.json()['items'])
    assert totals(case)['UsageLedger']==before['UsageLedger'] and totals(case)['Job']==before['Job']+1
    explicit=priced(case,key='new-fee-operation',force_new=True).json();assert not explicit['can_submit'] and 'original_render_unresolved' in explicit['reasons']


def test_all_reused_completed_tasks_do_not_need_provider_or_new_budget(wave_case,monkeypatch):
    case=wave_case;price=priced(case).json();result=submitted(case,price['id']).json()
    def complete(db):
        for item in result['items']:db.get(models.Job,item['job_id']).status='succeeded'
        db.scalar(select(models.TenantBudget)).monthly_limit_micros=0
    mutate(case,complete);monkeypatch.setattr(render_quotes,'require_render_support',lambda *args,**kw:(_ for _ in ()).throw(AssertionError('no new provider needed')))
    price=priced(case,key='reuse-completed').json();assert price['can_submit'] and not price['totals'] and all(item['disposition']=='reused' for item in price['items'])
    before=totals(case);assert submitted(case,price['id'],key='reuse-confirmed').status_code==200;assert totals(case)['UsageLedger']==before['UsageLedger']


def test_internal_child_render_quotes_cannot_bypass_batch_confirmation(wave_case):
    case=wave_case;price=priced(case).json();data={}
    def read(db):
        line=db.get(models.BatchRenderQuote,price['id']).snapshot_json['items'][0];data.update(line)
    mutate(case,read)
    request=case['client'].post(f"/api/v1/editing/{data['document_id']}/revisions/{data['revision_id']}/render",headers={**case['headers'],'Idempotency-Key':'bypass'},json={'quality':'high','quote_id':data['render_quote_id'],'output':{'format':'mp4'},'options':{'source':'studio-flow'}})
    assert request.status_code==409 and request.json()['detail']['code']=='render_quote_scope'


def test_selected_cancel_skips_a_completed_item_and_keeps_unknown_charge(wave_case):
    case=wave_case;price=priced(case).json();result=submitted(case,price['id']).json();view=run(case,result['run_id']);body=action_body(view);first,second=[item['job_id'] for item in result['items']]
    def update(db):
        db.get(models.Job,first).status='succeeded';db.add(models.ProviderOperation(tenant_id=case['tenant'],job_id=second,request_id=second,state='pending',external_job_id='original-remote'))
    mutate(case,update);before=totals(case);response=act(case,result['run_id'],body);assert response.status_code==200,response.text
    receipt=response.json();assert [(item['applied'],item.get('reason')) for item in receipt['items']]==[(False,'state_changed'),(True,None)]
    assert act(case,result['run_id'],body).json()==receipt and totals(case)==before
    def inspect(db):
        assert db.get(models.Job,first).status=='succeeded' and db.get(models.Job,second).status=='cancelled'
        assert db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id==second)).status=='pending_reconciliation'
        assert db.scalar(select(func.count()).select_from(models.JobAction).where(models.JobAction.job_id==second))==1
        batch_runs.reconcile_runs(db)
    mutate(case,inspect)
    assert run(case,result['run_id'])['aggregate']['status']=='partial_success'
    explicit=priced(case,key='after-cancel',force_new=True).json();assert not explicit['can_submit']


def test_selected_retry_reuses_failed_job_and_original_action_receipt(wave_case):
    case=wave_case;price=priced(case).json();result=submitted(case,price['id']).json();first=result['items'][0]['job_id']
    def fail(db):
        job=db.get(models.Job,first);job.status='failed';job.retryable=True
    mutate(case,fail);view=run(case,result['run_id']);body=action_body(view,'retry');body['items']=body['items'][:1];before=totals(case)
    response=act(case,result['run_id'],body);assert response.status_code==200,response.text;receipt=response.json();assert receipt['items'][0]['applied']
    mutate(case,fail)
    assert act(case,result['run_id'],body).json()==receipt and totals(case)==before
    assert case['client'].get('/api/v1/batches/'+case['batch']['id']+'/phases/'+result['run_id']+'/actions/requests/actions',headers=case['headers']).json()['result']==receipt


def test_cross_tenant_run_access_and_forged_action_items_are_rejected(wave_case):
    case=wave_case;price=priced(case).json();result=submitted(case,price['id']).json();view=run(case,result['run_id']);body=action_body(view);body['items'][0]['job_id']=case['job']['id'];before=totals(case)
    assert act(case,result['run_id'],body).status_code==404 and totals(case)==before
    tenant=case['client'].post('/api/v1/tenants',json={'slug':'foreign-runs','name':'Other'}).json()['id']
    for suffix in ('/runs/'+result['run_id'],'/render-quotes/'+price['id'],'/render-requests/render-confirm'):
        assert case['client'].get('/api/v1/batches/'+case['batch']['id']+suffix,headers={'X-Tenant-ID':tenant}).status_code==404


def test_expired_unconsumed_render_confirmation_has_server_abandon_proof(wave_case):
    case=wave_case;price=priced(case).json();path='/api/v1/batches/'+case['batch']['id']+'/render-requests/not-written?quote_id='+price['id']
    assert not case['client'].get(path,headers=case['headers']).json()['can_abandon']
    mutate(case,lambda db:setattr(db.get(models.BatchRenderQuote,price['id']),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    assert case['client'].get(path,headers=case['headers']).json()['can_abandon'] and submitted(case,price['id'],key='not-written').status_code==409
