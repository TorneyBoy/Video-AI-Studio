"""Operational evidence: committed state only, no vendor IO or private bodies."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import json
import logging
from threading import Event
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine,event,func,select
from sqlalchemy.orm import Session,sessionmaker
from app import models
from app.database import Base
from app.services import operations,runtime_logging as logs,worker_observation

PRIVATE='PRIVATE_TEST_BODY_BEARER_1234567890'


@pytest.fixture
def factory(tmp_path):
    engine=create_engine('sqlite:///'+(tmp_path/'observation.db').as_posix(),connect_args={'check_same_thread':False})
    # Exercise real SAVEPOINT rollback, including Python's legacy SQLite mode.
    @event.listens_for(engine,'connect')
    def connect(connection,record):connection.isolation_level=None
    @event.listens_for(engine,'begin')
    def begin(connection):connection.exec_driver_sql('BEGIN')
    Base.metadata.create_all(engine);logs.install_job_observation()
    yield sessionmaker(bind=engine,class_=Session,expire_on_commit=False)
    engine.dispose()


@contextmanager
def captured():
    rows=[]
    class Capture(logging.Handler):
        def emit(self,record):rows.append(json.loads(record.getMessage()))
    logs.configure_logging();handler=Capture();logs.logger.addHandler(handler)
    try:yield rows
    finally:logs.logger.removeHandler(handler)


def seed(factory):
    with factory() as db:
        tenant=models.Tenant(slug=uuid4().hex,name=PRIVATE);db.add(tenant);db.flush()
        project=models.Project(tenant_id=tenant.id,name=PRIVATE);db.add(project);db.flush()
        job=models.Job(tenant_id=tenant.id,project_id=project.id,type='video.generate',payload={'secret':PRIVATE,'provider_request':{'provider':'fixture','capability':'video_generation'}})
        db.add(job);db.commit();return tenant.id,project.id,job.id


def test_http_ids_are_server_owned_and_unknown_errors_do_not_echo_body_or_paths():
    application=FastAPI();application.add_middleware(logs.RequestObservation)
    @application.get('/probe/{opaque}')
    def broken(opaque:str):raise RuntimeError(PRIVATE+' https://fixture.invalid/file?signature='+PRIVATE)
    with captured() as events,TestClient(application) as client:
        def call(index):return client.get('/probe/'+PRIVATE+'?secret='+PRIVATE,headers={'X-Request-ID':PRIVATE,'Authorization':'Bearer '+PRIVATE})
        with ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(call,range(2)))
        assert all(response.status_code==500 and len(response.headers['X-Request-ID'])==32 for response in responses)
        assert responses[0].headers['X-Request-ID']!=responses[1].headers['X-Request-ID']
        for response in responses:
            assert response.json()['detail']['request_id']==response.headers['X-Request-ID']
            assert PRIVATE not in response.text and response.headers['Cache-Control']=='no-store'
        serialized=json.dumps(events);assert PRIVATE not in serialized and 'signature=' not in serialized
        assert all(row['route']=='/probe/{opaque}' and row['status']==500 for row in events if row['event']=='http_request')
        assert PRIVATE not in logs.http_measurements.prometheus()
    assert logs.request_id.get() is None


def test_job_origin_and_logs_only_survive_committed_outer_transactions(factory):
    tenant,project,id=seed(factory);origin=uuid4().hex
    with captured() as events,factory() as db:
        token=logs.request_id.set(origin)
        try:
            job=models.Job(tenant_id=tenant,project_id=project,type='render.high',payload={'signature':PRIVATE},error=PRIVATE)
            db.add(job);db.flush();identifier=job.id
            assert not [row for row in events if row['event']=='job_state']
            db.commit();assert db.get(models.JobTrace,identifier).request_id==origin
        finally:logs.request_id.reset(token)
        assert len([row for row in events if row['event']=='job_state'])==1
        assert events[-1]['origin_request_id']==origin and events[-1]['job_id']==identifier
        events.clear();job=db.get(models.Job,identifier)
        transaction=db.begin_nested();job.status='running';db.flush();transaction.rollback();db.commit()
        assert not events
        transaction=db.begin_nested();job.status='running';db.flush();transaction.commit();db.rollback()
        assert not events and db.get(models.Job,identifier).status=='queued'
        db.get(models.Job,identifier).status='running';db.commit()
        assert events[-1]['state']=='running' and events[-1]['origin_request_id']==origin
        assert PRIVATE not in json.dumps(events)
        token=logs.request_id.set(origin)
        try:
            rejected=models.Job(tenant_id=tenant,type='render.high');db.add(rejected);db.flush();rejected_id=rejected.id;db.rollback()
        finally:logs.request_id.reset(token)
        assert db.get(models.JobTrace,rejected_id) is None


def test_child_origin_inherits_only_within_same_project_and_log_failure_cannot_break_commit(factory):
    tenant,project,id=seed(factory);origin=uuid4().hex
    class Broken(logging.Handler):
        def emit(self,record):raise RuntimeError(PRIVATE)
    with factory() as db:
        db.add(models.JobTrace(job_id=id,request_id=origin));db.commit()
        other=models.Project(tenant_id=tenant,name='Other');db.add(other);db.flush()
        children=[models.Job(tenant_id=tenant,project_id=value,type='auto_edit.plan',payload={'parent_job_id':id}) for value in (project,other.id)]
        db.add_all(children);handler=Broken();logs.logger.addHandler(handler)
        try:db.commit()
        finally:logs.logger.removeHandler(handler)
        assert db.get(models.JobTrace,children[0].id).request_id==origin and db.get(models.JobTrace,children[1].id) is None


def test_due_age_remote_and_billing_alerts_are_separate_and_projections_are_private(factory):
    tenant,project,id=seed(factory);now=datetime.now(timezone.utc)
    with factory() as db:
        job=db.get(models.Job,id);job.created_at=now-timedelta(minutes=20)
        future=models.Job(tenant_id=tenant,project_id=project,type='render.high',status='queued',attempt_count=2,next_retry_at=now+timedelta(hours=1),created_at=now-timedelta(days=10));db.add(future)
        parent=models.Job(tenant_id=tenant,project_id=project,type='batch.render',status='queued',created_at=now-timedelta(days=10));db.add(parent)
        unknown=models.Job(tenant_id=tenant,project_id=project,type='render.high',status='cancelled',error=PRIVATE,failure_code=PRIVATE,created_at=now-timedelta(days=10));db.add(unknown);db.flush()
        db.add(models.ProviderOperation(tenant_id=tenant,job_id=unknown.id,request_id=PRIVATE,provider_key=PRIVATE,external_job_id=PRIVATE,capability=PRIVATE,state='unknown',created_at=now-timedelta(minutes=20),result_json={'secret':PRIVATE}))
        db.add(models.WorkflowRun(tenant_id=tenant,job_id=unknown.id,workflow_type='fixture',status='running',lease_expires_at=now-timedelta(minutes=1),input_json={'secret':PRIVATE}))
        db.add(models.UsageLedger(tenant_id=tenant,job_id=unknown.id,period_key='2026-10',source='fixture',status='pending_reconciliation',currency='CNY',amount_micros=200000,cost_json={'secret':PRIVATE},updated_at=now-timedelta(hours=2)))
        db.add(models.UsageEvidence(tenant_id=tenant,job_id=unknown.id,evidence_hash='b'*64,source='fixture',disposition='adjustment_pending',cost_json={'secret':PRIVATE}))
        for index in range(5):
            failed=models.Job(tenant_id=tenant,project_id=project,type='render.high',status='failed');db.add(failed);db.flush()
            db.add(models.ProviderOperation(tenant_id=tenant,job_id=failed.id,request_id=PRIVATE+str(index),provider_key=PRIVATE,state='result_received'))
        db.commit();value=operations.operational_snapshot(db,now=now+timedelta(seconds=1))
        assert value['queue']['due_count']==1 and value['queue']['oldest_unsubmitted_seconds']>=1200
        assert value['remote']['unknown_count']==1 and 1200<=value['remote']['oldest_unknown_seconds']<1202
        assert value['billing']['pending_count']==1 and value['billing']['unresolved_evidence_count']==1
        codes={row['code'] for row in value['alerts']}
        assert {'queue_aging','workers_unobserved','workflow_lease_expired','remote_outcome_unknown','billing_reconciliation_aging','billing_adjustment_pending','provider_failure_ratio'}<=codes
        assert value['providers'][0]['failed']==5 and value['providers'][0]['unclassified']==1
        diagnostic=operations.job_diagnostic(db,unknown.id)
        serialized=json.dumps({'snapshot':value,'job':diagnostic},default=str)
        assert PRIVATE not in serialized and 'cost_json' not in serialized and 'payload' not in serialized
        metrics=operations.prometheus_operations(value)
        assert all(secret not in metrics for secret in (PRIVATE,tenant,project,id,unknown.id))
        assert diagnostic['ledger'][0]['amount_micros']=='200000' and diagnostic['job']['failure_code']=='other'


def test_alert_transitions_are_deduplicated_recoverable_and_do_not_change_jobs_or_money(factory):
    tenant,project,id=seed(factory);now=datetime.now(timezone.utc)
    with factory() as db:
        before=db.get(models.Job,id).status
        first,events=operations.evaluate_alerts(db,now=now);db.commit();assert len(events)==1 and events[0]['code']=='workers_unobserved'
        _,again=operations.evaluate_alerts(db,now=now+timedelta(seconds=1));db.commit();assert not again
        db.add(models.WorkerPulse(id=str(uuid4()),worker_tag='fixture',last_seen_at=now));db.commit()
        _,resolved=operations.evaluate_alerts(db,now=now+timedelta(seconds=2));db.commit();assert resolved[0]['event']=='operational_alert_recovered'
        db.execute(models.WorkerPulse.__table__.update().values(stopped_at=now));db.commit()
        _,repeat=operations.evaluate_alerts(db,now=now+timedelta(seconds=3));db.commit();assert repeat[0]['count']==2
        assert db.get(models.Job,id).status==before and db.scalar(select(func.count()).select_from(models.UsageLedger))==0


def test_duplicate_counter_is_transactional_and_actual_callback_replays_are_counted(factory):
    from app.services.provider_callbacks import apply_callback,ProviderCallbackError
    tenant,project,id=seed(factory)
    with factory() as db:
        operations.increment_counter(db,'callback_duplicate');db.rollback();assert db.get(models.RuntimeCounter,'callback_duplicate') is None
        payload={'event_id':'fixture-event','provider':'fixture','request_id':id,'status':'failed','error':'Fixture failure'}
        apply_callback(db,job=db.get(models.Job,id),payload=payload);db.commit()
        for _ in range(2):
            assert apply_callback(db,job=db.get(models.Job,id),payload=payload)[1];db.commit()
        with pytest.raises(ProviderCallbackError):apply_callback(db,job=db.get(models.Job,id),payload={**payload,'error':'Different'})
        db.rollback();assert db.get(models.RuntimeCounter,'callback_duplicate').value==2
        assert db.scalar(select(func.count()).select_from(models.ProviderEvent))==1


def test_independent_worker_pulse_continues_and_graceful_exit_is_observed(factory,monkeypatch):
    seen=Event();original=worker_observation.pulse
    def tracked(*args):original(*args);seen.set()
    monkeypatch.setattr(worker_observation,'pulse',tracked)
    with worker_observation.observed_worker(factory,'fixture-worker',interval=.01) as id:
        assert seen.wait(timeout=2)
        with factory() as db:
            row=db.get(models.WorkerPulse,id);assert row.stopped_at is None and row.last_seen_at>=row.started_at
    with factory() as db:assert db.get(models.WorkerPulse,id).stopped_at is not None


def test_internal_routes_and_global_metrics_require_operator_secret_in_session_mode(client,monkeypatch):
    from app.config import get_settings
    from pydantic import SecretStr
    settings=get_settings();monkeypatch.setattr(settings,'auth_mode','session');monkeypatch.setattr(settings,'internal_api_secret','fixture-operator-key')
    monkeypatch.setattr(settings,'metrics_api_secret',SecretStr('fixture-metrics-readonly'))
    for path in ('/metrics','/internal/operations','/internal/operations/jobs/missing'):
        assert client.get(path).status_code==403
        assert client.get(path,headers={'X-Internal-API-Key':'wrong','X-Tenant-ID':'forged'}).status_code==403
    response=client.get('/internal/operations',headers={'X-Internal-API-Key':'fixture-operator-key'})
    assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    assert client.get('/metrics',headers={'X-Metrics-Key':'fixture-metrics-readonly'}).status_code==200
    assert client.get('/metrics',headers={'X-Internal-API-Key':'fixture-operator-key'}).status_code==403
    assert client.get('/internal/operations',headers={'X-Internal-API-Key':'fixture-metrics-readonly','X-Metrics-Key':'fixture-metrics-readonly'}).status_code==403


def test_api_threadpool_creations_retain_the_server_request_origin(client):
    tenant=client.post('/api/v1/tenants',json={'slug':'trace-origin','name':'Trace fixture'}).json()['id']
    response=client.post('/api/v1/jobs',headers={'X-Tenant-ID':tenant,'X-Request-ID':PRIVATE},json={'type':'render.high'})
    assert response.status_code==201,response.text
    identifier=response.json()['id'];origin=response.headers['X-Request-ID']
    diagnostic=client.get('/internal/operations/jobs/'+identifier)
    assert diagnostic.status_code==200 and diagnostic.json()['job']['origin_request_id']==origin


def test_worker_cli_does_not_print_raw_processor_results(factory,monkeypatch,capsys):
    from app import cli
    monkeypatch.setattr(cli,'SessionLocal',factory);monkeypatch.setattr(cli,'init_db',lambda:None)
    monkeypatch.setattr(cli,'process_queued_jobs',lambda *args,**kw:[{'job_id':'fixture','error':PRIVATE,'output_uri':'https://fixture.invalid/'+PRIVATE}])
    monkeypatch.setattr('sys.argv',['video-ai','process-jobs'])
    with captured() as events:cli.main()
    assert any(row['event']=='worker_cycle' and row['count']==1 for row in events)
    assert PRIVATE not in json.dumps(events)
    assert PRIVATE not in capsys.readouterr().out


def test_config_validation_does_not_include_other_credentials_in_exception_text():
    from app.config import Settings
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as caught:Settings(provider_mode=PRIVATE,internal_api_secret=PRIVATE,storage_gateway_api_key=PRIVATE)
    assert PRIVATE not in str(caught.value)
