"""Prepared for the M2 gate; row locks must reject a stale parallel editor."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import select

from app import models, schemas
from app.services.asset_library import update_asset_labels
from app.services.storage_gateway import StorageGatewayError
from test_postgres_completion import postgres_database as _postgres_database

postgres_database = _postgres_database


def test_two_label_writers_cannot_overwrite_same_revision(postgres_database):
    factory, job_id = postgres_database
    with factory() as db:
        tenant = db.get(models.Job, job_id).tenant_id
        asset = models.Asset(tenant_id=tenant, name='Original', kind='video/mp4',
                             uri='oss://fixture/immutable', metadata_json={'analysis': {'source': 'fixed'}})
        db.add(asset); db.commit(); asset_id = asset.id
    barrier = Barrier(2)

    def write(label):
        with factory() as db:
            # Both sessions intentionally hydrate revision 0 before the race.
            db.get(models.Asset, asset_id)
            barrier.wait(timeout=10)
            try:
                update_asset_labels(db, tenant_id=tenant, asset_id=asset_id,
                                    changes=schemas.AssetLabelsUpdate(base_revision=0, tags=[label]))
                return label
            except StorageGatewayError as exc:
                db.rollback(); return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, label) for label in ('web', 'mini')]
        results = [future.result(timeout=20) for future in futures]
    assert results.count('asset_labels_conflict') == 1
    winner = next(result for result in results if result != 'asset_labels_conflict')
    with factory() as db:
        metadata = db.get(models.Asset, asset_id).metadata_json
        assert metadata['user_tags'] == [winner] and metadata['user_labels_revision'] == 1
        assert metadata['analysis'] == {'source': 'fixed'}
        assert list(db.scalars(select(models.AssetLabel.label))) == [winner]
