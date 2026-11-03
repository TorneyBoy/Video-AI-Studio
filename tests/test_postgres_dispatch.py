"""Real admission/provider mutex races using disposable PostgreSQL schemas."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy import func,select
import pytest
from app import models
from app.config import Settings
from app.services import dispatch
from app.services.completion import prepare_provider_call
from app.services.providers import ProviderRequest,ProviderRouter
from app.services.providers import ProviderCapability,ProviderExecutionError
from app.services.worker import _claim_job_workflow
from test_completion_contract import claim
from test_dispatch import BoundedProvider,batch,job,project,tenant
from test_postgres_completion import postgres_database as _postgres_database

postgres_database=_postgres_database


@pytest.mark.parametrize('scope',['global','tenant','project','batch'])
def test_two_pollers_cannot_overfill_the_same_capacity(postgres_database,scope):
    factory,existing=postgres_database
    with factory() as db:
        first=db.get(models.Job,existing);owner=first.tenant_id;p=project(db,owner);group=batch(db,owner,p)
        first.project_id=p;first.payload={**first.payload,'batch_id':group.id};second=job(db,owner,p);second.payload={**second.payload,'batch_id':group.id};ids=[first.id,second.id];db.commit()
    caps={f'dispatch_{key}_limit':16 for key in ('global','tenant','project','batch')};caps[f'dispatch_{scope}_limit']=1
    barrier=Barrier(2)
    def dispatch_one(_):
        with factory() as db:
            db.get(models.Job,existing);barrier.wait(timeout=10)
            value=dispatch.claim_next(db,families=('provider',),workflow_claim=_claim_job_workflow,settings=Settings(**caps))
            return value[0].id if value else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(dispatch_one,i) for i in range(2)];results=[future.result(timeout=25) for future in futures]
    assert sum(value is not None for value in results)==1
    with factory() as db:
        assert sorted(db.get(models.Job,id).attempt_count for id in ids)==[0,1]
        assert db.scalar(select(func.count()).select_from(models.WorkflowRun))==1
        assert db.get(models.DispatchClock,1).turn==1


@pytest.mark.parametrize('legacy_active',[False,True])
def test_selected_provider_cap_is_atomic_before_any_external_call(postgres_database,legacy_active):
    factory,existing=postgres_database;provider=BoundedProvider();router=ProviderRouter([provider]);tickets={}
    with factory() as db:
        first=db.get(models.Job,existing);second=job(db,tenant(db));ids=[first.id,second.id]
        if legacy_active:
            old=job(db,first.tenant_id);old.status='cancelled';old.payload={**old.payload,'provider_job_id':'legacy-remote'}
        db.commit()
        for id in ids:tickets[id]=claim(db,db.get(models.Job,id))[1]
    barrier=Barrier(2)
    def prepare(id):
        with factory() as db:
            row=db.get(models.Job,id);barrier.wait(timeout=10)
            try:
                value=prepare_provider_call(db,row,tickets[id],ProviderRequest(request_id=id,capability='video_generation',inputs={},provider='bounded'),router)
                return value.provider
            except dispatch.DispatchDeferred:db.rollback();return 'capacity_wait'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(prepare,id) for id in ids];results=[future.result(timeout=25) for future in futures]
    assert sorted(results)==(['capacity_wait','capacity_wait'] if legacy_active else ['bounded','capacity_wait']) and not provider.calls
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.ProviderOperation))==(0 if legacy_active else 1)
        if not legacy_active:assert db.scalar(select(models.ProviderOperation.state))=='submitting'


def test_provider_poll_can_query_original_remote_when_cap_is_full(postgres_database):
    factory,existing=postgres_database;provider=BoundedProvider();router=ProviderRouter([provider])
    with factory() as db:
        row=db.get(models.Job,existing);_,ticket=claim(db,row)
        db.add(models.ProviderOperation(tenant_id=row.tenant_id,job_id=row.id,request_id=row.id,provider_key='bounded',capability='video_generation',external_job_id='original-remote',state='pending'))
        other=job(db,row.tenant_id);other.status='cancelled';db.add(models.ProviderOperation(tenant_id=other.tenant_id,job_id=other.id,request_id=other.id,provider_key='bounded',capability='video_generation',state='unknown'));db.commit()
        request=prepare_provider_call(db,row,ticket,ProviderRequest(request_id=row.id,capability='video_generation',inputs={},provider='bounded'),router)
        assert request.parameters['provider_job_id']=='original-remote'
        assert db.scalar(select(func.count()).select_from(models.ProviderOperation))==2 and not provider.calls


def test_cross_month_first_submissions_compete_under_the_same_budget_lock(postgres_database):
    factory,existing=postgres_database;provider=BoundedProvider();provider.capabilities=(ProviderCapability(name='video_generation',input_types=('text',),output_types=('video',),commercial_allowed=True,limits={'max_concurrent':2}),)
    router=ProviderRouter([provider]);tickets={}
    with factory() as db:
        first=db.get(models.Job,existing);second=job(db,first.tenant_id);ids=[first.id,second.id]
        db.add(models.TenantBudget(tenant_id=first.tenant_id,currency='USD',monthly_limit_micros=1_000_000))
        for id in ids:db.add(models.UsageLedger(tenant_id=first.tenant_id,job_id=id,source='fixture',period_key='2020-01',status='reserved',amount_micros=600_000,currency='USD'))
        db.commit()
        for id in ids:tickets[id]=claim(db,db.get(models.Job,id))[1]
    barrier=Barrier(2)
    def prepare(id):
        with factory() as db:
            row=db.get(models.Job,id);barrier.wait(timeout=10)
            try:return prepare_provider_call(db,row,tickets[id],ProviderRequest(request_id=id,capability='video_generation',inputs={},provider='bounded'),router).provider
            except ProviderExecutionError as error:db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(prepare,id) for id in ids];results=[future.result(timeout=25) for future in futures]
    assert sorted(results)==['bounded','budget_dispatch_exceeded'] and not provider.calls
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.ProviderOperation))==1
        assert db.scalar(select(func.sum(models.UsageLedger.amount_micros)).where(models.UsageLedger.period_key!='2020-01'))==600_000
