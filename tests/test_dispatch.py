"""Scheduling contracts; no real media, SDK, network or supplier fees."""
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from sqlalchemy import create_engine,func,select
from sqlalchemy.orm import sessionmaker
import pytest
from app import models
from app.config import Settings,get_settings
from app.database import Base
from app.services import dispatch
from app.services.completion import finish_job
from app.services.completion import execution_ticket,prepare_provider_call
from app.services.budgets import period_key,validate_dispatch_budget
from app.services.provider_callbacks import apply_callback
from app.services.providers import ModelSnapshot,ProviderCapability,ProviderResult,ProviderRouter,ProviderRequest,ProviderExecutionError
from app.services.worker import _claim_job_workflow,process_queued_provider_jobs
from app.services.workflows import create_workflow
from app.services import worker


@pytest.fixture
def queue(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path / "queue.db"}');Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    yield factory
    engine.dispose()


def tenant(db,name='Space'):
    value=models.Tenant(slug=uuid4().hex,name=name);db.add(value);db.flush();return value.id


def project(db,owner):
    row=models.Project(tenant_id=owner,name='Project');db.add(row);db.flush();return row.id


def job(db,owner,project_id=None,**changes):
    row=models.Job(tenant_id=owner,project_id=project_id,type='video.generate',status='queued',
        payload={'provider_request':{'capability':'video_generation','provider':'bounded','prompt':'fixture'}},**changes)
    db.add(row);db.flush();return row


def batch(db,owner,project_id):
    row=models.ProductionBatch(tenant_id=owner,project_id=project_id,name='Batch',idempotency_key=uuid4().hex,request_hash='0'*64,actor_id='fixture')
    db.add(row);db.flush();return row


def limits(**changes):
    return Settings(dispatch_global_limit=16,dispatch_tenant_limit=16,dispatch_project_limit=16,dispatch_batch_limit=16,**changes)


def take(factory,settings=None,*,complete=True,families=dispatch.FAMILIES):
    with factory() as db:
        pair=dispatch.claim_next(db,families=families,workflow_claim=_claim_job_workflow,settings=settings or limits())
        if pair is None:return None
        value=pair[0];out=(value.id,value.tenant_id,value.project_id,value.type)
        if complete:finish_job(db,value,'succeeded');db.commit()
        return out


def test_durable_tenant_and_project_turns_survive_new_sessions(queue):
    with queue() as db:
        a=tenant(db,'Batch heavy');b=tenant(db,'New customer');a1=project(db,a);a2=project(db,a)
        for i in range(20):job(db,a,a1,created_at=datetime(2020,1,1,tzinfo=timezone.utc)+timedelta(seconds=i))
        only=job(db,b);p2=job(db,a,a2);only_id,p2id=only.id,p2.id;db.commit()
    first=[take(queue) for _ in range(4)]
    assert {row[1] for row in first[:2]}=={a,b}
    assert only_id in [row[0] for row in first[:2]] and p2id in [row[0] for row in first[:4]]


def test_foreground_and_distinct_batches_rotate_instead_of_draining_old_batch(queue):
    with queue() as db:
        owner=tenant(db);p=project(db,owner);a=batch(db,owner,p);b=batch(db,owner,p)
        ids={}
        for group,count in ((a.id,20),(b.id,2),('',1)):
            for _ in range(count):
                row=job(db,owner,p);row.payload={**row.payload,**({'batch_id':group} if group else {})};ids[row.id]=group
        db.commit()
    assert {ids[take(queue)[0]] for _ in range(3)}=={a.id,b.id,''}


def test_analysis_backlog_cannot_take_every_turn_from_other_families(queue):
    with queue() as db:
        owner=tenant(db)
        for _ in range(20):row=job(db,owner);row.type='asset.analyze'
        row=job(db,owner);row.type='render.high';db.commit()
    assert {take(queue)[3] for _ in range(2)}=={'asset.analyze','render.high'}


def test_future_backoff_pending_dependencies_and_foreign_workflows_do_not_block_tail(queue):
    with queue() as db:
        owner=tenant(db);source=job(db,owner);source.type='asset.analyze';source.next_retry_at=datetime.now(timezone.utc)+timedelta(hours=1)
        for _ in range(20):
            row=job(db,owner);row.type='auto_edit.plan';row.payload={'analysis_job_ids':[source.id]}
        for _ in range(20):job(db,owner,next_retry_at=source.next_retry_at)
        other=job(db,owner);create_workflow(db,other,runtime='temporal')
        ready=job(db,owner);id=ready.id;db.commit()
    assert take(queue)[0]==id and take(queue) is None
    with queue() as db:
        assert db.get(models.Job,source.id).attempt_count==0
        assert all(row.attempt_count==0 for row in db.scalars(select(models.Job).where(models.Job.id!=id)))


