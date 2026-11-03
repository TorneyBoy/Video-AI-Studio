"""Cost/quote/task metadata contracts, with no provider or media calls."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from types import SimpleNamespace
from sqlalchemy import func,select
import pytest
from app import models
from app.api import routers
from app.config import Settings
from app.services import render_quotes
from app.services.budgets import preflight_job_budget,settle_provider_cost
from app.services import completion,artifact_reviews
from app.services.providers import ProviderRequest,ProviderExecutionError
from test_artifact_reviews import mutate
from test_edit_revisions import edit_case as _edit_case
from test_catalog_reservations import snapshot
from test_completion_contract import claim
from test_personal_identity import accounts as _accounts,register
from test_capability_catalog import config

edit_case=_edit_case
accounts=_accounts


@pytest.fixture
def billing_case(edit_case,monkeypatch):
    case=edit_case;selected=snapshot();selected.update(id='render:video_editing:1',capability='video_editing')
    case['selected']=selected
    monkeypatch.setattr(render_quotes,'require_render_support',lambda *args,**kw:deepcopy(case['selected']))
    monkeypatch.setattr(routers,'require_render_support',lambda *args,**kw:deepcopy(case['selected']))
    monkeypatch.setattr(render_quotes,'get_settings',lambda:Settings(auth_mode='session',auth_public_origin='http://testserver'))
    def policy(db):
        row=db.scalar(select(models.TenantBudget).where(models.TenantBudget.tenant_id==case['tenant']))
        if not row:row=models.TenantBudget(tenant_id=case['tenant']);db.add(row)
        row.currency='CNY';row.monthly_limit_micros=2_000_000;row.task_limit_micros=1_000_000;row.enabled=True
    mutate(case,policy)
    return case


def post_quote(case,key='quotation',**body):
    rev=case['revision']
    return case['client'].post(f"/api/v1/editing/{rev['document_id']}/revisions/{rev['revision_id']}/render-quote",
        headers={**case['headers'],'Idempotency-Key':key},json={'quality':'high',**body})


def render(case,quote,key='render-confirmed',**body):
    rev=case['revision']
    return case['client'].post(f"/api/v1/editing/{rev['document_id']}/revisions/{rev['revision_id']}/render",
        headers={**case['headers'],'Idempotency-Key':key},json={'quality':'high','quote_id':quote,**body})


def counts(case):
    values={}
    def read(db):
        for table in (models.Job,models.UsageLedger,models.RenderQuote):values[table.__name__]=db.scalar(select(func.count()).select_from(table))
    mutate(case,read);return values


def test_quote_is_read_only_and_one_confirmation_has_one_receipt(billing_case):
    case=billing_case;before=counts(case);response=post_quote(case)
    assert response.status_code==200,response.text
    quote=response.json();assert quote['amount_micros']=='500000' and quote['can_submit'] and not quote['bill_confirmed']
    assert counts(case)=={**before,'RenderQuote':before['RenderQuote']+1}
    assert post_quote(case).json()['id']==quote['id']
    assert post_quote(case,output={'format':'webm'}).status_code==409
    assert render(case,None).status_code==422
    submitted=render(case,quote['id']);assert submitted.status_code==201,submitted.text
    job=submitted.json()['job'];assert job['payload']['quote_id']==quote['id']
    targets=case['client'].get(f"/api/v1/jobs?project_id={case['project']}&revision_id={case['revision']['revision_id']}&job_type=render.high&order=desc&limit=1",headers=case['headers'])
    assert [row['id'] for row in targets.json()]==[job['id']]
    assert case['client'].get('/api/v1/jobs?revision_id=another-revision',headers=case['headers']).json()==[]
    assert counts(case)['Job']==before['Job']+1 and counts(case)['UsageLedger']==before['UsageLedger']+1
    assert render(case,quote['id'],key='different-intent').status_code==409
    mutate(case,lambda db:setattr(db.get(models.RenderQuote,quote['id']),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    assert render(case,quote['id']).json()['job']['id']==job['id']
    read=case['client'].get('/api/v1/billing/render-quotes/'+quote['id'],headers=case['headers'])
    assert read.json()['status']=='consumed' and read.json()['job_id']==job['id']


@pytest.mark.parametrize('change',['expiry','price','options','source'])
def test_changed_quote_never_creates_or_reserves_a_job(billing_case,change):
    case=billing_case;quote=post_quote(case).json();before=counts(case);extra={}
    if change=='expiry':mutate(case,lambda db:setattr(db.get(models.RenderQuote,quote['id']),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    elif change=='price':case['selected']['price']['version']='new-price'
    elif change=='options':extra={'options':{'new-option':True}}
    else:
        source=case['assets'][0]['id']
        mutate(case,lambda db:setattr(db.get(models.Asset,source),'uri','https://fixture.invalid/changed-source'))
    result=render(case,quote['id'],**extra)
    assert result.status_code in {409,422},result.text
    assert counts(case)==before


def test_project_budget_limits_reservations_and_actual_cost_without_raising_tenant_limit(billing_case):
    case=billing_case;path=f"/api/v1/projects/{case['project']}/spending-limit"
    saved=case['client'].post(path,headers=case['headers'],json={'base_revision':0,'currency':'CNY','limit_micros':600_000})
    assert saved.status_code==200 and saved.json()['revision']==1
    quote=post_quote(case).json();job=render(case,quote['id']).json()['job']
    assert post_quote(case,key='second-quote').json()['reasons']==['project_limit']
    too_low=case['client'].post(path,headers=case['headers'],json={'base_revision':1,'currency':'CNY','limit_micros':400_000})
    assert too_low.status_code==422
    stale=case['client'].post(path,headers=case['headers'],json={'base_revision':0,'currency':'CNY','limit_micros':2_000_000})
    assert stale.status_code==409
    def settle(db):
        decision=settle_provider_cost(db,job=db.get(models.Job,job['id']),cost={'amount':.7,'currency':'CNY'},usage={})
        assert not decision.allowed and 'project_limit' in decision.audit['reasons']
        assert db.scalar(select(models.TenantBudget)).monthly_limit_micros==2_000_000
    mutate(case,settle)
    summary=case['client'].get('/api/v1/billing/overview?project_id='+case['project'],headers=case['headers']).json()
    assert summary['totals'][0]['settled_micros']=='700000' and summary['project_policy']['remaining_micros']=='0'


def action(case,job_id,key,body):
    return case['client'].post(f'/api/v1/billing/jobs/{job_id}/actions',headers={**case['headers'],'Idempotency-Key':key},json=body)


def test_original_action_receipt_does_not_retry_a_later_failure_again(billing_case):
    case=billing_case;ids={}
    def seed(db):
        job=models.Job(tenant_id=case['tenant'],project_id=case['project'],type='render.high',status='failed',retryable=True,
            attempt_count=1,execution_generation=1,max_attempts=3,payload={'provider_job_id':'not-public','secret':'never-public'})
        db.add(job);db.flush();ids['job']=job.id
    mutate(case,seed);job_id=ids['job'];body={'action':'retry','expected_status':'failed','expected_attempt':1,'expected_generation':1}
    first=action(case,job_id,'original',body);assert first.status_code==200,first.text
    def failed_again(db):
        job=db.get(models.Job,job_id);job.status='failed';job.attempt_count=2;job.execution_generation=2
    mutate(case,failed_again)
    repeated=action(case,job_id,'original',body)
    assert repeated.json()['current']['status']=='failed' and repeated.json()['action']['id']==first.json()['action']['id']
    assert action(case,job_id,'new-but-stale',body).status_code==409
    assert action(case,job_id,'original',{**body,'action':'cancel'}).status_code==409
    receipt=case['client'].get(f'/api/v1/billing/jobs/{job_id}/actions/original',headers=case['headers'])
    assert receipt.json()['found'] and 'not-public' not in receipt.text and 'never-public' not in receipt.text


def test_cancellation_retains_unknown_spending_and_cannot_raise_retry_budget(billing_case):
    case=billing_case;quote=post_quote(case).json();job=render(case,quote['id']).json()['job']
    def unknown(db):
        row=db.get(models.Job,job['id']);row.status='running'
        settle_provider_cost(db,job=row,cost={'currency':'CNY'},usage={},provider=case['selected']['provider'],model_id=case['selected']['model_id'],model_version='1')
    mutate(case,unknown)
    state=case['client'].get('/api/v1/billing/jobs/'+job['id'],headers=case['headers']).json()
    body={'action':'cancel','expected_status':state['status'],'expected_attempt':state['attempt_count'],'expected_generation':state['execution_generation']}
    cancelled=action(case,job['id'],'cancel-original',body);assert cancelled.status_code==200,cancelled.text
    entries=case['client'].get('/api/v1/billing/entries',headers=case['headers']).json()['items']
    assert entries[0]['status']=='pending_reconciliation' and entries[0]['amount_micros']=='500000'
    assert 'provider_job_id' not in str(entries)
    assert action(case,job['id'],'forged-force',{**body,'force':True}).status_code==422
    assert case['client'].get('/api/v1/billing/overview?project_id='+case['project'],headers={'X-Tenant-ID':'other'}).status_code==404


def test_project_limit_does_not_mix_currency_or_overwrite_other_project_spending(billing_case):
    case=billing_case
    def seed(db):
        project=models.Project(tenant_id=case['tenant'],name='Other');db.add(project);db.flush()
        job=models.Job(tenant_id=case['tenant'],project_id=project.id,type='fixture');db.add(job);db.flush()
        preflight_job_budget(db,job,selected_capability=case['selected'])
    mutate(case,seed)
    path=f"/api/v1/projects/{case['project']}/spending-limit"
    assert case['client'].post(path,headers=case['headers'],json={'base_revision':0,'currency':'USD','limit_micros':1_000_000}).status_code==200
    quote=post_quote(case).json();assert quote['reasons']==['project_currency_mismatch']
    assert quote['amount_micros']=='500000'


@pytest.mark.parametrize('remote',[False,True])
def test_price_changes_stop_first_dispatch_but_do_not_replace_a_remote_job(billing_case,monkeypatch,remote):
    case=billing_case;quote=post_quote(case).json();job=render(case,quote['id']).json()['job']
    changed=deepcopy(case['selected']);changed['price']['version']='new-price'
    monkeypatch.setattr(completion,'require_render_support',lambda *args,**kw:changed)
    def exercise(db):
        row=db.get(models.Job,job['id']);_,ticket=claim(db,row)
        if remote:
            db.add(models.ProviderOperation(tenant_id=row.tenant_id,job_id=row.id,request_id=row.id,
                provider_key=case['selected']['provider'],external_job_id='known-remote-id',state='pending'));db.commit()
        request=ProviderRequest(request_id=row.id,capability='video_editing',inputs={'timeline':row.payload['manifest']['source']['timeline']},
            model_id=case['selected']['model_id'],model_version='1')
        router=SimpleNamespace(choose=lambda request:SimpleNamespace(name=case['selected']['provider']))
        if remote:
            prepared=completion.prepare_provider_call(db,row,ticket,request,router)
            assert prepared.parameters['provider_job_id']=='known-remote-id'
        else:
            with pytest.raises(ProviderExecutionError) as error:completion.prepare_provider_call(db,row,ticket,request,router)
            assert error.value.code=='render_quote_changed'
            assert db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id==row.id)) is None
    mutate(case,exercise)


def test_price_change_between_quote_check_and_budget_reservation_rolls_back(billing_case,monkeypatch):
    case=billing_case;quote=post_quote(case).json();before=counts(case);calls=[]
    def choose(*args,**kw):
        calls.append(True);value=deepcopy(case['selected'])
        if len(calls)>1:value['price']['version']='changed-before-reservation'
        return value
    monkeypatch.setattr(routers,'require_render_support',choose)
    assert render(case,quote['id']).status_code==409
    assert counts(case)==before
    def read(db):assert db.get(models.RenderQuote,quote['id']).consumed_key is None
    mutate(case,read)


def test_real_session_owner_only_project_limits_and_lossless_money_strings(accounts,monkeypatch):
    client,factory,settings=accounts;monkeypatch.setattr(artifact_reviews,'get_settings',lambda:settings)
    user=register(client).json()['user'];project=client.post('/api/v1/projects',json={'name':'Budget scope'}).json()['id']
    path=f'/api/v1/projects/{project}/spending-limit'
    assert client.post(path,json={'base_revision':0,'currency':'USD','limit_micros':0}).status_code==200
    with factory() as db:
        member=db.scalar(select(models.Membership).where(models.Membership.user_id==user['id']));member.role='editor'
        job=models.Job(tenant_id=user['tenant_id'],project_id=project,type='metadata-fixture');db.add(job);db.flush()
        db.add(models.UsageLedger(tenant_id=user['tenant_id'],job_id=job.id,amount_micros=9007199254740993,
            status='settled',source='metadata-fixture',period_key='2026-10',currency='CNY'));db.commit()
    assert client.post(path,json={'base_revision':1,'currency':'USD','limit_micros':1}).status_code==403
    summary=client.get('/api/v1/billing/overview?project_id='+project).json()
    assert summary['totals'][0]['settled_micros']=='9007199254740993'
    assert not summary['project_policy']['can_manage']
    assert client.get('/api/v1/billing/overview?project_id=not-this-tenant').status_code==404


def test_plain_video_quotes_use_the_real_catalog_selection_and_input_limits(billing_case,monkeypatch):
    from app.services import render_features
    case=billing_case;configured=config();capability=configured['providers'][0]['capabilities'][0]
    capability['cost']={'per_request':.5,'currency':'CNY'};capability['limits']['formats']=['mp4']
    monkeypatch.setenv('TEST_CATALOG_API_KEY','offline-quotation-fixture-only')
    monkeypatch.delenv('PROVIDER_REGISTRY_PATH',raising=False)
    monkeypatch.setenv('PROVIDER_REGISTRY_JSON',json.dumps(configured))
    monkeypatch.setattr(render_quotes,'require_render_support',render_features.require_render_support)
    monkeypatch.setattr(routers,'require_render_support',render_features.require_render_support)
    quote=post_quote(case)
    assert quote.status_code==200,quote.text
    assert quote.json()['provider']=='verified-gateway' and quote.json()['amount_micros']=='500000'
    result=render(case,quote.json()['id']);assert result.status_code==201,result.text
    before=counts(case)
    assert post_quote(case,key='unsupported-format',output={'format':'avi'}).status_code==422
    assert counts(case)==before
    capability['limits']['max_duration_seconds']=1;monkeypatch.setenv('PROVIDER_REGISTRY_JSON',json.dumps(configured))
    assert post_quote(case,key='unsupported-duration').status_code==422
    assert counts(case)==before and 'offline-quotation-fixture-only' not in quote.text
