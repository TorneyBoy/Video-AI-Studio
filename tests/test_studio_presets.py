"""The two clients share presets and exact, scoped operation recovery."""
from app.database import get_db
from app.api import routers
from app.main import app
from app.models import Template


def _space(client, slug):
    tenant = client.post('/api/v1/tenants', json={'slug': slug, 'name': slug}).json()['id']
    headers = {'X-Tenant-ID': tenant}
    project = client.post('/api/v1/projects', headers=headers, json={'name': 'Studio'}).json()['id']
    return headers, project


def test_presets_are_repeatable_project_scoped_and_do_not_overwrite(client):
    headers, project = _space(client, 'studio-presets')
    path = f'/api/v1/projects/{project}/template-presets'
    first = client.post(path, headers=headers)
    assert first.status_code == 200, first.text
    rows = first.json()
    assert len(rows) == 3
    assert all(row['content']['metadata']['real_acceptance'] == 'pending_m2' for row in rows)
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        row = db.get(Template, rows[0]['id']); row.name = 'Personal name'; db.commit()
    finally:
        iterator.close()
    second = client.post(path, headers=headers).json()
    assert [row['id'] for row in second] == [row['id'] for row in rows]
    assert second[0]['name'] == 'Personal name'
    other_project = client.post('/api/v1/projects', headers=headers, json={'name': 'Another'}).json()['id']
    other = client.post(f'/api/v1/projects/{other_project}/template-presets', headers=headers).json()
    assert not set(row['id'] for row in rows) & set(row['id'] for row in other)
    outsider, _ = _space(client, 'studio-outsider')
    assert client.post(path, headers=outsider).status_code == 404


def test_job_recovery_filters_exact_key_type_project_and_tenant(client, monkeypatch):
    monkeypatch.setattr(routers.settings, 'asset_storage_mode', 'local')
    headers, project = _space(client, 'studio-jobs')
    template = client.post(f'/api/v1/projects/{project}/template-presets', headers=headers).json()[0]['id']
    asset = client.post('/api/v1/assets', headers=headers, json={'name': 'Fixture', 'project_id': project,
        'kind': 'video/mp4', 'uri': 'https://fixture.invalid/source.mp4'}).json()['id']
    body = {'project_id': project, 'template_id': template, 'asset_ids': [asset]}
    jobs = []
    for key in ['operation', 'operation-extra']:
        result = client.post('/api/v1/jobs/auto-edit', headers={**headers, 'Idempotency-Key': key}, json=body)
        assert result.status_code == 201, result.text
        jobs.append(result.json())
    query = f'/api/v1/jobs?project_id={project}&order=desc&job_type=auto_edit.plan'
    assert [row['id'] for row in client.get(query, headers=headers).json()] == [row['id'] for row in reversed(jobs)]
    exact = client.get(query + '&idempotency_key=operation', headers=headers).json()
    assert [row['id'] for row in exact] == [jobs[0]['id']]
    assert client.get(f'/api/v1/jobs?project_id={project}&job_type=render.high', headers=headers).json() == []
    outsider, other_project = _space(client, 'job-outsider')
    assert client.get('/api/v1/jobs?idempotency_key=operation', headers=outsider).json() == []
    assert client.get(f'/api/v1/jobs?project_id={other_project}&idempotency_key=operation', headers=headers).json() == []
