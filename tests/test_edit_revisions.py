"""Authoring contracts execute locally without provider/media calls."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from app import models
from app.api import routers
from app.database import get_db
from app.main import app
from app.services.worker import process_queued_auto_edit_jobs
from app.services.worker import process_queued_render_jobs
from app.services.completion import store_artifact


@pytest.fixture
def edit_case(client, monkeypatch):
    monkeypatch.setattr(routers.settings, 'asset_storage_mode', 'local')
    tenant = client.post('/api/v1/tenants', json={'slug': 'editor', 'name': 'Editor'}).json()['id']
    headers = {'X-Tenant-ID': tenant}
    project = client.post('/api/v1/projects', headers=headers, json={'name': 'Editing'}).json()['id']
    template = client.post('/api/v1/templates', headers=headers, json={'name': 'Pair', 'project_id': project,
        'version': '1', 'content': {'schema_version': '1.0', 'id': 'pair', 'version': '1', 'fps': 30, 'duration': 8,
            'slots': [{'name': 'hook', 'type': 'video', 'required': True}, {'name': 'body', 'type': 'video', 'required': True}]}}).json()['id']
    assets = []
    for index in range(4):
        asset = client.post('/api/v1/assets', headers=headers, json={'name': f'source-{index}', 'project_id': project,
            'kind': 'video/mp4', 'uri': f'file:///source-{index}.mp4', 'metadata': {'duration': 8,
                'analysis': {'shots': [{'start': 0, 'end': 4, 'duration': 4, 'quality': 1 - index * 0.1},
                                       {'start': 4, 'end': 8, 'duration': 4, 'quality': 0.4 - index * 0.1}]}}}).json()
        assets.append(asset)
    job = client.post('/api/v1/jobs/auto-edit', headers={**headers, 'Idempotency-Key': 'first-plan'},
        json={'project_id': project, 'template_id': template, 'asset_ids': [asset['id'] for asset in assets]}).json()
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        assert process_queued_auto_edit_jobs(db)[0]['status'] == 'succeeded'
    finally:
        iterator.close()
    revision = client.post(f"/api/v1/jobs/{job['id']}/editing", headers=headers)
    assert revision.status_code == 200, revision.text
    return {'client': client, 'headers': headers, 'project': project, 'template': template,
            'job': job, 'assets': assets, 'revision': revision.json(), 'tenant': tenant}


def save(case, base, key, operations, source_asset_ids=None):
    return case['client'].post(f"/api/v1/editing/{base['document_id']}/revisions",
        headers={**case['headers'], 'Idempotency-Key': key},
        json={'base_revision_id': base['revision_id'], 'operations': operations,
              'source_asset_ids': source_asset_ids or []})


def timeline(case, revision):
    result = case['client'].get(f"/api/v1/editing/{revision['document_id']}/revisions/{revision['revision_id']}/timeline",
                                headers=case['headers'])
    assert result.status_code == 200, result.text
    return result.json()['timeline']


def test_lock_partial_replan_uses_frozen_analysis_and_does_not_change_locked_clip(edit_case):
    case = edit_case; first = case['revision']; original = deepcopy(first)
    locked = save(case, first, 'lock', [{'op': 'set_lock', 'selection_id': first['plan']['selected'][0]['selection_id'], 'locked': True}]).json()
    old_timeline = timeline(case, locked)
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        asset = db.get(models.Asset, case['assets'][0]['id'])
        asset.metadata_json = {**asset.metadata_json, 'analysis': {'shots': [{'start': 100, 'end': 104, 'duration': 4, 'quality': 1}]}}
        db.commit()
    finally:
        iterator.close()
    replanned = save(case, locked, 'replan', [{'op': 'replan'}])
    assert replanned.status_code == 201, replanned.text
    newest = replanned.json(); after = timeline(case, newest)
    assert newest['plan']['selected'][0] == locked['plan']['selected'][0]
    assert after['tracks'][0]['clips'][0] == old_timeline['tracks'][0]['clips'][0]
    assert newest['plan']['selected'][1]['asset_id'] != locked['plan']['selected'][1]['asset_id']
    assert all(row['source_end'] <= 8 for row in newest['plan']['selected'])
    retrieved = case['client'].get(f"/api/v1/editing/{first['document_id']}/revisions/{first['revision_id']}", headers=case['headers']).json()
    for key in ('plan', 'template', 'plan_hash', 'timeline_hash'):
        assert retrieved[key] == original[key]


def test_stale_editor_conflicts_and_unknown_ack_replay_does_not_move_head_back(edit_case):
    case = edit_case; first = case['revision']; selection = first['plan']['selected'][0]['selection_id']
    action = [{'op': 'set_lock', 'selection_id': selection, 'locked': True}]
    second = save(case, first, 'stable', action).json()
    conflict = save(case, first, 'other-page', action)
    assert conflict.status_code == 409 and conflict.json()['detail']['code'] == 'edit_revision_conflict'
    assert conflict.json()['detail']['details']['head_revision_id'] == second['revision_id']
    third = save(case, second, 'unlock', [{'op': 'set_lock', 'selection_id': selection, 'locked': False}]).json()
    replay = save(case, first, 'stable', action)
    assert replay.status_code == 201 and replay.json()['revision_id'] == second['revision_id']
    assert replay.json()['head_revision_id'] == third['revision_id']
    wrong = save(case, first, 'stable', [{'op': 'set_lock', 'selection_id': selection, 'locked': False}])
    assert wrong.status_code == 409 and wrong.json()['detail']['code'] == 'idempotency_input_conflict'
    current = case['client'].get(f"/api/v1/editing/{first['document_id']}", headers=case['headers']).json()
    assert current['revision_id'] == third['revision_id'] and current['number'] == 3
    receipt = case['client'].get(f"/api/v1/editing/{first['document_id']}/requests/stable", headers=case['headers'])
    assert receipt.status_code == 200 and receipt.json()['found'] is True
    assert receipt.json()['revision']['revision_id'] == second['revision_id']
    assert receipt.json()['revision']['head_revision_id'] == third['revision_id']
    assert case['client'].get(f"/api/v1/editing/{first['document_id']}/requests/not-submitted", headers=case['headers']).json() == {'found': False, 'revision': None}


def test_locked_replacement_rejected_and_restore_and_repeated_undo_create_new_snapshots(edit_case):
    case = edit_case; first = case['revision']; selection = first['plan']['selected'][0]['selection_id']
    locked = save(case, first, 'lock', [{'op': 'set_lock', 'selection_id': selection, 'locked': True}]).json()
    candidates = case['client'].get(f"/api/v1/editing/{first['document_id']}/revisions/{locked['revision_id']}/candidates",
        headers=case['headers'], params={'selection_id': selection}).json()['items']
    rejection = save(case, locked, 'replace-locked', [{'op': 'replace', 'selection_id': selection, 'candidate_id': candidates[-1]['candidate_id']}])
    assert rejection.status_code == 409 and rejection.json()['detail']['code'] == 'edit_selection_locked'
    undone = save(case, locked, 'undo-lock', [{'op': 'undo'}]).json()
    assert undone['number'] == 3 and undone['plan'] == first['plan']
    assert undone['parent_revision_id'] == locked['revision_id'] and undone['undo_revision_id'] is None
    no_more = save(case, undone, 'undo-again', [{'op': 'undo'}])
    assert no_more.status_code == 409 and no_more.json()['detail']['code'] == 'edit_nothing_to_undo'
    restored = save(case, undone, 'restore-locked', [{'op': 'restore', 'revision_id': locked['revision_id']}]).json()
    assert restored['number'] == 4 and restored['plan'] == locked['plan']
    assert restored['revision_id'] not in {first['revision_id'], locked['revision_id'], undone['revision_id']}
    before = timeline(case, locked); after = timeline(case, restored)
    assert before['id'] != after['id']
    before.pop('id'); after.pop('id'); assert before == after


def test_render_is_pinned_to_revision_not_head_or_mutable_timeline_and_key_cannot_change_version(edit_case):
    case = edit_case; first = case['revision']; body = {'quality': 'draft', 'max_attempts': 2, 'options': {'quality': 'studio'}}
    path = f"/api/v1/editing/{first['document_id']}/revisions/{first['revision_id']}/render"
    rendered = case['client'].post(path, headers={**case['headers'], 'Idempotency-Key': 'one-render'}, json=body)
    assert rendered.status_code == 201, rendered.text
    result = rendered.json(); job = result['job']; manifest = result['manifest']
    assert job['type'] == 'render.draft' and manifest['options']['quality'] == 'draft'
    assert job['payload']['timeline_revision_id'] == first['revision_id']
    assert job['payload']['timeline_hash'] == first['timeline_hash']
    repeated = case['client'].post(path, headers={**case['headers'], 'Idempotency-Key': 'one-render'},
                                   json={'quality': 'draft', 'options': {'quality': 'studio'}})
    assert repeated.status_code == 201 and repeated.json()['job']['id'] == job['id']
    assert repeated.json()['job']['max_attempts'] == 2
    second = save(case, first, 'lock', [{'op': 'set_lock', 'selection_id': first['plan']['selected'][0]['selection_id'], 'locked': True}]).json()
    wrong = case['client'].post(f"/api/v1/editing/{first['document_id']}/revisions/{second['revision_id']}/render",
        headers={**case['headers'], 'Idempotency-Key': 'one-render'}, json=body)
    assert wrong.status_code == 409
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        row = db.get(models.Timeline, first['timeline_id'])
        value = deepcopy(row.timeline_json); value['resolution'] = {'width': 500, 'height': 500}; row.timeline_json = value
        db.commit()
    finally:
        iterator.close()
    another = case['client'].post(path, headers={**case['headers'], 'Idempotency-Key': 'still-original'}, json=body)
    assert another.status_code == 201
    assert another.json()['manifest'] == manifest
    assert case['client'].delete(f"/api/v1/timelines/{first['timeline_id']}", headers=case['headers']).status_code == 409
    assert case['client'].delete(f"/api/v1/projects/{case['project']}", headers=case['headers']).status_code == 409


def test_foreign_revision_source_and_changed_object_fail_closed(edit_case):
    case = edit_case; first = case['revision']
    outsider = case['client'].post('/api/v1/tenants', json={'slug': 'outsider', 'name': 'Other'}).json()['id']
    for path in (f"/editing/{first['document_id']}", f"/editing/{first['document_id']}/revisions/{first['revision_id']}/candidates"):
        assert case['client'].get('/api/v1' + path, headers={'X-Tenant-ID': outsider}).status_code == 404
    foreign = save(case, first, 'foreign-source', [{'op': 'replan'}], ['missing-or-other-tenant'])
    assert foreign.status_code == 404
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        source_id = first['plan']['selected'][0]['source_asset_id']
        db.get(models.Asset, source_id).uri = 'file:///replaced.mp4'; db.commit()
    finally:
        iterator.close()
    reject = case['client'].post(f"/api/v1/editing/{first['document_id']}/revisions/{first['revision_id']}/render",
        headers={**case['headers'], 'Idempotency-Key': 'changed-source'}, json={'quality': 'draft'})
    assert reject.status_code == 409 and reject.json()['detail']['code'] == 'edit_source_changed'
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        assert list(db.scalars(select(models.Job).where(models.Job.type.like('render.%')))) == []
    finally:
        iterator.close()


def test_configure_is_revisioned_and_locked_duration_is_not_silently_changed(edit_case):
    case = edit_case; first = case['revision']
    changed = save(case, first, 'configure', [{'op': 'configure', 'target_duration': 4, 'aspect_ratio': '16:9'}])
    assert changed.status_code == 201, changed.text
    second = changed.json(); value = timeline(case, second)
    assert value['duration'] == 4 and value['resolution'] == {'width': 1920, 'height': 1080}
    assert sum(row['output_duration'] for row in second['plan']['selected']) == 4
    for clip in value['tracks'][0]['clips']:
        assert clip['source_end'] - clip['source_start'] == pytest.approx(clip['range']['end'] - clip['range']['start'])
    locked = save(case, second, 'lock', [{'op': 'set_lock', 'selection_id': second['plan']['selected'][0]['selection_id'], 'locked': True}]).json()
    rejected = save(case, locked, 'duration-after-lock', [{'op': 'configure', 'target_duration': 8, 'aspect_ratio': '9:16'}])
    assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'edit_locked_duration'


def test_queued_plan_pins_template_options_and_rejects_key_with_different_input(edit_case):
    case = edit_case
    body = {'project_id': case['project'], 'template_id': case['template'], 'asset_ids': [row['id'] for row in case['assets']],
            'target_duration': 4, 'aspect_ratio': '1:1'}
    headers = {**case['headers'], 'Idempotency-Key': 'configured-plan'}
    submitted = case['client'].post('/api/v1/jobs/auto-edit', headers=headers, json=body)
    assert submitted.status_code == 201
    job_id = submitted.json()['id']
    assert case['client'].post('/api/v1/jobs/auto-edit', headers=headers, json={**body, 'target_duration': 6}).status_code == 409
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        template = db.get(models.Template, case['template'])
        value = deepcopy(template.content); value['duration'] = 99; value['version'] = 'changed'; template.content = value
        db.commit()
        result = process_queued_auto_edit_jobs(db)
        assert result[0]['status'] == 'succeeded', result
    finally:
        iterator.close()
    editing = case['client'].post(f'/api/v1/jobs/{job_id}/editing', headers=case['headers']).json()
    assert editing['template']['version'] == '1' and editing['template']['duration'] == 4
    value = timeline(case, editing)
    assert value['duration'] == 4 and value['resolution'] == {'width': 1080, 'height': 1080}


def test_new_candidate_sources_do_not_change_selected_shots_and_cannot_override_old_source_snapshot(edit_case):
    case = edit_case; first = case['revision']
    added = case['client'].post('/api/v1/assets', headers=case['headers'], json={'name': 'New candidate',
        'project_id': case['project'], 'kind': 'video/mp4', 'uri': 'file:///new.mp4', 'metadata': {'duration': 4,
            'analysis': {'shots': [{'start': 0, 'end': 4, 'duration': 4, 'quality': 1}]}}}).json()
    revised = save(case, first, 'new-candidates', [{'op': 'refresh_candidates'}], [added['id']])
    assert revised.status_code == 201, revised.text
    second = revised.json()
    assert second['plan']['selected'] == first['plan']['selected']
    assert added['id'] in second['source_asset_ids'] and added['id'] not in first['source_asset_ids']
    options = case['client'].get(f"/api/v1/editing/{first['document_id']}/revisions/{second['revision_id']}/candidates",
        headers=case['headers'], params={'limit': 500}).json()['items']
    assert any(row['source_asset_id'] == added['id'] for row in options)
    original_options = case['client'].get(f"/api/v1/editing/{first['document_id']}/revisions/{first['revision_id']}/candidates",
        headers=case['headers'], params={'limit': 500}).json()['items']
    assert not any(row['source_asset_id'] == added['id'] for row in original_options)


def test_missing_required_slot_is_reviewable_but_rejected_before_render_allocation(edit_case):
    case = edit_case
    template = case['client'].post('/api/v1/templates', headers=case['headers'], json={'name': 'Needs cover',
        'project_id': case['project'], 'version': '1', 'content': {'id': 'cover', 'version': '1', 'duration': 4,
            'slots': [{'name': 'video', 'type': 'video', 'constraints': {'allowed_asset_types': ['video']}},
                      {'name': 'cover', 'type': 'image', 'constraints': {'allowed_asset_types': ['image']}}]}}).json()
    job = case['client'].post('/api/v1/jobs/auto-edit', headers={**case['headers'], 'Idempotency-Key': 'missing-cover'},
        json={'project_id': case['project'], 'template_id': template['id'], 'asset_ids': [case['assets'][0]['id']]}).json()
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        result = process_queued_auto_edit_jobs(db); assert result[0]['status'] == 'succeeded', result
    finally:
        iterator.close()
    revision = case['client'].post(f"/api/v1/jobs/{job['id']}/editing", headers=case['headers']).json()
    assert revision['plan']['missing_required_slots'] == ['cover']
    rejected = case['client'].post(f"/api/v1/editing/{revision['document_id']}/revisions/{revision['revision_id']}/render",
        headers={**case['headers'], 'Idempotency-Key': 'must-not-render'}, json={'quality': 'draft'})
    assert rejected.status_code == 422 and rejected.json()['detail']['code'] == 'edit_required_slots_missing'
    assert rejected.json()['detail']['details']['missing_slots'] == ['cover']


def test_snapshot_hash_corruption_is_not_treated_as_editable_or_renderable(edit_case):
    case = edit_case; first = case['revision']
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        stored = db.get(models.TimelineRevision, first['revision_id'])
        value = deepcopy(stored.timeline_json); value['resolution']['width'] = 999; stored.timeline_json = value
        db.commit()
    finally:
        iterator.close()
    response = case['client'].post(f"/api/v1/editing/{first['document_id']}/revisions/{first['revision_id']}/render",
        headers={**case['headers'], 'Idempotency-Key': 'corrupted'}, json={'quality': 'draft'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'edit_snapshot_changed'


def test_worker_rechecks_revision_before_any_provider_submission(edit_case):
    case = edit_case; first = case['revision']
    created = case['client'].post(f"/api/v1/editing/{first['document_id']}/revisions/{first['revision_id']}/render",
        headers={**case['headers'], 'Idempotency-Key': 'queue-then-change'}, json={'quality': 'draft'})
    assert created.status_code == 201
    job_id = created.json()['job']['id']

    class NoSubmission:
        def execute(self, request):
            pytest.fail('A mismatched version must be rejected before supplier execution')

    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        job = db.get(models.Job, job_id); job.payload = {**job.payload, 'timeline_hash': '0' * 64}; db.commit()
        result = process_queued_render_jobs(db, router=NoSubmission())
        assert result[0]['status'] == 'failed', result
        db.refresh(job)
        assert job.failure_code == 'render_revision_changed' and job.retryable is False
        assert not list(db.scalars(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job_id)))
        assert not list(db.scalars(select(models.Artifact).where(models.Artifact.job_id == job_id)))
    finally:
        iterator.close()


def test_output_receipt_uses_server_version_binding_instead_of_provider_claim(edit_case):
    case = edit_case; first = case['revision']
    rendered = case['client'].post(f"/api/v1/editing/{first['document_id']}/revisions/{first['revision_id']}/render",
        headers={**case['headers'], 'Idempotency-Key': 'receipt-binding'}, json={'quality': 'draft'}).json()
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        job = db.get(models.Job, rendered['job']['id'])
        artifact = store_artifact(db, job, kind='video.rendered', uri='https://fixture.invalid/result.mp4',
            metadata={'timeline_revision_id': 'untrusted-provider-value', 'plan_hash': 'untrusted-provider-hash'})
        assert artifact.metadata_json['timeline_revision_id'] == first['revision_id']
        assert artifact.metadata_json['plan_hash'] == first['plan_hash']
        assert artifact.metadata_json['input_manifest_hash'] == rendered['manifest']['content_hash']
        db.rollback()  # Only the receipt contract is under test; no real output exists.
    finally:
        iterator.close()


def test_nonfinite_edit_input_is_rejected_without_echo_or_head_change(edit_case):
    case = edit_case; first = case['revision']
    response = case['client'].post(f"/api/v1/editing/{first['document_id']}/revisions",
        headers={**case['headers'], 'Idempotency-Key': 'invalid-number', 'Content-Type': 'application/json'},
        content='{"base_revision_id":"' + first['revision_id'] + '","operations":[{"op":"configure","target_duration":NaN,"aspect_ratio":"1:1"}]}')
    assert response.status_code == 422, response.text
    assert all('input' not in item for item in response.json()['detail'])
    assert case['client'].get(f"/api/v1/editing/{first['document_id']}", headers=case['headers']).json()['revision_id'] == first['revision_id']
