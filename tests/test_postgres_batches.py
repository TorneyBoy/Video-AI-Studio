"""Real batch row locks and reconciliation races in disposable PG schemas."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from sqlalchemy import func,select
from app import models
from app.batch_schemas import BatchCreate,BatchSave
from app.services import batches,preparation_quotes
from app.services.editing_errors import EditingError
from test_catalog_reservations import snapshot
from test_postgres_completion import postgres_database as _postgres_database

postgres_database=_postgres_database


def setup(factory,existing,monkeypatch,*,same_batch=False,same_assets=True,ceiling=2_000_000):
    monkeypatch.setattr(preparation_quotes,'capability_catalog',lambda:{'capabilities':[deepcopy(snapshot())]})
    with factory() as db:
        tenant=db.get(models.Job,existing).tenant_id;project=models.Project(tenant_id=tenant,name='Batch race fixture');db.add(project);db.flush()
        template=models.Template(tenant_id=tenant,project_id=project.id,name='Batch',version='1',content={'schema_version':'1.0','id':'fixture','version':'1','duration':6,'slots':[{'name':'hook','type':'video','required':True}]});db.add(template);db.flush()
        ids=[]
        for i in range(4):
            row=models.Asset(tenant_id=tenant,project_id=project.id,name=str(i),kind='video',uri=f'https://fixture.invalid/{i}',metadata_json={});db.add(row);db.flush();ids.append(row.id)
        db.add(models.TenantBudget(tenant_id=tenant,currency='CNY',monthly_limit_micros=ceiling,task_limit_micros=600_000));db.flush()
        body={'project_id':project.id,'name':'First','shared':{'template_id':template.id,'asset_ids':ids[:2]},'items':[{'key':'a','name':'A'},{'key':'b','name':'B'}]}
        first=batches.create_batch(db,tenant,'fixture',BatchCreate(**body),'batch-first')
        second=first if same_batch else batches.create_batch(db,tenant,'fixture',BatchCreate(**{**body,'name':'Second','shared':{**body['shared'],'asset_ids':ids[:2] if same_assets else ids[2:]}}),'batch-second')
        quotes=[batches.create_quote(db,first,1,'quote-first'),batches.create_quote(db,second,1,'quote-second')];db.commit()
        return tenant,[first.id,second.id],[row.id for row in quotes]


def parallel(factory,tenant,ids,quotes):
    barrier=Barrier(2)
    def submit(index):
        with factory() as db:
            cached=db.get(models.ProductionBatch,ids[index]);assert cached.state=='draft';db.get(models.BatchQuote,quotes[index]);barrier.wait(timeout=10)
            try:
                batch=batches.batch_for(db,tenant,ids[index],lock=True);quote=batches.quote_for(db,batch,quotes[index],lock=True)
                result=batches.confirm_quote(db,batch,quote,1,f'confirm-{index}');db.commit();return result
            except EditingError as error:db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(submit,range(2)))


def test_parallel_batch_confirmation_creates_children_once(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,ids,quotes=setup(factory,existing,monkeypatch,same_batch=True)
    result=parallel(factory,tenant,ids,[quotes[0],quotes[0]])
    assert sum(isinstance(row,dict) for row in result)==1 and 'batch_quote_used' in result
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.type=='auto_edit.plan'))==2
        assert db.scalar(select(func.count()).select_from(models.UsageLedger))==2


def test_different_batches_share_analysis_without_sharing_independent_plans(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,ids,quotes=setup(factory,existing,monkeypatch)
    results=parallel(factory,tenant,ids,quotes);assert all(isinstance(row,dict) for row in results)
    assert not {row['job_id'] for row in results[0]['items']} & {row['job_id'] for row in results[1]['items']}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.type=='asset.analyze'))==2
        assert db.scalar(select(func.count()).select_from(models.UsageLedger))==2


def test_budget_competition_leaves_no_partial_batch_or_extra_analysis(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,ids,quotes=setup(factory,existing,monkeypatch,same_assets=False,ceiling=1_500_000)
    results=parallel(factory,tenant,ids,quotes);assert sum(isinstance(row,dict) for row in results)==1 and 'preparation_budget_exceeded' in results
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.ProductionBatch).where(models.ProductionBatch.state=='submitted'))==1
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.type=='auto_edit.plan'))==2
        assert db.scalar(select(func.sum(models.UsageLedger.amount_micros)))==1_000_000


def test_batch_save_uses_fresh_revision_under_parallel_editing(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,ids,_=setup(factory,existing,monkeypatch,same_batch=True);barrier=Barrier(2)
    def save(index):
        with factory() as db:
            stale=db.get(models.ProductionBatch,ids[0]);assert stale.revision==1;body=batches.draft_snapshot(db,stale);barrier.wait(timeout=10)
            try:
                current=batches.batch_for(db,tenant,ids[0],lock=True);result=batches.save_batch(db,current,'fixture',BatchSave(**{**body,'name':str(index),'base_revision':1}),f'save-{index}');db.commit();return result.number
            except EditingError as error:db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(save,range(2)))
    assert results.count(2)==1 and 'batch_revision_conflict' in results


def test_reconciliation_watermark_does_not_miss_child_completion_after_snapshot(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,ids,quotes=setup(factory,existing,monkeypatch,same_batch=True)
    with factory() as db:
        batch=batches.batch_for(db,tenant,ids[0],lock=True);quote=batches.quote_for(db,batch,quotes[0],lock=True);result=batches.confirm_quote(db,batch,quote,1,'confirm');db.commit()
        for item in result['items']:db.get(models.Job,item['job_id']).status='failed'
        db.commit()
    original=batches._metadata;called=[]
    def race(db,batch):
        read=original(db,batch)
        if not called:
            called.append(True)
            with factory() as other:
                for item in result['items']:other.get(models.Job,item['job_id']).status='succeeded'
                other.commit()
        return read
    monkeypatch.setattr(batches,'_metadata',race)
    with factory() as db:
        batches.reconcile_batches(db);assert db.get(models.Job,result['parent_job_id']).status=='failed'
        batches.reconcile_batches(db);assert db.get(models.Job,result['parent_job_id']).status=='succeeded'