@pytest.mark.parametrize('scope',['global','tenant','project','batch'])
def test_in_flight_caps_block_new_admission_without_spending_attempts(queue,scope):
    values={f'dispatch_{key}_limit':16 for key in ('global','tenant','project','batch')};values[f'dispatch_{scope}_limit']=1
    with queue() as db:
        owner=tenant(db);p=project(db,owner);group=batch(db,owner,p)
        rows=[job(db,owner,p) for _ in range(2)]
        for row in rows:row.payload={**row.payload,'batch_id':group.id}
        ids=[row.id for row in rows];db.commit()
    assert take(queue,Settings(**values),complete=False)
    assert take(queue,Settings(**values),complete=False) is None
    with queue() as db:
        assert sorted(db.get(models.Job,id).attempt_count for id in ids)==[0,1]
        assert db.scalar(select(func.count()).select_from(models.WorkflowRun))==1


def test_known_remote_poll_uses_its_existing_slot_even_over_new_lower_limit(queue):
    with queue() as db:
        owner=tenant(db);old=job(db,owner);old_id=old.id
        db.add(models.ProviderOperation(tenant_id=owner,job_id=old.id,request_id=old.id,state='pending',provider_key='bounded',capability='video_generation',external_job_id='remote'))
        stuck=job(db,owner);stuck.status='cancelled';db.add(models.ProviderOperation(tenant_id=owner,job_id=stuck.id,request_id=stuck.id,state='unknown'))
        new=job(db,owner);new_id=new.id;db.commit()
    setting=Settings(dispatch_global_limit=1)
    assert take(queue,setting,complete=False)[0]==old_id
    assert take(queue,setting,complete=False) is None
    with queue() as db:assert db.get(models.Job,new_id).attempt_count==0


def test_empty_legacy_remote_id_does_not_bypass_capacity_admission(queue):
    with queue() as db:
        owner=tenant(db);old=job(db,owner);old.status='cancelled'
        db.add(models.ProviderOperation(tenant_id=owner,job_id=old.id,request_id=old.id,state='unknown'))
        row=job(db,owner);row.payload={**row.payload,'provider_job_id':'  '};id=row.id;db.commit()
    assert take(queue,Settings(dispatch_global_limit=1)) is None
    with queue() as db:assert db.get(models.Job,id).attempt_count==0


def test_polling_and_new_work_alternate_and_poll_identity_remains_original(queue):
    with queue() as db:
        owner=tenant(db);poll=job(db,owner);poll_id=poll.id
        db.add(models.ProviderOperation(tenant_id=owner,job_id=poll.id,request_id=poll.id,state='pending',external_job_id='same-remote'))
        job(db,owner);db.commit()
    assert take(queue,complete=False)[0]==poll_id
    with queue() as db:
        row=db.get(models.Job,poll_id);row.status='queued';run=db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id==row.id));run.status='queued';run.worker_id=None;run.lease_expires_at=None;db.commit()
    assert take(queue)[0]!=poll_id
    with queue() as db:job(db,owner);db.commit()
    assert take(queue,complete=False)[0]==poll_id
    with queue() as db:
        assert db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id==poll_id)).external_job_id=='same-remote'


def test_terminal_remote_evidence_releases_capacity_without_refunding_unknown_bill(queue):
    with queue() as db:
        owner=tenant(db);old=job(db,owner);old.status='cancelled';old_id=old.id;new=job(db,owner);new_id=new.id
        db.add(models.ProviderOperation(tenant_id=owner,job_id=old.id,request_id=old.id,state='unknown',provider_key='bounded',capability='video_generation'))
        db.add(models.UsageLedger(tenant_id=owner,job_id=old.id,source='fixture',period_key='2026-10',status='pending_reconciliation',amount_micros=500_000,currency='USD'));db.commit()
    assert take(queue,Settings(dispatch_global_limit=1)) is None
    with queue() as db:
        apply_callback(db,job=db.get(models.Job,old_id),payload={'event_id':'remote-ended','request_id':old_id,'status':'failed','provider':'bounded'});db.commit()
        assert db.get(models.Job,old_id).status=='cancelled'
        assert db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id==old_id)).status=='pending_reconciliation'
    assert take(queue,Settings(dispatch_global_limit=1))[0]==new_id


