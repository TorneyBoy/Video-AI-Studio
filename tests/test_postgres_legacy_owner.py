"""Actual PG owner races and read-only source execution tripwires; no cloud IO."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import sql
import pytest
from sqlalchemy import func,select,text
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import get_db
from app.migrations import migrate
from app.services import worker
from app.services.legacy_owner import AdoptionError,AdoptionIntent,adoption_status,apply_adoption
from app.services.recovery_guard import RecoveryQuarantined,database_state
from app.services.release_preflight import database_facts
from app.services.worker_observation import observed_worker
from app.services.workflows import recover_expired_workflows
from test_postgres_release_upgrade import legacy_pg as _legacy_pg,pg_recovery_lab as _pg_recovery_lab
from test_postgres_recovery import fingerprint
from test_legacy_owner import owner_password_hash as _owner_password_hash

legacy_pg=_legacy_pg
pg_recovery_lab=_pg_recovery_lab
owner_password_hash=_owner_password_hash


def intent_for(endpoint,*,key=None,login='legacy_pg_owner',tenant=None):
    with endpoint.connect() as db:
        instance=db.execute('SELECT instance_id FROM database_recovery_state WHERE id=1').fetchone()[0]
        row=db.execute('SELECT id,slug FROM tenants WHERE id=%s',(tenant,)).fetchone() if tenant else db.execute('SELECT id,slug FROM tenants ORDER BY id LIMIT 1').fetchone()
    return AdoptionIntent.make(request_id=key or str(uuid4()),tenant_id=row[0],expected_slug=row[1],database_instance_id=instance,login_name=login)


@pytest.fixture
def owner_pg(legacy_pg):
    source,engine,admin,client,reserve,captured=legacy_pg;migrate(engine)
    with source.connect() as db:db.execute("UPDATE database_recovery_state SET state='quarantined' WHERE id=1")
    with admin.connect(autocommit=True) as db:
        db.execute(sql.SQL("ALTER DATABASE {} SET video_ai.recovery_quarantine TO 'on'").format(sql.Identifier(source.parameters['dbname'])))
    engine.dispose()
    return source,engine,admin,sessionmaker(bind=engine,expire_on_commit=False)


def race(function):
    barrier=Barrier(2)
    def run(index):barrier.wait(timeout=10);return function(index)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,index) for index in range(2)]
        return [future.result(timeout=30) for future in futures]


@pytest.mark.parametrize('same_key',[True,False])
def test_parallel_adoptions_preserve_one_owner_and_all_legacy_business_rows(owner_pg,owner_password_hash,same_key):
    source,_,_,factory=owner_pg;before=fingerprint(source);first=intent_for(source)
    second=first if same_key else intent_for(source,login='another_pg_owner')
    def write(index):
        with factory() as db:
            try:
                value=apply_adoption(db,(first,second)[index],password_hash=owner_password_hash);db.commit();return value
            except AdoptionError as error:db.rollback();return error.code
    results=race(write)
    if same_key:
        assert sorted(value['disposition'] for value in results)==['created','replayed']
        assert results[0]['result']==results[1]['result']
    else:
        assert len([value for value in results if isinstance(value,dict)])==1
        assert 'legacy_tenant_already_owned' in results
    after=fingerprint(source)
    for name,value in before['tables'].items():
        if name not in {'public.users','public.memberships','public.legacy_tenant_adoptions'}:assert after['tables'][name]==value,name
    with factory() as db:
        for model in (models.User,models.Membership,models.LegacyTenantAdoption):assert db.scalar(select(func.count()).select_from(model))==1
        assert db.scalar(select(func.count()).select_from(models.UserSession))==0
        assert db.scalar(select(models.UsageLedger.amount_micros))==350000
        assert db.get(models.DatabaseRecoveryState,1).state=='quarantined'


def test_global_request_key_cannot_bind_a_different_space(owner_pg,owner_password_hash):
    source,_,_,factory=owner_pg;first=intent_for(source)
    with factory() as db:
        other=models.Tenant(slug='different-legacy',name='Different legacy');db.add(other);db.commit();other_id=other.id
    second=intent_for(source,key=first.request_id,tenant=other_id,login='different_owner')
    with factory() as db:apply_adoption(db,first,password_hash=owner_password_hash);db.commit()
    before=fingerprint(source)
    with factory() as db:
        with pytest.raises(AdoptionError,match='request_conflict'):apply_adoption(db,second,password_hash=owner_password_hash)
        db.rollback()
    assert fingerprint(source)==before


def test_historical_adoption_does_not_recreate_deleted_credentials(owner_pg,owner_password_hash):
    source,_,_,factory=owner_pg;intent=intent_for(source)
    with factory() as db:
        created=apply_adoption(db,intent,password_hash=owner_password_hash);db.commit();user=db.get(models.User,created['result']['user_id'])
        db.execute(models.Membership.__table__.delete().where(models.Membership.user_id==user.id));db.delete(user);db.commit()
        status=adoption_status(db,intent.request_id);assert status['found'] and not status['bound_user_still_exists']
        fresh=intent_for(source,login='replacement_owner')
        with pytest.raises(AdoptionError,match='prior_adoption'):apply_adoption(db,fresh,password_hash=owner_password_hash)
        db.rollback();assert db.scalar(select(func.count()).select_from(models.User))==0


def test_cli_commit_replay_skips_missing_password_and_keeps_original_data(owner_pg,tmp_path,owner_password_hash):
    source,_,_,factory=owner_pg;intent=intent_for(source);root=Path(__file__).resolve().parents[1]
    from sqlalchemy import URL
    params=source.parameters
    value=URL.create('postgresql+psycopg',username=params.get('user'),password=params.get('password'),host=params['host'],port=int(params['port']),database=params['dbname']).render_as_string(hide_password=False)
    env={**os.environ,'ADOPTION_TEST_DATABASE':value,'ADOPTION_TEST_PASSWORD':'Disposable CLI personal account fixture password'}
    args=[sys.executable,str(root/'scripts/adopt_legacy_tenant.py'),'apply','--database-env','ADOPTION_TEST_DATABASE',
        '--request-id',intent.request_id,'--tenant-id',intent.tenant_id,'--expected-slug',intent.expected_slug,
        '--database-instance-id',intent.database_instance_id,'--login-name',intent.login_name]
    before=fingerprint(source)
    first=subprocess.run([*args,'--password-env','ADOPTION_TEST_PASSWORD'],env=env,cwd=root,capture_output=True,text=True,encoding='utf-8',check=False,timeout=40)
    assert first.returncode==0
    assert json.loads(first.stdout)['disposition']=='created' and env['ADOPTION_TEST_PASSWORD'] not in first.stdout+first.stderr
    stable=fingerprint(source)
    second=subprocess.run([*args,'--password-file',str(tmp_path/'does-not-exist')],env=env,cwd=root,capture_output=True,text=True,encoding='utf-8',check=False,timeout=40)
    assert second.returncode==0 and json.loads(second.stdout)['disposition']=='replayed'
    assert not json.loads(second.stdout)['credential_changed'] and fingerprint(source)==stable
    for name,value in before['tables'].items():
        if name not in {'public.users','public.memberships','public.legacy_tenant_adoptions'}:assert stable['tables'][name]==value
    evidence_root=os.getenv('ADOPTION_EVIDENCE_DIR')
    if evidence_root:
        path=Path(evidence_root);path.mkdir(parents=True,exist_ok=True)
        (path/'cli-adoption-evidence.json').write_text(json.dumps({'first':json.loads(first.stdout),'replay':json.loads(second.stdout),
            'all_original_business_rows_preserved':True,'source_still_quarantined':True,'session_issued':False,'fixture_only':True,'provider_calls':0},ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')


def test_readonly_source_is_not_ready_or_executable_and_is_not_a_hard_fence(legacy_pg,monkeypatch,tmp_path):
    source,engine,admin,_,_,_=legacy_pg;migrate(engine)
    with admin.connect(autocommit=True) as db:
        db.execute(sql.SQL("ALTER DATABASE {} SET default_transaction_read_only TO 'on'").format(sql.Identifier(source.parameters['dbname'])))
    engine.dispose();factory=sessionmaker(bind=engine,expire_on_commit=False);before=fingerprint(source)
    with factory() as db:
        assert database_state(db)['state']=='fenced'
        assert worker.process_queued_jobs(db)==[] and recover_expired_workflows(db)==[]
        assert db.get(models.DatabaseRecoveryState,1).state=='active'
    with pytest.raises(RecoveryQuarantined):
        with observed_worker(factory,'readonly-test-worker'):raise AssertionError('worker started')
    facts=database_facts(source);assert facts['default_read_only'] and not facts['quarantined']
    api=importlib.import_module('app.main');previous=dict(api.app.dependency_overrides)
    monkeypatch.setattr(api,'init_db',lambda:None);monkeypatch.setattr(api,'SessionLocal',factory)
    monkeypatch.setattr(api.settings,'storage_root',str(tmp_path/'storage'))
    def override():
        with factory() as db:yield db
    api.app.dependency_overrides[get_db]=override
    try:
        with TestClient(api.app) as client:
            assert client.get('/healthz').status_code==200 and client.get('/readyz').status_code==503
            assert client.get('/api/v1/projects').status_code==503
    finally:api.app.dependency_overrides.clear();api.app.dependency_overrides.update(previous)
    assert fingerprint(source)==before
    # PostgreSQL permits an explicit override; this must never be sold as a
    # physical source fence or sufficient evidence to activate another copy.
    with factory() as db:
        db.execute(text('SET TRANSACTION READ WRITE'))
        assert database_state(db)['state']=='active'
