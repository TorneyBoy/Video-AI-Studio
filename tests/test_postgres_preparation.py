"""Real row locks: composite confirmation must not duplicate paid children."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from sqlalchemy import func,select
from app import models
from app.billing_schemas import AnalysisPreparationCreate
from app.services import preparation_quotes as service
from app.services.editing_errors import EditingError
from test_catalog_reservations import snapshot
from test_postgres_completion import postgres_database as _postgres_database

postgres_database=_postgres_database


def setup(factory,existing,monkeypatch,*,same_sources=True,ceiling=2_000_000):
    monkeypatch.setattr(service,'capability_catalog',lambda:{'capabilities':[deepcopy(snapshot())]})
    with factory() as db:
        tenant=db.get(models.Job,existing).tenant_id
        project=models.Project(tenant_id=tenant,name='Composite PG QA');db.add(project);db.flush()
        db.add(models.TenantBudget(tenant_id=tenant,currency='CNY',monthly_limit_micros=ceiling,task_limit_micros=600_000))
        sources=[]
        for i in range(4):
            asset=models.Asset(tenant_id=tenant,project_id=project.id,name=str(i),kind='video',uri=f'https://fixture.invalid/{i}',metadata_json={});db.add(asset);db.flush();sources.append(asset.id)
        first=service.create_quote(db,tenant,'analysis',AnalysisPreparationCreate(asset_ids=sources[:2],transcribe=False),'first')
        second=service.create_quote(db,tenant,'analysis',AnalysisPreparationCreate(asset_ids=sources[:2] if same_sources else sources[2:],transcribe=False),'second')
        db.commit();return tenant,[first.id,second.id]


def parallel(factory,tenant,quotes,*,same_key=False):
    barrier=Barrier(2)
    def submit(index):
        with factory() as db:
            cached=db.get(models.PreparationQuote,quotes[index]);assert cached.consumed_key is None
            barrier.wait(timeout=10)
            try:row=service.consume_quote(db,tenant,quotes[index],'shared' if same_key else f'intent-{index}');result=service.public_result(db,row);db.commit();return result
            except EditingError as error:db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(submit,range(2)))


def test_parallel_different_quotes_reuse_single_paid_analysis_batch(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,quotes=setup(factory,existing,monkeypatch)
    results=parallel(factory,tenant,quotes)
    assert {row['job_id'] for row in results[0]['items']}=={row['job_id'] for row in results[1]['items']}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.UsageLedger))==2
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.type=='asset.analyze'))==2


def test_parallel_batches_apply_aggregate_ceiling_without_partial_jobs(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,quotes=setup(factory,existing,monkeypatch,same_sources=False,ceiling=1_500_000)
    results=parallel(factory,tenant,quotes)
    assert sum(isinstance(row,dict) for row in results)==1 and 'preparation_budget_exceeded' in results
    with factory() as db:
        assert db.scalar(select(func.sum(models.UsageLedger.amount_micros)))==1_000_000
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.type=='asset.analyze'))==2


def test_parallel_consumption_refreshes_old_quote_and_refuses_other_key(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,quotes=setup(factory,existing,monkeypatch)
    results=parallel(factory,tenant,[quotes[0],quotes[0]])
    assert sum(isinstance(row,dict) for row in results)==1 and 'preparation_quote_used' in results
    with factory() as db:assert db.scalar(select(func.count()).select_from(models.UsageLedger))==2


def test_same_confirmation_key_cannot_consume_two_different_quotes(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,quotes=setup(factory,existing,monkeypatch,same_sources=False)
    results=parallel(factory,tenant,quotes,same_key=True)
    assert sum(isinstance(row,dict) for row in results)==1 and 'preparation_key_conflict' in results
