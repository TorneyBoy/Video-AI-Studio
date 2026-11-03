"""Render-wave row locks, atomic budgets and observed aggregation snapshots."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from sqlalchemy import func,select
from app import models
from app.batch_schemas import BatchCreate,BatchRenderCreate,BatchTaskRequest
from app.services import batches,batch_runs,render_quotes
from app.services.worker import process_queued_auto_edit_jobs
from app.services.editing_errors import EditingError
from test_catalog_reservations import snapshot
from test_postgres_completion import postgres_database as _postgres_database

postgres_database=_postgres_database


def setup(factory,existing,monkeypatch,*,different=False,ceiling=2_000_000):
    price=snapshot();price.update(id='render:video_editing:1',capability='video_editing')
    monkeypatch.setattr(render_quotes,'require_render_support',lambda *args,**kw:deepcopy(price))
    with factory() as db:
        tenant=db.get(models.Job,existing).tenant_id
        project=models.Project(tenant_id=tenant,name='Wave races');db.add(project);db.flush()
        template=models.Template(tenant_id=tenant,project_id=project.id,name='Quick',version='1',content={'id':'quick','version':'1','duration':4,'slots':[{'name':'hook','type':'video','required':True}]});db.add(template)
        asset=models.Asset(tenant_id=tenant,project_id=project.id,name='Source',kind='video',uri='https://fixture.invalid/source.mp4',metadata_json={'duration':4,'analysis':{'shots':[{'start':0,'end':4,'duration':4,'quality':1}]}});db.add(asset);db.flush()
        db.add(models.TenantBudget(tenant_id=tenant,currency='CNY',monthly_limit_micros=ceiling,task_limit_micros=600_000));db.flush()
        ids=[]
        for index in range(2 if different else 1):
            body=BatchCreate(project_id=project.id,name=str(index),shared={'template_id':template.id,'asset_ids':[asset.id]},items=[{'key':'a','name':'A'},{'key':'b','name':'B'}])
            batch=batches.create_batch(db,tenant,'fixture',body,f'batch-{index}');quote=batches.create_quote(db,batch,1,f'plan-price-{index}')
            batches.confirm_quote(db,batch,quote,1,f'plan-confirm-{index}');ids.append(batch.id);db.commit()
        process_queued_auto_edit_jobs(db,limit=10)
        quotes=[]
        for index,id in enumerate(ids if different else ids*2):
            batch=batches.batch_for(db,tenant,id);view=batches.batch_read(db,batch)
            assert all(row['status']=='succeeded' and row['revision_id'] for row in view['items'])
            request=BatchRenderCreate(items=[{'key':row['key'],'revision_id':row['revision_id']} for row in view['items']],quality='high')
            quotes.append(batch_runs.create_quote(db,batch,request,f'render-price-{index}').id)
        db.commit();return tenant,ids if different else ids*2,quotes


def parallel(factory,tenant,ids,quotes,keys):
    barrier=Barrier(2)
    def submit(index):
        with factory() as db:
            db.get(models.ProductionBatch,ids[index]);db.get(models.BatchRenderQuote,quotes[index]);barrier.wait(timeout=10)
            try:
                batch=batches.batch_for(db,tenant,ids[index],lock=True);quote=batch_runs.quote_for(db,batch,quotes[index],lock=True)
                result=batch_runs.confirm_quote(db,batch,quote,keys[index]);db.commit();return result
            except EditingError as error:db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(submit,index) for index in range(2)]
        return [future.result(timeout=25) for future in futures]


@pytest.mark.parametrize('same_quote',[True,False])
def test_parallel_render_confirmation_reserves_each_original_once(postgres_database,monkeypatch,same_quote):
    factory,existing=postgres_database;tenant,ids,quotes=setup(factory,existing,monkeypatch)
    results=parallel(factory,tenant,ids,[quotes[0]]*2 if same_quote else quotes,['same']*2 if same_quote else ['first','second'])
    assert all(isinstance(row,dict) for row in results)
    assert {row['job_id'] for row in results[0]['items']}=={row['job_id'] for row in results[1]['items']}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.type=='render.high'))==2
        assert db.scalar(select(func.sum(models.UsageLedger.amount_micros)))==1_000_000
        assert db.scalar(select(func.count()).select_from(models.BatchRun))==(1 if same_quote else 2)


def test_render_budget_competition_rolls_back_the_whole_losing_wave(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,ids,quotes=setup(factory,existing,monkeypatch,different=True,ceiling=1_500_000)
    results=parallel(factory,tenant,ids,quotes,['first','second'])
    assert sum(isinstance(row,dict) for row in results)==1 and 'batch_render_budget_exceeded' in results
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.BatchRun))==1
        assert db.scalar(select(func.count()).select_from(models.BatchRunItem))==2
        assert db.scalar(select(func.sum(models.UsageLedger.amount_micros)))==1_000_000
        assert db.scalar(select(func.count()).select_from(models.BatchRenderQuote).where(models.BatchRenderQuote.consumed_key.is_not(None)))==1


def test_parallel_original_actions_have_one_fixed_receipt(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,ids,quotes=setup(factory,existing,monkeypatch)
    result=parallel(factory,tenant,ids,[quotes[0]]*2,['same']*2)[0]
    with factory() as db:
        batch=batches.batch_for(db,tenant,ids[0]);view=batch_runs.run_read(db,batch,batch_runs.run_for(db,batch,result['run_id']))
        request=BatchTaskRequest(action='cancel',items=[{'key':row['key'],'job_id':row['job_id'],'expected_status':'queued','expected_attempt':0,'expected_generation':0} for row in view['items']])
    barrier=Barrier(2)
    def cancel(_):
        with factory() as db:
            barrier.wait(timeout=10);batch=batches.batch_for(db,tenant,ids[0],lock=True)
            receipt=batch_runs.apply_actions(db,batch,result['run_id'],'owner','cancel-originals',request);db.commit();return receipt.result_json
    with ThreadPoolExecutor(max_workers=2) as pool:receipts=list(pool.map(cancel,range(2)))
    assert receipts[0]==receipts[1] and all(row['applied'] for row in receipts[0]['items'])
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.JobAction))==2
        assert db.scalar(select(func.count()).select_from(models.BatchTaskAction))==1
        assert {db.get(models.Job,row['job_id']).status for row in result['items']}=={'cancelled'}


def test_render_aggregate_keeps_the_exact_observed_child_watermark(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,ids,quotes=setup(factory,existing,monkeypatch)
    result=parallel(factory,tenant,ids,[quotes[0]]*2,['same']*2)[0]
    with factory() as db:
        for row in result['items']:db.get(models.Job,row['job_id']).status='failed'
        db.commit()
    original=batch_runs.task_rows;called=[]
    def race(db,job_ids,tenant):
        rows=original(db,job_ids,tenant)
        if not called:
            called.append(True)
            with factory() as other:
                for row in result['items']:other.get(models.Job,row['job_id']).status='succeeded'
                other.commit()
        return rows
    monkeypatch.setattr(batch_runs,'task_rows',race)
    with factory() as db:
        batch_runs.reconcile_runs(db);assert db.get(models.Job,result['parent_job_id']).status=='failed'
        batch_runs.reconcile_runs(db);assert db.get(models.Job,result['parent_job_id']).status=='succeeded'
