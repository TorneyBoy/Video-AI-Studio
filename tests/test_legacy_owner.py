"""Legacy personal owner preparation, immutable scope and normal client login."""
import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select,func

from app import models
from app.main import app
from app.services.identity import hash_password,verify_password
from app.services.legacy_owner import (
    AdoptionError,AdoptionIntent,adoption_status,apply_adoption,plan_adoption,
)
from test_personal_identity import accounts as _accounts

accounts=_accounts
PASSWORD='Disposable legacy owner adoption fixture password'


@pytest.fixture(scope='module')
def owner_password_hash():return hash_password(PASSWORD)


@pytest.fixture
def legacy_space(accounts):
    client,factory,settings=accounts;settings.auth_registration_enabled=False
    with factory() as db:
        tenant=models.Tenant(slug='old-private-space',name='Original metadata workspace')
        other=models.Tenant(slug='unrelated-space',name='Other workspace')
        db.add_all([tenant,other]);db.flush()
        project=models.Project(tenant_id=tenant.id,name='Existing project');foreign=models.Project(tenant_id=other.id,name='Not granted')
        db.add_all([project,foreign]);db.flush()
        db.add(models.Asset(tenant_id=tenant.id,project_id=project.id,name='Existing asset',kind='video',uri='oss://fixture-only/original-object'))
        job=models.Job(tenant_id=tenant.id,project_id=project.id,type='video.generate',status='running',payload={'fixture_only':True});db.add(job);db.flush()
        db.add(models.ProviderOperation(tenant_id=tenant.id,job_id=job.id,request_id='original-operation',external_job_id='original-remote',state='unknown'))
        db.add(models.UsageLedger(tenant_id=tenant.id,job_id=job.id,status='pending_reconciliation',source='fixture-only',period_key='2026-10',currency='CNY',amount_micros=350000))
        db.add(models.TenantBudget(tenant_id=tenant.id,currency='CNY',monthly_limit_micros=1000000,task_limit_micros=500000,enabled=True))
        guard=models.DatabaseRecoveryState(id=1,state='quarantined');db.add(guard);db.commit()
        intent=AdoptionIntent.make(request_id=str(uuid4()),tenant_id=tenant.id,expected_slug=tenant.slug,database_instance_id=guard.instance_id,login_name='legacy_owner',display_name='个人工作台')
        ids={'tenant':tenant.id,'project':project.id,'foreign_project':foreign.id,'job':job.id}
    return client,factory,settings,intent,ids


BUSINESS=(models.Tenant,models.Project,models.Asset,models.Job,models.ProviderOperation,models.UsageLedger,models.TenantBudget,models.Artifact)


def business(db):
    return {model.__tablename__:[dict(row) for row in db.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns)).mappings()] for model in BUSINESS}


def test_operator_adoption_preserves_data_and_enables_normal_web_and_mini_accounts(legacy_space,owner_password_hash):
    client,factory,settings,intent,ids=legacy_space
    with factory() as db:
        original=business(db);plan=plan_adoption(db,tenant_id=intent.tenant_id,expected_slug=intent.expected_slug,login_name=intent.login_name)
        assert plan['database_instance_id']==intent.database_instance_id and plan['can_prepare_owner']
        result=apply_adoption(db,intent,password_hash=owner_password_hash);db.commit()
        assert business(db)==original
        assert db.scalar(select(func.count()).select_from(models.UserSession))==0
        assert db.scalar(select(func.count()).select_from(models.User))==1
        user=db.get(models.User,result['result']['user_id']);assert user.personal_tenant_id==ids['tenant'] and verify_password(PASSWORD,user.password_hash)
        assert PASSWORD not in json.dumps(result) and owner_password_hash not in json.dumps(result)
    assert client.post('/api/v1/auth/login',json={'login_name':intent.login_name,'password':PASSWORD,'client':'web'}).status_code==503
    # Test-only activation simulation; the production tool never does this.
    with factory() as db:db.get(models.DatabaseRecoveryState,1).state='active';db.commit()
    response=client.post('/api/v1/auth/login',json={'login_name':intent.login_name,'password':PASSWORD,'client':'web'})
    assert response.status_code==200 and 'access_token' not in response.json()
    assert client.get('/api/v1/session').json()['tenant_id']==ids['tenant']
    assert [row['id'] for row in client.get('/api/v1/projects').json()]==[ids['project']]
    assert client.get('/api/v1/projects/'+ids['foreign_project']).status_code==404
    with TestClient(app) as mini:
        response=mini.post('/api/v1/auth/login',json={'login_name':intent.login_name,'password':PASSWORD,'client':'mini'})
        assert response.status_code==200
        headers={'Authorization':'Bearer '+response.json()['access_token']}
        assert mini.get('/api/v1/session',headers=headers).json()['tenant_id']==ids['tenant']
        assert mini.get('/api/v1/projects/'+ids['project'],headers=headers).status_code==200
        assert mini.get('/api/v1/projects/'+ids['foreign_project'],headers=headers).status_code==404
    with factory() as db:assert business(db)==original


