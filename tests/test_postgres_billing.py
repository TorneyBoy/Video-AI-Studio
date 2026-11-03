"""Project ceilings and quote consumption serialize in a disposable PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from sqlalchemy import func,select
from app import models,schemas
from app.billing_schemas import ProjectLimitUpdate
from app.config import Settings
from app.services import render_quotes
from app.services.budgets import preflight_job_budget
from app.services.editing_errors import EditingError
from app.services.project_spending import update_limit
from test_artifact_reviews import legacy_output
from test_catalog_reservations import snapshot
from test_postgres_completion import postgres_database as _postgres_database

postgres_database=_postgres_database


def setup(factory,existing):
    with factory() as db:
        source=db.get(models.Job,existing);tenant=source.tenant_id
        project=models.Project(tenant_id=tenant,name='Disposable billing QA');db.add(project);db.flush();source.project_id=project.id
        legacy_output(db,tenant,source)
        canonical=source.payload['manifest']['source']['timeline']
        timeline=models.Timeline(tenant_id=tenant,project_id=project.id,name='Quote fixture',timeline_json=canonical);db.add(timeline)
        db.add(models.TenantBudget(tenant_id=tenant,currency='CNY',monthly_limit_micros=2_000_000,task_limit_micros=1_000_000))
        db.commit();return tenant,project.id,timeline.id


def test_parallel_project_ceiling_reservations_do_not_overspend(postgres_database):
    factory,existing=postgres_database;tenant,project,_=setup(factory,existing)
    with factory() as db:update_limit(db,tenant,project,'owner',ProjectLimitUpdate(base_revision=0,currency='CNY',limit_micros=600_000));db.commit()
    barrier=Barrier(2)
    def submit(_):
        with factory() as db:
            job=models.Job(tenant_id=tenant,project_id=project,type='fixture',payload={});db.add(job);db.flush()
            barrier.wait(timeout=10);decision=preflight_job_budget(db,job,selected_capability=snapshot());db.commit();return decision.allowed
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,range(2)))
    assert sorted(results)==[False,True]
    with factory() as db:
        assert db.scalar(select(func.sum(models.UsageLedger.amount_micros)).where(models.UsageLedger.status=='reserved'))==500_000


def test_parallel_quote_consumption_refreshes_stale_identity_map(postgres_database,monkeypatch):
    factory,existing=postgres_database;tenant,project,timeline_id=setup(factory,existing)
    selected=snapshot();selected.update(id='render:video_editing:1',capability='video_editing')
    monkeypatch.setattr(render_quotes,'require_render_support',lambda *args,**kw:deepcopy(selected))
    monkeypatch.setattr(render_quotes,'get_settings',lambda:Settings(auth_mode='session',auth_public_origin='http://fixture.invalid'))
    with factory() as db:
        quote=render_quotes.create_quote(db,db.get(models.Timeline,timeline_id),tenant,schemas.RenderRequest(),'same-quote');db.commit();quote_id=quote.id
    barrier=Barrier(2)
    def consume(index):
        with factory() as db:
            cached=db.get(models.RenderQuote,quote_id);assert cached.consumed_key is None
            timeline=db.get(models.Timeline,timeline_id);payload=schemas.RenderRequest(quote_id=quote_id)
            canonical,binding,_,_=render_quotes.quote_context(db,timeline,tenant,payload)
            barrier.wait(timeout=10)
            try:
                render_quotes.consume_quote(db,timeline,tenant,payload,f'job-intent-{index}',render_quotes.request_hash(timeline.id,binding,payload),selected,canonical.model_dump(mode='json'))
                db.commit();return 'consumed'
            except EditingError as error:db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(consume,range(2)))
    assert sorted(results)==['consumed','render_quote_expired']


def test_parallel_project_updates_use_revision_condition(postgres_database):
    factory,existing=postgres_database;tenant,project,_=setup(factory,existing);barrier=Barrier(2)
    def update(index):
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                update_limit(db,tenant,project,'owner',ProjectLimitUpdate(base_revision=0,currency='CNY',limit_micros=500_000+index))
                db.commit();return 'saved'
            except EditingError as error:db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(update,range(2)))
    assert sorted(results)==['project_budget_conflict','saved']
