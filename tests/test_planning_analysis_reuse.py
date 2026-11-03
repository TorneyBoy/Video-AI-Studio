from app.api import routers


def test_plan_reuses_upload_analysis_including_unknown_or_failed_original(client, monkeypatch):
    monkeypatch.setattr(routers.settings, 'asset_storage_mode', 'local')
    tenant = client.post('/api/v1/tenants', json={'slug': 'reuse-analysis', 'name': 'Reuse'}).json()['id']
    headers = {'X-Tenant-ID': tenant}
    project = client.post('/api/v1/projects', headers=headers, json={'name': 'Reusable'}).json()['id']
    template = client.post('/api/v1/templates', headers=headers, json={'project_id': project, 'name': 'Short', 'version': '1',
        'content': {'schema_version': '1.0', 'id': 'short', 'version': '1', 'duration': 6,
                    'slots': [{'name': 'hook', 'type': 'video', 'required': True}]}}).json()['id']
    asset = client.post('/api/v1/assets', headers=headers, json={'project_id': project, 'name': 'clip', 'kind': 'video/mp4',
        'uri': 'file:///fixture.mp4', 'metadata': {'duration': 4}}).json()['id']
    original = client.post(f'/api/v1/assets/{asset}/analyze', headers={**headers, 'Idempotency-Key': 'upload-analysis'},
                           json={'shot_analysis': True, 'transcribe': True}).json()['id']
    first = client.post('/api/v1/jobs/auto-edit', headers={**headers, 'Idempotency-Key': 'plan-one'},
        json={'project_id': project, 'template_id': template, 'asset_ids': [asset]})
    assert first.status_code == 201 and first.json()['payload']['analysis_job_ids'] == [original]
    assert client.patch(f'/api/v1/jobs/{original}/status', headers=headers, json={'status': 'failed', 'error': 'Unknown external outcome'}).status_code == 200
    second = client.post('/api/v1/jobs/auto-edit', headers={**headers, 'Idempotency-Key': 'plan-two'},
        json={'project_id': project, 'template_id': template, 'asset_ids': [asset]})
    assert second.status_code == 201 and second.json()['payload']['analysis_job_ids'] == [original]
    jobs = client.get('/api/v1/jobs', headers=headers).json()
    assert [job['id'] for job in jobs if job['type'] == 'asset.analyze'] == [original]