def test_same_request_replays_without_password_reset_or_new_session(legacy_space,owner_password_hash):
    _,factory,_,intent,_=legacy_space
    with factory() as db:
        first=apply_adoption(db,intent,password_hash=owner_password_hash);db.commit()
        original_hash=db.get(models.User,first['result']['user_id']).password_hash
        again=apply_adoption(db,intent);db.commit()
        assert again['result']==first['result'] and again['disposition']=='replayed' and not again['credential_changed']
        assert db.get(models.User,first['result']['user_id']).password_hash==original_hash
        assert adoption_status(db,intent.request_id)['found'] and db.scalar(select(func.count()).select_from(models.UserSession))==0


@pytest.mark.parametrize('change',['instance','slug','request_intent','owned','member','active','login_taken'])
def test_wrong_scope_and_existing_identity_cannot_be_silently_claimed(legacy_space,owner_password_hash,change):
    _,factory,_,intent,_=legacy_space;changes={}
    with factory() as db:
        if change=='instance':changes['database_instance_id']=str(uuid4())
        elif change=='slug':changes['expected_slug']='wrong-workspace'
        elif change=='active':db.get(models.DatabaseRecoveryState,1).state='active';db.commit()
        elif change=='request_intent':
            apply_adoption(db,intent,password_hash=owner_password_hash);db.commit();changes['display_name']='Different intent'
        else:
            other=models.Tenant(slug='already-personal',name='Existing');db.add(other);db.flush()
            user=models.User(login_name='legacy_owner' if change=='login_taken' else 'existing_user',display_name='Existing',personal_tenant_id=intent.tenant_id if change=='owned' else other.id)
            db.add(user);db.flush()
            if change=='member':db.add(models.Membership(user_id=user.id,tenant_id=intent.tenant_id,role='editor',status='disabled'))
            db.commit()
        blocked=AdoptionIntent.make(**{**intent.__dict__,**changes});original=business(db)
        previous=(db.scalar(select(func.count()).select_from(models.User)),db.scalar(select(func.count()).select_from(models.Membership)))
        with pytest.raises(AdoptionError):apply_adoption(db,blocked,password_hash=owner_password_hash)
        db.rollback()
        assert business(db)==original
        assert previous==(db.scalar(select(func.count()).select_from(models.User)),db.scalar(select(func.count()).select_from(models.Membership)))


def test_partial_adoption_rolls_back_all_identity_writes(legacy_space,owner_password_hash):
    _,factory,_,intent,_=legacy_space
    with factory() as db:
        original=business(db);apply_adoption(db,intent,password_hash=owner_password_hash);db.rollback()
        assert business(db)==original
        for model in (models.User,models.Membership,models.LegacyTenantAdoption):assert db.scalar(select(func.count()).select_from(model))==0


def test_cli_generated_password_is_private_and_never_printed(tmp_path,capsys):
    path=Path(__file__).resolve().parents[1]/'scripts/adopt_legacy_tenant.py'
    spec=importlib.util.spec_from_file_location('legacy_owner_cli_fixture',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    directory=tmp_path/'private-owner'
    assert module.main(['make-password','--output-directory',str(directory)])==0
    value=(directory/'initial-password.txt').read_text().strip();assert len(value)>=32 and value not in capsys.readouterr().out
    if os.name!='nt':assert (directory/'initial-password.txt').stat().st_mode&0o077==0
    assert module.main(['make-password','--output-directory',str(directory)])==1
    assert (directory/'initial-password.txt').read_text().strip()==value
    (directory/'initial-password.txt').unlink()
