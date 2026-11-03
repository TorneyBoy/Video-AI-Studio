"""Composite opt-in prices: atomic jobs, source binding and durable receipts."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import func,select
from app import models
from app.api import routers
from app.config import Settings
from app.services import preparation_quotes as service
from app.services.editing_errors import EditingError
from app.services.providers import ProviderExecutionError,ProviderCapability
from app.services.completion import prepare_provider_call
from app.services.worker import process_queued_auto_edit_jobs,_external_analysis_request
from test_artifact_reviews import mutate
from test_edit_revisions import edit_case as _edit_case
from test_catalog_reservations import snapshot
from test_completion_contract import claim

edit_case=_edit_case


@pytest.fixture
def prepared(edit_case,monkeypatch):
    case=edit_case;case['price']=snapshot()
    monkeypatch.setattr(service,'capability_catalog',lambda:{'capabilities':[deepcopy(case['price'])]})
    def setup(db):
        db.add(models.TenantBudget(tenant_id=case['tenant'],currency='CNY',monthly_limit_micros=2_000_000,task_limit_micros=600_000))
        ids=[]
        for number in range(3):
            row=models.Asset(tenant_id=case['tenant'],project_id=case['project'],name=f'Unanalysed {number}',kind='video',uri=f'https://fixture.invalid/{number}.mp4',metadata_json={'upload':{'status':'ready'}})
            db.add(row);db.flush();ids.append(row.id)
        case['fresh']=ids
    mutate(case,setup);return case


def quote(case,kind='analysis',key='quote',**extra):
    body={'asset_ids':case['fresh'][:2],'transcribe':False} if kind=='analysis' else {'project_id':case['project'],'template_id':case['template'],'asset_ids':[row['id'] for row in case['assets']]}
    return case['client'].post('/api/v1/preparation/'+kind+'-quotes',headers={**case['headers'],'Idempotency-Key':key},json={**body,**extra})


def confirm(case,id,key='confirmed'):
    return case['client'].post('/api/v1/preparation/quotes/'+id+'/confirm',headers={**case['headers'],'Idempotency-Key':key},json={})


def counts(case):
    out={}
    def read(db):
        for table in (models.Job,models.UsageLedger,models.PreparationQuote):out[table.__name__]=db.scalar(select(func.count()).select_from(table))
    mutate(case,read);return out


def test_quote_is_metadata_only_and_confirmation_has_stable_expired_receipt(prepared):
    case=prepared;before=counts(case);response=quote(case);assert response.status_code==200,response.text
    row=response.json();assert row['totals']==[{'currency':'CNY','amount_micros':'1000000'}] and row['can_submit']
    assert counts(case)=={**before,'PreparationQuote':before['PreparationQuote']+1}
    assert quote(case).json()['id']==row['id']
    assert 'fixture.invalid' not in response.text and 'analysis_snapshot' not in response.text
    submitted=confirm(case,row['id']);assert submitted.status_code==200,submitted.text
    result=submitted.json();assert len({item['job_id'] for item in result['items']})==2
    assert counts(case)['UsageLedger']==2 and counts(case)['Job']==before['Job']+2
    mutate(case,lambda db:setattr(db.get(models.PreparationQuote,row['id']),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    assert confirm(case,row['id']).json()==result
    assert confirm(case,row['id'],'other-key').status_code==409
    receipt=case['client'].get('/api/v1/preparation/quotes/'+row['id']+'/requests/confirmed',headers=case['headers']).json()
    assert receipt['found'] and receipt['result']==result and receipt['status']=='consumed'


@pytest.mark.parametrize('change',['source','expiry','price','budget','project_limit'])
def test_confirmation_revalidates_entire_batch_before_creating_jobs(prepared,change):
    case=prepared;row=quote(case).json();before=counts(case)
    def update(db):
        if change=='source':db.get(models.Asset,case['fresh'][1]).uri='https://fixture.invalid/replaced'
        elif change=='expiry':db.get(models.PreparationQuote,row['id']).expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        elif change=='budget':db.scalar(select(models.TenantBudget)).monthly_limit_micros=999_999
        elif change=='project_limit':db.add(models.ProjectBudget(tenant_id=case['tenant'],project_id=case['project'],currency='CNY',limit_micros=999_999,actor_id='fixture-owner'))
    if change=='price':case['price']['price']['version']='changed'
    else:mutate(case,update)
    response=confirm(case,row['id']);assert response.status_code==409,response.text
    assert counts(case)==before


def test_per_child_task_ceiling_and_aggregate_monthly_limit_are_distinct(prepared):
    case=prepared;assert quote(case).json()['can_submit'] # 1.0 total, each 0.5 <= 0.6
    mutate(case,lambda db:setattr(db.scalar(select(models.TenantBudget)),'task_limit_micros',499_999))
    assert 'task_limit' in quote(case,key='too-big').json()['reasons']


def test_two_quotes_for_same_assets_downgrade_to_original_tasks_without_double_charge(prepared):
    case=prepared;first=quote(case).json();second=quote(case,key='second').json()
    one=confirm(case,first['id']).json();two=confirm(case,second['id'],'second-confirm').json()
    assert [item['job_id'] for item in one['items']]==[item['job_id'] for item in two['items']]
    assert all(item['disposition']=='reused' for item in two['items']) and counts(case)['UsageLedger']==2
    forced=quote(case,key='explicit-new',force_new=True).json()
    assert not forced['can_submit'] and 'original_analysis_unresolved' in forced['reasons']
    assert confirm(case,forced['id'],'fresh-confirm').status_code==409


def test_failed_or_unknown_original_requires_recovery_and_explicit_new_intent(prepared):
    case=prepared;first=quote(case,asset_ids=case['fresh'][:1]).json();job=confirm(case,first['id']).json()['items'][0]['job_id']
    def fail(db):
        db.get(models.Job,job).status='failed';db.add(models.ProviderOperation(tenant_id=case['tenant'],job_id=job,request_id=job,state='unknown'))
    mutate(case,fail)
    assert not quote(case,key='reuse',asset_ids=case['fresh'][:1]).json()['can_submit']
    assert not quote(case,key='fresh',asset_ids=case['fresh'][:1],force_new=True).json()['can_submit']
    assert not quote(case,key='changed-options',asset_ids=case['fresh'][:1],transcribe=True,force_new=True).json()['can_submit']


def test_cached_plan_works_without_provider_and_freezes_original_analysis(prepared,monkeypatch):
    case=prepared;monkeypatch.setattr(service,'capability_catalog',lambda:(_ for _ in ()).throw(AssertionError('no provider needed')))
    row=quote(case,'plan').json();assert row['can_submit'] and not row['totals']
    before=counts(case);response=case['client'].post('/api/v1/jobs/auto-edit',headers={**case['headers'],'Idempotency-Key':'plan-confirm'},json={'project_id':case['project'],'template_id':case['template'],'asset_ids':[asset['id'] for asset in case['assets']],'quote_id':row['id']})
    assert response.status_code==201,response.text
    job_id=response.json()['id'];assert counts(case)['Job']==before['Job']+1 and counts(case)['UsageLedger']==0
    def execute(db):
        asset=db.get(models.Asset,case['assets'][0]['id']);asset.metadata_json={**asset.metadata_json,'analysis':{'shots':[{'start':100,'end':104,'duration':4}]}};db.commit()
        assert process_queued_auto_edit_jobs(db)[0]['status']=='succeeded'
        revision=db.scalar(select(models.PlanRevision).join(models.EditDocument).where(models.EditDocument.origin_job_id==job_id))
        assert max(shot['source_end'] for shot in revision.plan_json['selected'])<=8
    mutate(case,execute)


def test_changed_cached_result_and_plan_settings_require_new_quote(prepared):
    case=prepared;row=quote(case,'plan').json();before=counts(case)
    def change(db):
        asset=db.get(models.Asset,case['assets'][0]['id']);asset.metadata_json={**asset.metadata_json,'analysis':{'shots':[]}}
    mutate(case,change);assert confirm(case,row['id']).status_code==409 and counts(case)==before
    other=quote(case,'plan',key='new-plan',asset_ids=[case['assets'][1]['id']]).json()
    result=case['client'].post('/api/v1/jobs/auto-edit',headers={**case['headers'],'Idempotency-Key':'wrong-input'},json={'project_id':case['project'],'template_id':case['template'],'asset_ids':[case['assets'][1]['id']],'target_duration':12,'quote_id':other['id']})
    assert result.status_code==409


def test_price_source_checks_apply_only_to_new_calls_and_request_uses_frozen_source(prepared):
    case=prepared;row=quote(case,asset_ids=case['fresh'][:1]).json();result=confirm(case,row['id']).json();job_id=result['items'][0]['job_id']
    def check(db):
        job=db.get(models.Job,job_id);asset=db.get(models.Asset,case['fresh'][0]);service.validate_analysis_price(db,job)
        case['price']['price']['version']='new'
        with pytest.raises(ProviderExecutionError,match='price'):service.validate_analysis_price(db,job)
        asset.uri='https://fixture.invalid/new-source';db.flush()
        with pytest.raises(ProviderExecutionError,match='inputs'):service.validate_analysis_source(job,asset)
        assert _external_analysis_request(job,asset,job.payload['options']).inputs['uri']==job.payload['analysis_input']['uri']
    mutate(case,check)


def test_session_cannot_bypass_quote_and_confirmation_cannot_inject_price(prepared,monkeypatch):
    case=prepared;monkeypatch.setattr(routers,'get_settings',lambda:Settings(auth_mode='session',auth_public_origin='http://testserver'))
    assert case['client'].post('/api/v1/assets/'+case['fresh'][0]+'/analyze',headers=case['headers'],json={}).status_code==422
    assert case['client'].post('/api/v1/jobs/auto-edit',headers=case['headers'],json={'project_id':case['project'],'template_id':case['template']}).status_code==422
    row=quote(case).json()
    assert case['client'].post('/api/v1/preparation/quotes/'+row['id']+'/confirm',headers={**case['headers'],'Idempotency-Key':'bad'},json={'price':0}).status_code==422
    assert quote(case,key='bad-options',options={'provider_job_id':'forged'}).status_code==422


def test_cross_tenant_receipts_and_quotes_are_invisible(prepared):
    case=prepared;row=quote(case).json();tenant=case['client'].post('/api/v1/tenants',json={'slug':'other-preparation','name':'Other'}).json()['id']
    for suffix in ('','/requests/confirmed'):
        assert case['client'].get('/api/v1/preparation/quotes/'+row['id']+suffix,headers={'X-Tenant-ID':tenant}).status_code==404


def test_source_hash_ignores_labels_and_completed_analysis(prepared):
    case=prepared
    def check(db):
        asset=db.get(models.Asset,case['fresh'][0]);original=service.source_hash(asset);asset.name='Renamed';asset.metadata_json={**asset.metadata_json,'tags':['safe'],'duration':30,'analysis':{'media':{'status':'ok'}}};assert service.source_hash(asset)==original
        asset.uri='https://fixture.invalid/replaced';assert service.source_hash(asset)!=original
    mutate(case,check)


def test_worker_preserves_known_remote_operation_when_prices_and_source_change(prepared):
    case=prepared;row=quote(case,asset_ids=case['fresh'][:1]).json();job_id=confirm(case,row['id']).json()['items'][0]['job_id']
    def execute(db):
        job=db.get(models.Job,job_id);asset=db.get(models.Asset,case['fresh'][0]);_,ticket=claim(db,job)
        router=type('Router',(),{'choose':lambda self,request:type('Selected',(),{'name':'analysis',
            'capabilities':(ProviderCapability(name='media_analysis',input_types=('video',),output_types=('analysis',),limits={'max_concurrent':1}),)})()})()
        request=_external_analysis_request(job,asset,job.payload['options'])
        request=prepare_provider_call(db,job,ticket,request,router)
        operation=db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id==job.id));operation.state='pending';operation.external_job_id='original-remote'
        job.payload={**job.payload,'provider_job_id':'original-remote'};asset.uri='https://fixture.invalid/new-source';db.commit()
        case['price']['price']['version']='changed'
        request=prepare_provider_call(db,job,ticket,_external_analysis_request(job,asset,job.payload['options']),router)
        assert request.parameters['provider_job_id']=='original-remote' and operation.external_job_id=='original-remote'
        with pytest.raises(ProviderExecutionError):service.validate_analysis_source(job,asset)
    mutate(case,execute)


def test_late_budget_failure_rolls_back_all_children_and_reservations(prepared,monkeypatch):
    case=prepared;row=quote(case).json();before=counts(case);original=service.preflight_job_budget;calls=[]
    def fail(db,job,**kwargs):
        calls.append(job.id)
        if len(calls)==2:raise EditingError('preparation_budget_exceeded','changed',409)
        return original(db,job,**kwargs)
    monkeypatch.setattr(service,'preflight_job_budget',fail)
    assert confirm(case,row['id']).status_code==409 and counts(case)==before
