"""Independent transactions serialize counters and alert episodes on PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy import func,select
from app import models
from app.services.operations import increment_counter,evaluate_alerts
from app.services.runtime_logging import install_job_observation
from test_postgres_completion import postgres_database as _postgres_database
from test_runtime_observability import captured

postgres_database=_postgres_database


def test_parallel_duplicate_receipts_do_not_lose_observations(postgres_database):
    factory,_=postgres_database;barrier=Barrier(2)
    def run(index):
        barrier.wait(timeout=10)
        for _ in range(8):
            with factory() as db:increment_counter(db,'callback_duplicate');db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,index) for index in range(2)]
        for future in futures:future.result(timeout=25)
    with factory() as db:assert db.get(models.RuntimeCounter,'callback_duplicate').value==16


def test_parallel_alert_evaluators_create_one_episode(postgres_database):
    factory,id=postgres_database;barrier=Barrier(2)
    def run(index):
        with factory() as db:
            barrier.wait(timeout=10);snapshot,transitions=evaluate_alerts(db);db.commit();return transitions
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,index) for index in range(2)];results=[future.result(timeout=30) for future in futures]
    assert sorted(len(result) for result in results)==[0,1]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.OperationalAlert))==1
        assert db.get(models.OperationalAlert,'workers_unobserved').occurrence_count==1


def test_nested_rollback_never_emits_an_uncommitted_job_transition(postgres_database):
    factory,id=postgres_database;install_job_observation()
    with captured() as events,factory() as db:
        job=db.get(models.Job,id);nested=db.begin_nested();job.status='running';db.flush();nested.commit();db.rollback()
        assert not events and db.get(models.Job,id).status=='queued'
        nested=db.begin_nested();job=db.get(models.Job,id);job.status='running';db.flush();nested.rollback();db.commit()
        assert not events and db.get(models.Job,id).status=='queued'
        job=db.get(models.Job,id);job.status='running';db.flush();job.status='failed';db.commit()
        assert len(events)==1 and events[-1]['state']=='failed' and events[-1]['previous_state']=='queued'
