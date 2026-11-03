"""Real PostgreSQL serializes human decisions on the exact artifact row."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app import models
from app.review_schemas import ReviewSubmit
from app.services.artifact_reviews import review_report, submit_review
from app.services.editing_errors import EditingError
from test_artifact_reviews import legacy_output
from test_postgres_completion import postgres_database as _postgres_database

postgres_database = _postgres_database


@pytest.mark.parametrize('same_request',[False,True])
def test_parallel_review_decisions_use_cas_and_identical_receipts_replay(postgres_database,same_request):
    factory, existing = postgres_database
    with factory() as db:
        job = db.get(models.Job,existing); tenant = job.tenant_id; artifact = legacy_output(db,tenant,job)
        state = review_report(db,tenant_id=tenant,artifact_id=artifact)
    request = ReviewSubmit(binding_hash=state['binding_hash'],decision='approved',note='Fixture only')
    barrier = Barrier(2)
    def write(index):
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                row = submit_review(db,tenant_id=tenant,artifact_id=artifact,actor_id='same-owner',
                    key='same' if same_request else f'page-{index}',request=request)
                db.commit(); return row.id
            except EditingError as error:
                db.rollback(); return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write,index) for index in range(2)]
        results = [future.result(timeout=25) for future in futures]
    if same_request:
        assert results[0] == results[1]
    else:
        assert results.count('review_revision_conflict') == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.ArtifactReview)) == 1
        current = review_report(db,tenant_id=tenant,artifact_id=artifact)
        assert current['can_download'] and current['latest']['number'] == 1