class BoundedProvider:
    name='bounded'
    capabilities=(ProviderCapability(name='video_generation',input_types=('text',),output_types=('video',),commercial_allowed=True,limits={'max_concurrent':1}),)
    def __init__(self):self.calls=[]
    def execute(self,request):
        self.calls.append(request)
        return ProviderResult(artifact_uri='https://fixture.invalid/result.mp4',artifact_type='video.rendered',
            model=ModelSnapshot(provider=self.name,model_id='fixture',version='1'),cost={'amount':0,'currency':'USD'})


@pytest.mark.parametrize('unknown_identity',[False,True])
def test_worker_capacity_wait_keeps_retry_budget_workflow_and_reservation(queue,tmp_path,monkeypatch,unknown_identity):
    provider=BoundedProvider();router=ProviderRouter([provider]);monkeypatch.setattr(get_settings(),'storage_root',str(tmp_path/'artifacts'))
    with queue() as db:
        owner=tenant(db);remote=job(db,owner);remote.status='cancelled';remote_id=remote.id
        db.add(models.ProviderOperation(tenant_id=owner,job_id=remote.id,request_id=remote.id,provider_key=None if unknown_identity else 'bounded',capability=None if unknown_identity else 'video_generation',state='unknown' if unknown_identity else 'pending',external_job_id=None if unknown_identity else 'already-remote'))
        row=job(db,owner,max_attempts=1);id=row.id
        db.add(models.UsageLedger(tenant_id=owner,job_id=id,source='fixture',period_key='2026-10',status='reserved',amount_micros=500_000,currency='USD'));db.commit()
        result=process_queued_provider_jobs(db,limit=10,router=router)
        assert result==[{'job_id':id,'status':'capacity_wait'}] and not provider.calls
        db.refresh(row);run=db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id==id))
        assert row.status==run.status=='queued' and row.attempt_count==run.attempt_count==0
        assert row.execution_generation==1 and not row.failure_code and row.next_retry_at
        assert db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id==id)).status=='reserved'
        assert db.scalar(select(func.count()).select_from(models.ProviderOperation).where(models.ProviderOperation.job_id==id))==0
        assert db.scalar(select(models.WorkflowStep).where(models.WorkflowStep.workflow_run_id==run.id)).attempt_count==0
        apply_callback(db,job=db.get(models.Job,remote_id),payload={'event_id':'done','request_id':remote_id,'status':'failed','provider':'bounded','external_job_id':'already-remote'})
        row.next_retry_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
        result=process_queued_provider_jobs(db,router=router)
        assert result[0]['status']=='succeeded' and len(provider.calls)==1
        db.refresh(row);assert row.attempt_count==1 and row.execution_generation==2


def test_legacy_remote_identity_without_operation_still_limits_new_provider_calls(queue,tmp_path,monkeypatch):
    provider=BoundedProvider();router=ProviderRouter([provider]);monkeypatch.setattr(get_settings(),'storage_root',str(tmp_path/'artifacts'))
    with queue() as db:
        owner=tenant(db);old=job(db,owner);old.status='cancelled';old.payload={**old.payload,'provider_job_id':'legacy-remote'}
        row=job(db,owner);id=row.id;db.commit()
        assert process_queued_provider_jobs(db,router=router)==[{'job_id':id,'status':'capacity_wait'}] and not provider.calls
        apply_callback(db,job=old,payload={'event_id':'legacy-ended','request_id':old.id,'status':'failed','provider':'bounded','external_job_id':'legacy-remote'})
        row.next_retry_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
        assert process_queued_provider_jobs(db,router=router)[0]['status']=='succeeded' and len(provider.calls)==1


def test_dependency_reopened_after_admission_waits_without_burning_a_plan_attempt(queue,monkeypatch):
    with queue() as db:
        owner=tenant(db);p=project(db,owner);dependency=job(db,owner,p);dependency.status='succeeded';dependency_id=dependency.id
        row=job(db,owner,p,max_attempts=1);row.type='auto_edit.plan';row.payload={'template_snapshot':{'id':'fixture','version':'1','duration':4,'slots':[{'name':'hook','type':'video'}]},'analysis_job_ids':[dependency.id]};id=row.id;db.commit()
    original=worker._start_workflow_step
    def reopen(db,run):
        db.get(models.Job,dependency_id).status='running';db.commit();return original(db,run)
    monkeypatch.setattr(worker,'_start_workflow_step',reopen)
    with queue() as db:
        assert worker.process_queued_auto_edit_jobs(db)==[{'job_id':id,'status':'waiting_dependencies'}]
        assert db.get(models.Job,id).status=='queued' and db.get(models.Job,id).attempt_count==0
        assert db.scalar(select(func.count()).select_from(models.EditDocument))==0


