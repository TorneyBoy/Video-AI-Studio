"""Synthetic metadata API contracts, not perceptual/media acceptance."""
from copy import deepcopy

import pytest

from app import models
from app.database import get_db
from app.main import app
from app.services.scenario_profiles import get_scenario_profile, scenario_binding


def write(callback):
    iterator = app.dependency_overrides[get_db]()
    db = next(iterator)
    try:
        result = callback(db)
        db.commit()
        return result
    finally:
        iterator.close()


@pytest.fixture
def case(client):
    tenant = client.post('/api/v1/tenants', json={'slug': 'quality-case', 'name': 'Quality Case'}).json()['id']
    headers = {'X-Tenant-ID': tenant}
    project = client.post('/api/v1/projects', headers=headers, json={'name': 'Quality'}).json()['id']
    def setup(db):
        artifact = models.Artifact(tenant_id=tenant, project_id=project, kind='video', uri='oss://fixture/output.mp4',
            metadata_json={'output_sha256': 'a' * 64, 'quality_control': {'has_audio': False}})
        db.add(artifact)
        db.flush()
        return artifact.id
    id = write(setup)
    body = {'artifact_id': id, 'output_sha256': 'a' * 64,
        'scenario': scenario_binding(get_scenario_profile('product_showcase')), 'rubric_version': 'pilot-1',
        'dimensions': [{'dimension': dimension, 'status': 'passed', 'score': 4.0, 'evidence_ids': [id]}
                       for dimension in ['facts', 'technical', 'narrative', 'visual', 'audio']]}
    return client, headers, project, id, body


def send(case, body=None, key='review'):
    client, headers, project, _, original = case
    return client.post(f'/api/v1/projects/{project}/quality/reviews', json=body or original,
        headers={**headers, 'Idempotency-Key': key})


def test_review_binds_actual_output_rules_identity_and_original_receipt(case):
    response = send(case)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['assessment']['verdict'] == 'passed'
    assert result['assessment']['commercial_or_release_admission'] is False
    assert result['review']['reviewer_id'].startswith('reviewer-')
    assert send(case).json()['id'] == result['id']
    client, headers, project, id, _ = case
    context = client.get(f'/api/v1/projects/{project}/quality/artifacts/{id}', headers=headers).json()
    assert context['output_sha256'] == 'a' * 64 and context['reviews'][0]['id'] == result['id']
    assert 'uri' not in context


@pytest.mark.parametrize('change,code', [('hash',409), ('rules',422), ('spoof_reviewer',422), ('foreign_evidence',422), ('duplicate_dimension',422)])
def test_unbound_or_spoofed_review_is_rejected(case, change, code):
    body = deepcopy(case[-1])
    if change == 'hash':
        body['output_sha256'] = 'b' * 64
    elif change == 'rules':
        body['scenario'].pop('content_hash')
    elif change == 'spoof_reviewer':
        body['reviewer_id'] = 'another-person'
    elif change == 'foreign_evidence':
        body['dimensions'][0]['evidence_ids'] = ['not-in-this-project']
    else:
        body['dimensions'][-1] = deepcopy(body['dimensions'][0])
    assert send(case, body).status_code == code


def test_key_conflict_hard_fail_and_changed_media_are_not_hidden(case):
    body = deepcopy(case[-1]); body['dimensions'][0]['status'] = 'failed'
    result = send(case, body)
    assert result.status_code == 201, result.text
    assert result.json()['assessment']['verdict'] == 'failed'
    assert send(case).status_code == 409
    write(lambda db: setattr(db.get(models.Artifact, case[3]), 'metadata_json', {'output_sha256': 'b' * 64}))
    replay = send(case, body).json()
    assert replay['id'] == result.json()['id'] and replay['stale'] is True
    assert send(case, body, key='new-review').status_code == 409


def test_artifact_and_evidence_cannot_cross_projects(case):
    client, headers, _, id, body = case
    other = client.post('/api/v1/projects', headers=headers, json={'name': 'Other'}).json()['id']
    assert client.get(f'/api/v1/projects/{other}/quality/artifacts/{id}', headers=headers).status_code == 404
    assert client.post(f'/api/v1/projects/{other}/quality/reviews', headers={**headers, 'Idempotency-Key':'wrong'}, json=body).status_code == 404


def test_known_render_error_cannot_be_overridden_by_positive_manual_scores(case):
    def known_failure(db):
        row = db.get(models.Artifact, case[3])
        row.metadata_json = {**row.metadata_json, 'metrics': {'renderer_quality_findings': [{
            'code': 'image_end_timing_unverified', 'severity': 'error', 'dimension': 'technical',
            'message': 'Known measured boundary failure', 'evidence': {'source': 'acceptance_record', 'reference':'fixture'}}]}}
    write(known_failure)
    result = send(case)
    assert result.status_code == 201, result.text
    assert result.json()['assessment']['verdict'] == 'failed'


