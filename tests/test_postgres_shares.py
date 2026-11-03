"""Share receipts serialize on PostgreSQL; fixtures never execute media."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app import models
from app.config import Settings
from app.delivery_schemas import ShareCreate
from app.review_schemas import ReviewSubmit
from app.services.artifact_reviews import review_report, submit_review
from app.services.artifact_shares import create_share, revoke_share, share_read
from test_artifact_reviews import legacy_output
from test_postgres_completion import postgres_database as _postgres_database

postgres_database = _postgres_database


@pytest.mark.parametrize('same_request', [False, True])
def test_parallel_shares_keep_one_receipt_per_intent_and_revoke_monotonically(postgres_database, same_request):
    factory, existing = postgres_database
    settings = Settings(artifact_share_secret='disposable-postgres-metadata-fixture-secret', auth_public_origin='http://fixture.invalid')
    with factory() as db:
        job = db.get(models.Job, existing); tenant = job.tenant_id; artifact = legacy_output(db, tenant, job)
        state = review_report(db, tenant_id=tenant, artifact_id=artifact)
        approved = submit_review(db, tenant_id=tenant, artifact_id=artifact, actor_id='owner', key='fixture-approval',
            request=ReviewSubmit(binding_hash=state['binding_hash'], decision='approved'))
        request = ShareCreate(binding_hash=state['binding_hash'], review_id=approved.id)
        db.commit()
    barrier = Barrier(2)

    def create(index):
        with factory() as db:
            barrier.wait(timeout=10)
            row = create_share(db, tenant_id=tenant, artifact_id=artifact, actor_id='owner',
                key='same-intent' if same_request else f'intent-{index}', request=request, settings=settings)
            db.commit(); return row.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.ArtifactShare)) == (1 if same_request else 2)
        assert (ids[0] == ids[1]) == same_request
    barrier = Barrier(2)

    def revoke(_):
        with factory() as db:
            barrier.wait(timeout=10)
            row = revoke_share(db, tenant_id=tenant, artifact_id=artifact, actor_id='owner', share_id=ids[0])
            db.commit(); return row.revoked_at

    with ThreadPoolExecutor(max_workers=2) as pool:
        times = list(pool.map(revoke, range(2)))
    assert times[0] == times[1]
    with factory() as db:
        assert share_read(db, db.get(models.ArtifactShare, ids[0]), settings, include_link=True)['url'] is None