def test_prior_actual_overrun_stops_the_next_reserved_item_before_supplier_dispatch(queue,tmp_path,monkeypatch):
    provider=BoundedProvider();monkeypatch.setattr(get_settings(),'storage_root',str(tmp_path/'artifacts'))
    with queue() as db:
        owner=tenant(db);old=job(db,owner);old.status='succeeded';row=job(db,owner);id=row.id
        db.add(models.TenantBudget(tenant_id=owner,currency='USD',monthly_limit_micros=1_000_000))
        for source,amount,status in ((old,800_000,'settled'),(row,500_000,'reserved')):
            db.add(models.UsageLedger(tenant_id=owner,job_id=source.id,source='fixture',period_key=period_key(),status=status,amount_micros=amount,currency='USD'))
        db.commit();result=process_queued_provider_jobs(db,router=ProviderRouter([provider]))
        assert result[0]['status']=='failed' and not provider.calls
        db.refresh(row);assert row.policy_blocked and row.failure_code=='budget_dispatch_exceeded'
        assert db.scalar(select(models.UsageLedger.status).where(models.UsageLedger.job_id==id))=='released'
        assert db.scalar(select(func.count()).select_from(models.ProviderOperation))==0


def test_original_remote_query_is_allowed_when_new_spending_is_blocked(queue):
    provider=BoundedProvider()
    with queue() as db:
        owner=tenant(db);row=job(db,owner);id=row.id
        db.add(models.TenantBudget(tenant_id=owner,currency='USD',monthly_limit_micros=0))
        db.add(models.UsageLedger(tenant_id=owner,job_id=id,source='fixture',period_key=period_key(),status='pending_reconciliation',amount_micros=500_000,currency='USD'))
        db.add(models.ProviderOperation(tenant_id=owner,job_id=id,request_id=id,provider_key='bounded',capability='video_generation',external_job_id='original',state='pending'));db.commit()
        row,run=dispatch.claim_next(db,families=('provider',),workflow_claim=_claim_job_workflow)
        request=prepare_provider_call(db,row,execution_ticket(row,run),ProviderRequest(request_id=id,capability='video_generation',provider='bounded',inputs={}),ProviderRouter([provider]))
        assert request.parameters['provider_job_id']=='original' and not provider.calls
        assert db.scalar(select(models.UsageLedger.status).where(models.UsageLedger.job_id==id))=='pending_reconciliation'


@pytest.mark.parametrize('blocked',[False,True])
def test_old_month_reservations_are_rechecked_and_moved_atomically_at_dispatch(queue,blocked):
    with queue() as db:
        owner=tenant(db);old=job(db,owner);current=job(db,owner)
        db.add(models.TenantBudget(tenant_id=owner,currency='USD',monthly_limit_micros=1_000_000))
        ledger=models.UsageLedger(tenant_id=owner,job_id=old.id,source='fixture',period_key='2020-01',status='reserved',amount_micros=500_000 if blocked else 400_000,currency='USD')
        db.add(ledger);db.add(models.UsageLedger(tenant_id=owner,job_id=current.id,source='fixture',period_key=period_key(),status='reserved',amount_micros=600_000,currency='USD'));db.commit()
        if blocked:
            with pytest.raises(ProviderExecutionError,match='current budget'):validate_dispatch_budget(db,old)
            db.rollback();db.refresh(ledger);assert ledger.period_key=='2020-01'
        else:
            validate_dispatch_budget(db,old);db.commit();assert ledger.period_key==period_key() and ledger.metadata_json['dispatch_period_rollover']['from']=='2020-01'
            validate_dispatch_budget(db,old);db.commit()
            evidence=list(db.scalars(select(models.UsageEvidence).where(models.UsageEvidence.job_id==old.id)))
            assert len(evidence)==1 and evidence[0].usage_json['from_period']=='2020-01' and evidence[0].amount_micros is None


@pytest.mark.parametrize('name',['dispatch_global_limit','dispatch_tenant_limit','dispatch_project_limit','dispatch_batch_limit'])
def test_invalid_dispatch_limits_are_configuration_errors(name):
    with pytest.raises(ValueError):Settings(**{name:0})