def test_audio_not_applicable_requires_real_no_audio_fact(case):
    body = deepcopy(case[-1])
    body['dimensions'][-1] = {'dimension':'audio','status':'not_applicable','note':'Measured silent output','evidence_ids':[case[3]]}
    assert send(case, body).status_code == 201
    write(lambda db: setattr(db.get(models.Artifact, case[3]), 'metadata_json', {'output_sha256':'a'*64}))
    response = send(case, body, key='new')
    assert response.status_code == 422 and response.json()['detail']['code'] == 'quality_audio_unverified'


def test_structural_warning_cannot_hide_a_renderer_error(case):
    warning = {'code': 'subtitle_needs_review', 'severity': 'warning', 'dimension': 'visual',
        'message': 'Check rendered text', 'evidence': {'source': 'canonical_timeline'}}
    error = {**warning, 'code': 'ice_image_end_unverified', 'severity': 'error', 'dimension': 'technical'}
    def reports(db):
        row = db.get(models.Artifact, case[3])
        row.metadata_json = {**row.metadata_json, 'metrics': {
            'render_quality_assessment': {'schema_version': 'render-quality-assessment/1',
                'scope': 'structural_probe', 'status': 'passed', 'findings': [warning]},
            'renderer_quality_findings': [warning, error]}}
    write(reports)
    result = send(case)
    assert result.status_code == 201, result.text
    assert result.json()['assessment']['verdict'] == 'failed'
    findings = result.json()['assessment']['findings']
    assert len(findings) == 2 and {row['severity'] for row in findings} == {'warning', 'error'}


def test_invalid_persisted_report_does_not_become_a_pass(case):
    def corrupt_report(db):
        row = db.get(models.Artifact, case[3])
        row.metadata_json = {**row.metadata_json, 'metrics': {'renderer_quality_findings': 'not-a-report'}}
    write(corrupt_report)
    result = send(case)
    assert result.status_code == 409
    assert result.json()['detail']['code'] == 'quality_findings_invalid'


def test_bound_context_and_original_receipt_are_available(case):
    client, headers, project, id, body = case
    def bind(db):
        job = models.Job(tenant_id=headers['X-Tenant-ID'], project_id=project, type='render.timeline',
            payload={'manifest': {'source': {'timeline': {'metadata': {'scenario': body['scenario']}}}}})
        db.add(job)
        db.flush()
        db.get(models.Artifact, id).job_id = job.id
    write(bind)
    result = send(case).json()
    context = client.get(f'/api/v1/projects/{project}/quality/artifacts/{id}', headers=headers).json()
    assert context['scenario'] == body['scenario']
    assert set(context['required_dimensions']) == {'facts', 'technical', 'narrative', 'visual', 'audio'}
    assert context['has_audio'] is False and context['technical_findings'] == []
    path = f'/api/v1/projects/{project}/quality/reviews/requests/'
    assert client.get(path + 'review', headers=headers).json()['result']['id'] == result['id']
    assert client.get(path + 'unknown', headers=headers).json() == {'found': False, 'result': None}


def test_receipt_is_scoped_to_original_reviewer(case):
    from app.api.deps import RequestIdentity, authenticate_request
    client, headers, project, _, _ = case
    send(case)
    app.dependency_overrides[authenticate_request] = lambda: RequestIdentity(principal_id='another-reviewer', tenant_id=headers['X-Tenant-ID'])
    try:
        result = client.get(f'/api/v1/projects/{project}/quality/reviews/requests/review', headers=headers)
        assert result.status_code == 200
        assert result.json() == {'found': False, 'result': None}
    finally:
        app.dependency_overrides.pop(authenticate_request, None)


@pytest.mark.parametrize('has_audio', [True, None, 'false', 0])
def test_conflicting_audio_measurements_cannot_mark_sound_inapplicable(case, has_audio):
    def conflict(db):
        row = db.get(models.Artifact, case[3])
        row.metadata_json = {**row.metadata_json, 'metrics': {'media': {'has_audio': has_audio}}}
    write(conflict)
    body = deepcopy(case[-1])
    body['dimensions'][-1] = {'dimension': 'audio', 'status': 'not_applicable', 'evidence_ids': [case[3]], 'note': 'Silent'}
    assert send(case, body).status_code == 422


@pytest.mark.parametrize('metrics', [[], False, {'render_quality_assessment': []},
    {'render_quality_assessment': {'schema_version': 'render-quality-assessment/1', 'scope': 'structural_probe', 'status': 'failed', 'findings': []}}])
def test_corrupt_or_contradictory_report_fails_closed(case, metrics):
    def corrupt(db):
        row = db.get(models.Artifact, case[3])
        row.metadata_json = {**row.metadata_json, 'metrics': metrics}
    write(corrupt)
    result = send(case)
    assert result.status_code == 409
