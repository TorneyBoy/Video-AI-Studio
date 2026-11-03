"""Real pg_dump/pg_restore in new, disposable databases; no provider calls.

Requires the explicit TEST_POSTGRES_URL and TEST_PG_CONTAINER test environment.
Every created database name is owned by this fixture and removed afterward.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import create_engine,select,text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import models
from app.config import get_settings
from app.database import Base,get_db
from app.migrations import migrate
from app.services import archive_worker,package_worker,worker
from app.services.completion import ExecutionSuperseded,accept_worker_result,execution_ticket,lock_execution
from app.services.database_recovery import backup_database,inspect_backup,restore_database
from app.services.jobs import claim_job
from app.services.pg_recovery import DatabaseEndpoint,PgClient,snapshot_state
from app.services.provider_callbacks import apply_callback
from app.services.recovery_bundle import RecoveryToolError
from app.services.recovery_guard import RecoveryQuarantined,database_state
from app.services.workflows import claim_workflow,recover_expired_workflows
from app.services.worker_observation import observed_worker
from test_batch_delivery import (
    delivery_case as _delivery_case,wave_case as _wave_case,prepared as _prepared,
    edit_case as _edit_case,mutate,approve,create_package,
)

delivery_case=_delivery_case
wave_case=_wave_case
prepared=_prepared
edit_case=_edit_case


@pytest.fixture
def pg_recovery_lab():
    raw=os.getenv('TEST_POSTGRES_URL');container=os.getenv('TEST_PG_CONTAINER')
    if not raw or not container:pytest.skip('explicit disposable PG16 database/container required')
    url=make_url(raw)
    if url.host not in {'127.0.0.1','localhost'} or 'test' not in (url.database or ''):
        pytest.fail('recovery drill only accepts an explicitly disposable local test database')
    base=DatabaseEndpoint.from_environment('TEST_POSTGRES_URL')
    admin=DatabaseEndpoint({**base.parameters,'dbname':'postgres'})
    client=PgClient(container=container,host='127.0.0.1',port=5432,timeout=90)
    owned=[];engines=[]
    def reserve():
        name='video_ai_restore_h04_'+uuid4().hex[:24];owned.append(name);return name
    def create():
        name=reserve()
        with admin.connect(autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
        endpoint=DatabaseEndpoint({**base.parameters,'dbname':name})
        engine=create_engine(url.set(database=name));engines.append(engine);migrate(engine)
        return endpoint,engine,sessionmaker(bind=engine,expire_on_commit=False)
    yield admin,client,reserve,create
    for engine in engines:engine.dispose()
    with admin.connect(autocommit=True) as db:
        for name in reversed(owned):
            if not re.fullmatch('video_ai_restore_h04_[a-f0-9]{24}',name):raise AssertionError('test cleanup scope changed')
            # Only exact, fresh UUID database names reserved by this fixture.
            db.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))


def fingerprint(endpoint):
    with endpoint.connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return snapshot_state(db)


def target_factory(admin,name):
    params=admin.parameters
    from sqlalchemy import URL
    url=URL.create('postgresql+psycopg',username=params.get('user'),password=params.get('password'),
        host=params.get('host'),port=int(params.get('port',5432)),database=name)
    engine=create_engine(url)
    return engine,sessionmaker(bind=engine,expire_on_commit=False)


def _evidence(name,value):
    root=os.getenv('RECOVERY_EVIDENCE_DIR')
    if root:
        path=Path(root);path.mkdir(exist_ok=True,parents=True)
        (path/name).write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')


def test_full_application_backup_restores_exact_metadata_and_blocks_execution(delivery_case,pg_recovery_lab,tmp_path,monkeypatch):
    case=delivery_case;approve(case)
    assert create_package(case).status_code==200
    admin,client,reserve,create=pg_recovery_lab;source,engine,factory=create()
    copied=[]
    def capture(db):
        for table in Base.metadata.sorted_tables:
            if table.name in {'dispatch_clock','database_recovery_state'}:continue
            rows=[deepcopy(dict(row)) for row in db.execute(select(table)).mappings()]
            if rows:copied.append((table,rows))
    mutate(case,capture)
    with factory() as db:
        for table,rows in copied:db.execute(table.insert(),rows)
        owner=models.User(login_name='restore_fixture',display_name='Recovery fixture',password_hash='NOT_A_REAL_PASSWORD_HASH',personal_tenant_id=case['tenant'])
        db.add(owner);db.flush()
        db.add(models.Membership(user_id=owner.id,tenant_id=case['tenant'],role='owner'))
        session=models.UserSession(user_id=owner.id,tenant_id=case['tenant'],access_token_hash='a'*64,
            access_expires_at=datetime.now(timezone.utc)+timedelta(hours=1),refresh_expires_at=datetime.now(timezone.utc)+timedelta(days=1),
            credential_epoch=0,client_type='web',login_method='password')
        db.add(session);db.flush();db.add(models.RefreshToken(session_id=session.id,token_hash='b'*64,expires_at=session.refresh_expires_at))
        job=models.Job(tenant_id=case['tenant'],project_id=case['project'],type='video.generate',status='running',execution_generation=7,payload={'explicit_fixture':True})
        db.add(job);db.flush();unknown=job.id
        run=models.WorkflowRun(tenant_id=case['tenant'],job_id=job.id,workflow_type='video.generate',status='running',worker_id='fixture-lost-worker',
            attempt_count=2,lease_expires_at=datetime.now(timezone.utc)-timedelta(minutes=2))
        db.add(run);db.flush()
        db.add(models.ProviderOperation(tenant_id=case['tenant'],job_id=job.id,request_id='fixture-request',provider_key='fixture-only',external_job_id='fixture-unknown-remote',state='unknown'))
        db.add(models.UsageLedger(tenant_id=case['tenant'],job_id=job.id,period_key='2026-10',status='pending_reconciliation',
            source='explicit_recovery_fixture',currency='CNY',amount_micros=350_000))
        db.add(models.JobTrace(job_id=job.id,request_id='f'*32))
        db.add(models.RuntimeCounter(key='callback_duplicate',value=9))
        db.add(models.WorkerPulse(id=str(uuid4()),worker_tag='stopped-fixture',last_seen_at=datetime.now(timezone.utc),stopped_at=datetime.now(timezone.utc)))
        db.add(models.OperationalAlert(code='remote_unknown',severity='warning',first_seen_at=datetime.now(timezone.utc),last_seen_at=datetime.now(timezone.utc)))
        db.commit();old_ticket=execution_ticket(job,run)
    with source.connect() as db:
        db.execute('CREATE TABLE recovery_fixture_sequence (id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, removed TEXT, marker TEXT)')
        db.execute('ALTER TABLE recovery_fixture_sequence DROP COLUMN removed')
        db.execute("INSERT INTO recovery_fixture_sequence(marker) VALUES ('metadata sequence fixture')")
        db.execute('SELECT nextval(pg_get_serial_sequence(\'recovery_fixture_sequence\',\'id\'))')
        db.execute('SELECT lo_from_bytea(0,%s)',(b'explicit tiny metadata LO fixture',))
        db.execute('SELECT lo_from_bytea(0,%s)',(b'',))
    initial=fingerprint(source);key=os.urandom(32)
    config=tmp_path/'fixture.env';config.write_text('FIXTURE_ONLY=PRIVATE_CONFIG_SENTINEL\n',encoding='utf-8')
    receipt=backup_database(source,client,key,output_directory=tmp_path/'backup',scratch_root=tmp_path,configs={'app-env':config})
    bundle=tmp_path/'backup'/'metadata.vab'
    assert fingerprint(source)==initial and b'PRIVATE_CONFIG_SENTINEL' not in bundle.read_bytes()
    assert inspect_backup(bundle,key,scratch_root=tmp_path)['backup_id']==receipt['backup_id']
    # An existing database, even one with a valid-looking name, is never replaced.
    with pytest.raises(RecoveryToolError,match='already_exists'):
        restore_database(bundle,key,admin,client,database=source.parameters['dbname'],output_directory=tmp_path/'refused',scratch_root=tmp_path)
    assert fingerprint(source)==initial
    name=reserve();restored=restore_database(bundle,key,admin,client,database=name,output_directory=tmp_path/'restored',scratch_root=tmp_path,export_configs=True)
    target=DatabaseEndpoint({**source.parameters,'dbname':name});actual=fingerprint(target)
    assert actual['catalog_sha256']==initial['catalog_sha256']
    assert actual['sequences']==initial['sequences'] and actual['large_objects']==initial['large_objects']
    assert {key:value for key,value in actual['tables'].items() if key!='public.database_recovery_state'}=={key:value for key,value in initial['tables'].items() if key!='public.database_recovery_state'}
    assert (tmp_path/'restored'/'recovered-config'/'app-env').read_bytes()==config.read_bytes()
    assert 'PRIVATE_CONFIG_SENTINEL' not in json.dumps(restored)
    target_engine,restored_factory=target_factory(admin,name)
    try:
        with restored_factory() as db:
            guard=db.get(models.DatabaseRecoveryState,1)
            assert guard.state=='quarantined' and guard.instance_id!=old_ticket.database_instance
            assert database_state(db)['state']=='quarantined'
            assert worker.process_queued_jobs(db)==[] and recover_expired_workflows(db)==[]
            assert archive_worker.process_archive_tasks(db,settings=get_settings())==[]
            assert package_worker.process_queued_package_jobs(db)==[] and package_worker.reconcile_package_operations(db)==[]
            job=db.get(models.Job,unknown);run=db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id==unknown))
            assert not claim_workflow(db,run,worker_id='forbidden')
            queued=db.scalar(select(models.Job).where(models.Job.status=='queued'))
            assert queued is not None and not claim_job(db,queued)
            assert not accept_worker_result(db,job,old_ticket)
            with pytest.raises(ExecutionSuperseded,match='database recovery fence'):lock_execution(db,job,old_ticket)
            with pytest.raises(RecoveryQuarantined):apply_callback(db,job=job,payload={'event_id':'forbidden'})
            db.commit()
        with pytest.raises(RecoveryQuarantined):
            with observed_worker(restored_factory,'forbidden-start'):raise AssertionError('worker started')
        import app.main as api
        previous=dict(api.app.dependency_overrides)
        def target_db():
            with restored_factory() as db:yield db
        monkeypatch.setattr(api,'init_db',lambda:None)
        monkeypatch.setattr(api,'SessionLocal',restored_factory)
        monkeypatch.setattr(get_settings(),'storage_root',str(tmp_path/'storage'))
        api.app.dependency_overrides[get_db]=target_db
        try:
            with TestClient(api.app) as browser:
                assert browser.get('/healthz').status_code==200
                assert browser.get('/readyz').status_code==503
                for method,path in [('get','/api/v1/projects'),('post','/api/v1/projects'),('get','/api/v1/session')]:
                    response=getattr(browser,method)(path)
                    assert response.status_code==503 and response.json()['detail']['code']=='database_recovery_quarantine',path
                assert browser.get('/metrics').status_code==200
                monkeypatch.setattr(api,'SessionLocal',factory)
                assert browser.get('/readyz').status_code==200
        finally:api.app.dependency_overrides.clear();api.app.dependency_overrides.update(previous)
        assert fingerprint(target)==actual and fingerprint(source)==initial
        # Test-only simulation: a future activation must still reject an old DB ticket.
        with target.connect(autocommit=True) as db:
            db.execute(sql.SQL("ALTER DATABASE {} RESET video_ai.recovery_quarantine").format(sql.Identifier(name)))
            db.execute("UPDATE database_recovery_state SET state='active' WHERE id=1")
        target_engine.dispose()
        with restored_factory() as db:
            assert database_state(db)['state']=='active'
            with pytest.raises(ExecutionSuperseded,match='database recovery fence'):
                lock_execution(db,db.get(models.Job,unknown),old_ticket)
            assert not accept_worker_result(db,db.get(models.Job,unknown),old_ticket)
        _evidence('roundtrip-evidence.json',{'backup':receipt,'restore':restored,
            'source_unchanged':True,'all_application_rows_verified':True,'sequences_and_large_objects_verified':True,
            'api_503_worker_callback_claims_blocked':True,'foreign_database_ticket_rejected':True,
            'pending_reconciliation_micros':350_000,'pending_reconciliation_currency':'CNY',
            'provider_calls':0,'fixture_only':True,'test_activation_simulation_only':True})
    finally:target_engine.dispose()


def test_exported_snapshot_excludes_concurrent_uuid_writes(pg_recovery_lab,tmp_path):
    admin,client,reserve,create=pg_recovery_lab;source,engine,factory=create()
    with factory() as db:db.add(models.Tenant(slug='before-snapshot',name='Before'));db.commit()
    class ConcurrentWriter(PgClient):
        def run(self,program,args,endpoint,**kwargs):
            if program=='pg_dump' and '--format=custom' in args:
                with factory() as db:db.add(models.Tenant(slug='after-snapshot',name='After'));db.commit()
            return super().run(program,args,endpoint,**kwargs)
    active=ConcurrentWriter(**vars(client));key=os.urandom(32)
    receipt=backup_database(source,active,key,output_directory=tmp_path/'backup',scratch_root=tmp_path)
    name=reserve();restore_database(tmp_path/'backup'/'metadata.vab',key,admin,client,database=name,output_directory=tmp_path/'restored',scratch_root=tmp_path)
    with source.connect() as db:assert db.execute("SELECT count(*) FROM tenants").fetchone()[0]==2
    with admin.connect(database=name) as db:
        assert db.execute('SELECT slug FROM tenants').fetchall()==[('before-snapshot',)]
    _evidence('snapshot-evidence.json',{'backup_id':receipt['backup_id'],'source_tenants':2,'restored_snapshot_tenants':1,'concurrent_snapshot_verified':True,'provider_calls':0})


def test_sequence_change_refuses_inconsistent_backup(pg_recovery_lab,tmp_path):
    _,client,_,create=pg_recovery_lab;source,_,_=create()
    with source.connect() as db:db.execute('CREATE SEQUENCE recovery_sequence_fixture')
    class SequenceWriter(PgClient):
        def run(self,program,args,endpoint,**kwargs):
            result=super().run(program,args,endpoint,**kwargs)
            if program=='pg_dump' and '--format=custom' in args:
                with source.connect() as db:db.execute("SELECT nextval('recovery_sequence_fixture')")
            return result
    with pytest.raises(RecoveryToolError,match='sequence_changed'):
        backup_database(source,SequenceWriter(**vars(client)),os.urandom(32),output_directory=tmp_path/'backup',scratch_root=tmp_path)
    assert not (tmp_path/'backup').exists() and not list(tmp_path.glob('video-ai-recovery-*'))


def test_failed_restore_and_later_migration_cannot_clear_quarantine(pg_recovery_lab,tmp_path):
    admin,client,reserve,create=pg_recovery_lab;source,engine,_=create()
    with engine.begin() as db:
        db.execute(text('DROP TABLE database_recovery_state'))
        db.execute(text("DELETE FROM schema_migrations WHERE version='0020_database_recovery_state'"))
    key=os.urandom(32);backup_database(source,client,key,output_directory=tmp_path/'backup',scratch_root=tmp_path)
    bundle=tmp_path/'backup'/'metadata.vab'
    class FailedRestore(PgClient):
        def run(self,program,args,endpoint,**kwargs):
            if program=='pg_restore' and '--single-transaction' in args:raise RecoveryToolError('pg_restore_failed')
            return super().run(program,args,endpoint,**kwargs)
    failed=reserve()
    with pytest.raises(RecoveryToolError,match='pg_restore_failed'):
        restore_database(bundle,key,admin,FailedRestore(**vars(client)),database=failed,output_directory=tmp_path/'failed',scratch_root=tmp_path)
    with admin.connect(database=failed) as db:
        assert db.execute("SELECT current_setting('video_ai.recovery_quarantine',true)").fetchone()[0]=='on'
        assert db.execute("SELECT to_regclass('public.jobs')").fetchone()[0] is None
    target=reserve();restore_database(bundle,key,admin,client,database=target,output_directory=tmp_path/'restored',scratch_root=tmp_path)
    target_engine,factory=target_factory(admin,target)
    try:
        with factory() as db:before=db.get(models.DatabaseRecoveryState,1).instance_id
        assert '0020_database_recovery_state' in migrate(target_engine)
        with factory() as db:
            guard=db.get(models.DatabaseRecoveryState,1)
            assert guard.instance_id==before and guard.state=='quarantined'
            assert database_state(db)['state']=='quarantined'
    finally:target_engine.dispose()
