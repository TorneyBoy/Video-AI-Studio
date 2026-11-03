"""Real row locks for fixed-output reviews and package creation; no cloud IO."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from sqlalchemy import func,select
from app import models
from app.database import Base
from app.batch_schemas import BatchReviewRequest,BatchDeliverySelection,BatchPackageCreate
from app.review_schemas import ReviewSubmit
from app.services import batches,batch_runs,batch_delivery
from app.services.artifact_reviews import submit_review
from app.services.editing_errors import EditingError
from test_batch_delivery import (
    delivery_case as _delivery_case,wave_case as _wave_case,prepared as _prepared,
    edit_case as _edit_case,mutate,review_body,
)
from test_postgres_completion import postgres_database as _postgres_database

delivery_case=_delivery_case
wave_case=_wave_case
prepared=_prepared
edit_case=_edit_case
postgres_database=_postgres_database


@pytest.fixture
def pg_delivery(delivery_case,postgres_database):
    """Move the actual plan/render/QC fixture into an isolated PostgreSQL schema."""
    factory,_=postgres_database;tables=[]
    def capture(db):
        for table in Base.metadata.sorted_tables:
            # PostgreSQL migration already seeds its singleton dispatch mutex.
            if table.name in {'dispatch_clock','database_recovery_state'}:continue
            rows=[deepcopy(dict(row)) for row in db.execute(select(table)).mappings()]
            if rows:tables.append((table,rows))
    mutate(delivery_case,capture)
    with factory() as db:
        for table,rows in tables:db.execute(table.insert(),rows)
        db.commit()
        batch,run=context(db,delivery_case)
        body=BatchReviewRequest.model_validate(review_body(batch_delivery.prepare_reviews(db,batch,run,[row['key'] for row in delivery_case['outputs']],'owner')))
    return factory,delivery_case,body


def context(db,case,lock=False):
    batch=batches.batch_for(db,case['tenant'],case['batch']['id'],lock=lock)
    return batch,batch_runs.run_for(db,batch,case['run'])


def race(function):
    barrier=Barrier(2)
    def call(index):
        barrier.wait(timeout=10)
        return function(index)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(call,index) for index in range(2)]
        return [future.result(timeout=30) for future in futures]


@pytest.mark.parametrize('same_key',[True,False])
def test_parallel_group_reviews_preserve_original_receipts_and_cas(pg_delivery,same_key):
    factory,case,body=pg_delivery
    def write(index):
        with factory() as db:
            batch,run=context(db,case,True)
            result=batch_delivery.apply_reviews(db,batch,run,'owner','same' if same_key else f'page-{index}',body)
            db.commit();return result
    results=race(write)
    if same_key:assert results[0]==results[1]
    else:assert sorted(sum(row['applied'] for row in result['items']) for result in results)==[0,2]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.ArtifactReview))==2
        assert db.scalar(select(func.count()).select_from(models.BatchReviewAction))==(1 if same_key else 2)


def test_single_review_and_reversed_group_selection_do_not_overwrite_each_other(pg_delivery):
    factory,case,body=pg_delivery;first=body.items[0]
    def write(index):
        with factory() as db:
            try:
                if index:
                    request=ReviewSubmit(binding_hash=first.review.binding_hash,base_review_id=None,decision='changes_requested',note='Competing fixture opinion')
                    result=submit_review(db,tenant_id=case['tenant'],artifact_id=first.artifact_id,actor_id='owner',key='single',request=request).id
                else:
                    batch,run=context(db,case,True)
                    result=batch_delivery.apply_reviews(db,batch,run,'owner','group',BatchReviewRequest(items=list(reversed(body.items))))
                db.commit();return result
            except EditingError as error:db.rollback();return error.code
    results=race(write)
    assert isinstance(results[0],dict)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.ArtifactReview))==2
        rows=list(db.scalars(select(models.ArtifactReview).where(models.ArtifactReview.artifact_id==first.artifact_id)))
        assert len(rows)==1 and rows[0].number==1


@pytest.mark.parametrize('same_key',[True,False])
def test_parallel_package_requests_do_not_duplicate_jobs_or_storage_work(pg_delivery,same_key):
    factory,case,body=pg_delivery
    with factory() as db:
        batch,run=context(db,case,True);batch_delivery.apply_reviews(db,batch,run,'owner','approval',body)
        selection=BatchDeliverySelection(keys=[row['key'] for row in case['batch']['items']])
        value,_,_=batch_delivery.delivery_preview(db,batch,run,selection)
        request=BatchPackageCreate(**selection.model_dump(),manifest_hash=value['manifest_hash']);db.commit()
        previous_jobs=db.scalar(select(func.count()).select_from(models.Job));previous_cost=db.scalar(select(func.count()).select_from(models.UsageLedger))
    def write(index):
        with factory() as db:
            batch,run=context(db,case,True)
            result=batch_delivery.create_package(db,batch,run,'owner','same' if same_key else f'device-{index}',request)
            db.commit();return result
    results=race(write)
    assert len({row['package_id'] for row in results})==1 and len({row['job_id'] for row in results})==1
    if same_key:assert results[0]==results[1]
    else:assert sorted(row['disposition'] for row in results)==['new','reused']
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.DeliveryPackage))==1
        assert db.scalar(select(func.count()).select_from(models.DeliveryPackageRequest))==(1 if same_key else 2)
        assert db.scalar(select(func.count()).select_from(models.Job))==previous_jobs+1
        assert db.scalar(select(func.count()).select_from(models.UsageLedger))==previous_cost
