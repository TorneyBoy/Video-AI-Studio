"""M2 prepared cases: metadata ownership, concurrent-edit fences and discovery.

These cases deliberately do not submit cloud work or use real media credentials.
"""
from copy import deepcopy

from sqlalchemy import select

from app import models
from app.database import get_db
from app.main import app


def _tenant(client, slug):
    return client.post('/api/v1/tenants', json={'slug': slug, 'name': slug}).json()['id']


def _asset(tenant_id, name='商品100%.mp4', project_id=None):
    factory = app.dependency_overrides[get_db]
    iterator = factory()
    db = next(iterator)
    try:
        evidence = {'storage': {'mode': 'external_presigned', 'status': 'ready', 'sha256': 'a' * 64,
                                'size_bytes': 100, 'attestation': {'signature': 'fixture-proof'}},
                    'analysis': {'provider': 'fixture', 'shots': [{'start': 0, 'end': 4}]},
                    'license': {'owner': 'original'}}
        asset = models.Asset(tenant_id=tenant_id, project_id=project_id, name=name, kind='video/mp4',
                             uri=f'oss://fixture/tenants/{tenant_id}/immutable', metadata_json=evidence)
        db.add(asset); db.commit()
        return asset.id, deepcopy(evidence)
    finally:
        iterator.close()


def test_user_labels_preserve_source_analysis_and_revision_conflict(client):
    tenant = _tenant(client, 'labels-a'); asset, evidence = _asset(tenant)
    headers = {'X-Tenant-ID': tenant}
    path = f'/api/v1/assets/{asset}/labels'
    first = client.patch(path, headers=headers, json={'base_revision': 0, 'name': '新展示名',
        'tags': [' 商品 ', 'SALE', 'sale', 'Straße'], 'note': '待补拍'})
    assert first.status_code == 200
    row = first.json()
    assert row['name'] == '新展示名'
    assert row['uri'] == f'oss://fixture/tenants/{tenant}/immutable'
    for field in ('storage', 'analysis', 'license'):
        assert row['metadata'][field] == evidence[field]
    assert row['metadata']['user_tags'] == ['商品', 'SALE', 'Straße']
    assert row['metadata']['user_labels_revision'] == 1
    conflict = client.patch(path, headers=headers, json={'base_revision': 0, 'tags': ['late']})
    assert conflict.status_code == 409 and conflict.json()['detail']['code'] == 'asset_labels_conflict'
    assert client.get(f'/api/v1/assets/{asset}', headers=headers).json()['metadata'] == row['metadata']
    forbidden = client.patch(path, headers=headers, json={'base_revision': 1, 'metadata': {'storage': {'status': 'ready'}}})
    assert forbidden.status_code == 422


def test_search_labels_literal_wildcards_and_tenant_isolation(client):
    tenant = _tenant(client, 'search-a'); other = _tenant(client, 'search-b')
    asset, _ = _asset(tenant); _asset(tenant, '商品普通.mp4'); private, _ = _asset(other)
    path = f'/api/v1/assets/{asset}/labels'; headers = {'X-Tenant-ID': tenant}
    assert client.patch(path, headers=headers, json={'base_revision': 0, 'tags': ['SALE', 'Straße']}).status_code == 200
    for query in ({'q': '%'}, {'tag': 'sale'}, {'tag': 'STRASSE', 'media_type': 'video'}):
        result = client.get('/api/v1/assets', params=query, headers=headers)
        assert result.status_code == 200 and [row['id'] for row in result.json()] == [asset]
    assert client.get('/api/v1/assets', params={'tag': 'sale', 'media_type': 'audio'}, headers=headers).json() == []
    assert client.patch(f'/api/v1/assets/{private}/labels', headers=headers,
                        json={'base_revision': 0, 'tags': ['leak']}).status_code == 404
    assert client.get('/api/v1/assets', params={'tag': 'sale'}, headers={'X-Tenant-ID': other}).json() == []


def test_pending_discovery_is_metadata_only_scoped_and_excludes_completed(client):
    tenant = _tenant(client, 'upload-list-a'); other = _tenant(client, 'upload-list-b')
    project = client.post('/api/v1/projects', json={'name': 'Own'}, headers={'X-Tenant-ID': tenant}).json()['id']
    asset, _ = _asset(tenant, project_id=project); second, _ = _asset(tenant, 'done.mp4', project)
    private, _ = _asset(other)
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        for owner, asset_id, status, hash_value in [(tenant, asset, 'unknown', 'a' * 64),
                    (tenant, second, 'completed', 'b' * 64), (other, private, 'unknown', 'a' * 64)]:
            db.add(models.StorageUploadIntent(tenant_id=owner, project_id=project if owner == tenant else None,
                asset_id=asset_id, idempotency_key=asset_id, request_hash='f' * 64, status=status,
                request_json={'sha256': hash_value, 'name': 'source.mp4', 'size_bytes': 100}))
        db.commit()
    finally:
        iterator.close()
    headers = {'X-Tenant-ID': tenant}
    pending = client.get('/api/v1/assets/upload-intents', params={'project_id': project}, headers=headers)
    assert pending.status_code == 200 and [row['asset_id'] for row in pending.json()] == [asset]
    assert not {'upload_url', 'upload_fields', 'upload_headers'} & pending.json()[0].keys()
    done = client.get('/api/v1/assets/upload-intents', headers=headers,
                      params={'sha256': 'b' * 64, 'include_completed': True}).json()
    assert [row['asset_id'] for row in done] == [second]
    assert client.get('/api/v1/assets/upload-intents', headers={'X-Tenant-ID': other},
                      params={'project_id': project}).status_code == 404
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        assert len(list(db.scalars(select(models.AssetLabel)))) == 0
    finally:
        iterator.close()
