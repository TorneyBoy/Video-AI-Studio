"""AI business transactions, mutable rights and real-media adoption boundaries."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import models
from app.database import get_db
from app.main import app
from app.services import ai_production as service
from app.services.worker import _provider_request, _validate_provider_policy


def mutate(callback):
    iterator = app.dependency_overrides[get_db]()
    db = next(iterator)
    try:
        result = callback(db)
        db.commit()
        return result
    finally:
        iterator.close()


@pytest.fixture
def production(client, monkeypatch):
    tenant = client.post('/api/v1/tenants', json={'slug': 'ai-case', 'name': 'AI Case'}).json()['id']
    headers = {'X-Tenant-ID': tenant}
    project = client.post('/api/v1/projects', headers=headers, json={'name': 'AI production'}).json()['id']
    entry = {'id': 'aliyun-wan:video_generation:wan2.2', 'provider': 'aliyun-wan', 'model_id': 'wan2.2-t2v-plus',
        'model_version': 'wan2.2', 'capability': 'video_generation', 'available': True, 'status': 'available',
        'limits': {'max_output_duration_seconds': 5, 'allowed_durations': [5], 'generation_resolutions': ['720P'],
                   'max_reference_inputs': 0, 'max_prompt_chars': 800},
        'price': {'version': 'fixture-1', 'currency': 'CNY', 'billing_unit': 'request', 'per_request': 1, 'per_minute': None}}
    monkeypatch.setattr(service, 'capability_catalog', lambda: {'capabilities': [deepcopy(entry)]})
    def setup(db):
        db.add(models.TenantBudget(tenant_id=tenant, currency='CNY', monthly_limit_micros=20_000_000,
                                  task_limit_micros=5_000_000))
        image = models.Asset(tenant_id=tenant, project_id=project, name='Authorized identity', kind='image/png',
            uri=f'oss://fixture/tenants/{tenant}/uploads/identity.png', metadata_json={'storage': {'status': 'ready'}})
        video = models.Asset(tenant_id=tenant, project_id=project, name='Owned source', kind='video/mp4',
            uri=f'oss://fixture/tenants/{tenant}/uploads/source.mp4', metadata_json={'duration': 5, 'storage': {'status': 'ready'}})
        db.add_all([image, video]); db.flush()
        return image.id, video.id
    image, video = mutate(setup)
    return {'client': client, 'headers': headers, 'project': project, 'tenant': tenant, 'entry': entry,
            'image': image, 'video': video, 'prefix': f'/api/v1/projects/{project}/ai-production'}


def post(case, path, body, key):
    return case['client'].post(case['prefix'] + path, headers={**case['headers'], 'Idempotency-Key': key}, json=body)


def quote(case, **changes):
    prompt = '' if changes.get('operation') == 'person_replace' else '自己的蓝色商品在柔光下缓慢转动'
    return post(case, '/quotes', {'operation': 'video_generation', 'prompt': prompt, **changes}, 'quote')


def submitted(case, **changes):
    q = quote(case, **changes)
    assert q.status_code == 201, q.text
    response = post(case, '/jobs', {'quote_id': q.json()['id']}, 'generate')
    assert response.status_code == 201, response.text
    return response.json(), q.json()


def identity(case):
    result = post(case, '/profiles', {'name': '自己的形象', 'subject_id': '授权测试主体',
        'reference_asset_id': case['image'], 'granted': True}, 'identity')
    assert result.status_code == 201, result.text
    return result.json()


def finish(case, job_id, **media_changes):
    def update(db):
        job = db.get(models.Job, job_id)
        uri = f'oss://fixture/tenants/{case["tenant"]}/generated/{job.id}/output.mp4'
        media = {'status': 'ok', 'object_uri': uri, 'sha256': 'a' * 64, 'size_bytes': 500,
                 'duration_seconds': 5, 'width': 1280, 'height': 720, 'fps': 30, **media_changes}
        artifact = models.Artifact(tenant_id=case['tenant'], project_id=case['project'], job_id=job_id,
            kind='video', uri=uri, metadata_json={'provider': 'aliyun-wan',
                'model': {'model_id': case['entry']['model_id'], 'version': 'wan2.2'}, 'metrics': {'media': media}})
        db.add(artifact); job.status = 'succeeded'
    mutate(update)


def test_quote_does_not_dispatch_and_confirmation_is_atomic_and_recoverable(production):
    case = production
    q = quote(case)
    assert q.status_code == 201 and q.json()['amount_micros'] == 1_000_000, q.text
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.Job))) == 0
    job = post(case, '/jobs', {'quote_id': q.json()['id']}, 'generate')
    assert job.status_code == 201, job.text
    replay = post(case, '/jobs', {'quote_id': q.json()['id']}, 'generate')
    assert replay.json()['id'] == job.json()['id']
    receipt = case['client'].get(case['prefix'] + '/requests/generate', headers=case['headers']).json()
    assert receipt['found'] and receipt['result']['id'] == job.json()['id']
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.Job))) == 1
    assert post(case, '/jobs', {'quote_id': q.json()['id']}, 'different-confirm').status_code == 409
    assert quote(case, prompt='原键用于另一个描述').status_code == 409


@pytest.mark.parametrize('change', ['price', 'expiry', 'budget'])
def test_changed_quote_never_creates_a_billable_job(production, change):
    case = production; q = quote(case).json()
    if change == 'price':
        case['entry']['price']['version'] = 'new-price'
    elif change == 'expiry':
        mutate(lambda db: setattr(db.get(models.PreparationQuote, q['id']), 'expires_at', datetime.now(timezone.utc) - timedelta(seconds=1)))
    else:
        mutate(lambda db: setattr(db.scalar(select(models.TenantBudget)), 'monthly_limit_micros', 0))
    response = post(case, '/jobs', {'quote_id': q['id']}, 'generate')
    assert response.status_code == 409, response.text
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.Job))) == 0


@pytest.mark.parametrize('changes', [{'duration_seconds': 6}, {'prompt': '长' * 801},
                                   {'reference_asset_ids': ['missing']}, {'options': {'mode': 'wan-pro'}}])
def test_model_limits_block_unexecutable_quotes(production, changes):
    response = quote(production, **changes)
    assert response.status_code in {404, 409}, response.text


def test_atomic_identity_has_receipt_and_never_regrants_revoked_consent(production):
    case = production; profile = identity(case)
    mutate(lambda db: setattr(db.get(models.Consent, profile['consent_id']), 'granted', False))
    same = identity(case)
    assert same['id'] == profile['id']
    receipt = case['client'].get(case['prefix'] + '/profiles/requests/identity', headers=case['headers']).json()
    assert receipt['found'] and receipt['result']['id'] == profile['id']
    assert not mutate(lambda db: db.get(models.Consent, profile['consent_id']).granted)
    denied = post(case, '/profiles', {'name': 'other', 'subject_id': 'same', 'reference_asset_id': case['image'], 'granted': False}, 'denied')
    assert denied.status_code == 422


def test_face_only_and_person_replacement_are_not_interchangeable(production):
    case = production; profile = identity(case)
    case['entry'].update(capability='person_replacement', model_id='wan2.2-animate-mix')
    case['entry']['limits'].update(max_reference_inputs=1, generation_modes=['wan-std', 'wan-pro'])
    body = {'avatar_profile_id': profile['id'], 'source_asset_id': case['video']}
    assert quote(case, operation='face_replace', **body).status_code == 409
    job, _ = submitted(case, operation='person_replace', **body)
    def check(db):
        row = db.get(models.Job, job['id'])
        request = _provider_request(row, __import__('pathlib').Path('unused'), db)
        assert request.inputs['reference_asset_uris'] and request.inputs['source_asset_uri'].endswith('source.mp4')
        _validate_provider_policy(db, row, request)
        db.get(models.Consent, profile['consent_id']).granted = False
        db.flush()
        with pytest.raises(Exception, match='授权'):
            _validate_provider_policy(db, row, request)
        db.add(models.ProviderOperation(tenant_id=case['tenant'], job_id=row.id, request_id=row.id,
                                       external_job_id='paid-original', state='pending'))
        db.flush()
        _validate_provider_policy(db, row, request)  # Poll paid task despite revocation.
    mutate(check)
    finish(case, job['id'])
    assert not case['client'].get(case['prefix'] + '/jobs/' + job['id'], headers=case['headers']).json()['can_adopt']
    assert post(case, '/jobs/' + job['id'] + '/adopt', {}, 'adopt').status_code == 409


def test_derived_result_is_a_candidate_until_verified_and_explicitly_adopted(production):
    case = production; job, _ = submitted(case)
    assert post(case, '/jobs/' + job['id'] + '/adopt', {}, 'adopt').status_code == 409
    finish(case, job['id'])
    adopted = post(case, '/jobs/' + job['id'] + '/adopt', {}, 'adopt')
    assert adopted.status_code == 200, adopted.text
    second = post(case, '/jobs/' + job['id'] + '/adopt', {}, 'adopt')
    assert second.json()['id'] == adopted.json()['id']
    assert adopted.json()['metadata']['storage']['sha256'] == 'a' * 64
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.Asset))) == 3


@pytest.mark.parametrize('bad', [{'duration_seconds': 0.1}, {'width': 0.5}, {'fps': 9999},
                                {'height': 480}, {'height': 704}, {'object_uri': 'oss://other/not-the-result'}, {'sha256': 'bad'}])
def test_bad_actual_media_is_not_adoptable(production, bad):
    case = production; job, _ = submitted(case); finish(case, job['id'], **bad)
    response = post(case, '/jobs/' + job['id'] + '/adopt', {}, 'adopt')
    assert response.status_code == 409, response.text


def person_model(case, *, historical_catalog=False):
    case['entry'].update(capability='person_replacement', model_id='wan2.2-animate-mix')
    case['entry']['limits'].update(max_reference_inputs=1, generation_modes=['wan-std', 'wan-pro'])
    if not historical_catalog:
        del case['entry']['limits']['generation_resolutions']
        case['entry']['limits']['output_fps_by_mode'] = {'wan-std': 15, 'wan-pro': 25}
    def actual_source(db):
        source = db.get(models.Asset, case['video'])
        source.metadata_json = {**source.metadata_json, 'width': 1282, 'height': 720, 'fps': 30}
    mutate(actual_source)


@pytest.mark.parametrize('mode,fps', [('wan-std', 15), ('wan-pro', 25)])
@pytest.mark.parametrize('historical_catalog', [False, True])
def test_person_mode_preserves_measured_704_output_without_claiming_exact_720(production, mode, fps, historical_catalog):
    case = production; profile = identity(case); person_model(case, historical_catalog=historical_catalog)
    job, _ = submitted(case, operation='person_replace', avatar_profile_id=profile['id'],
        source_asset_id=case['video'], options={'mode': mode})
    finish(case, job['id'], width=1252, height=704, fps=fps, duration_seconds=5.2,
           frame_count=round(5.2 * fps))
    view = case['client'].get(case['prefix'] + '/jobs/' + job['id'], headers=case['headers'])
    assert view.json()['can_adopt'], view.text
    adopted = post(case, '/jobs/' + job['id'] + '/adopt', {}, 'adopt')
    assert adopted.status_code == 200, adopted.text
    media = adopted.json()['metadata']['generated_media']
    assert (media['width'], media['height'], media['fps'], media['duration_seconds']) == (1252, 704, fps, 5.2)
    assert media['sha256'] == 'a' * 64
    assert adopted.json()['metadata']['analysis']['video']['height'] == 704
    assert post(case, '/jobs/' + job['id'] + '/adopt', {}, 'adopt').json()['id'] == adopted.json()['id']
    def check(db):
        request = _provider_request(db.get(models.Job, job['id']), __import__('pathlib').Path('unused'), db)
        assert request.parameters['mode'] == mode and 'resolution' not in request.parameters
    mutate(check)


@pytest.mark.parametrize('historical_catalog', [False, True])
@pytest.mark.parametrize('bad', [{'width': True}, {'height': 0}, {'width': 8193}, {'width': 1252.5},
                                {'duration_seconds': 5.4}, {'fps': 25}, {'fps': 30},
                                {'sha256': 'bad'}, {'size_bytes': 0}, {'status': 'failed'},
                                {'object_uri': 'oss://other/not-the-result'}])
def test_person_mode_keeps_media_evidence_and_duration_guards(production, historical_catalog, bad):
    case = production; profile = identity(case); person_model(case, historical_catalog=historical_catalog)
    job, _ = submitted(case, operation='person_replace', avatar_profile_id=profile['id'], source_asset_id=case['video'])
    finish(case, job['id'], **{'width': 1252, 'height': 704, 'fps': 15, 'duration_seconds': 5.2, **bad})
    response = post(case, '/jobs/' + job['id'] + '/adopt', {}, 'adopt')
    assert response.status_code == 409, response.text
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.Asset))) == 2


def test_reference_only_source_and_cross_project_assets_cannot_be_used(production):
    case = production; profile = identity(case)
    case['entry'].update(capability='person_replacement', model_id='wan2.2-animate-mix')
    mutate(lambda db: db.add(models.ReferenceUsage(asset_id=case['video'], tenant_id=case['tenant'],
        project_id=case['project'], allow_output=False)))
    response = quote(case, operation='person_replace', avatar_profile_id=profile['id'], source_asset_id=case['video'])
    assert response.status_code == 409 and response.json()['detail']['code'] == 'ai_reference_output_forbidden'
    other = case['client'].post('/api/v1/projects', headers=case['headers'], json={'name': 'other'}).json()['id']
    result = case['client'].post(f'/api/v1/projects/{other}/ai-production/profiles', headers={**case['headers'], 'Idempotency-Key': 'cross'},
        json={'name': 'wrong-project', 'subject_id': 'subject', 'granted': True, 'reference_asset_id': case['image']})
    assert result.status_code == 404


def test_queued_price_change_blocks_new_spending_but_keeps_paid_original_queryable(production):
    case = production; job, _ = submitted(case)
    case['entry']['price']['version'] = 'price-after-queue'
    def check(db):
        row = db.get(models.Job, job['id'])
        request = _provider_request(row, __import__('pathlib').Path('unused'), db)
        with pytest.raises(Exception, match='价格'):
            _validate_provider_policy(db, row, request)
        db.add(models.ProviderOperation(tenant_id=case['tenant'], job_id=row.id, request_id=row.id,
                                       external_job_id='already-paid', state='pending'))
        db.flush()
        _validate_provider_policy(db, row, request)
    mutate(check)


@pytest.mark.parametrize('after_submit', [False, True])
def test_story_change_requires_a_new_quote_before_any_new_generation(production, after_submit):
    case = production
    def story(db):
        document = models.StoryboardDocument(tenant_id=case['tenant'], project_id=case['project'],
            request_key='story', request_hash='b' * 64)
        db.add(document); db.flush()
        revision = models.StoryboardRevision(tenant_id=case['tenant'], document_id=document.id,
            number=1, request_key='story-v1', request_hash='c' * 64,
            content_json={'shots': [{'id': 'missing', 'duration_ms': 4000}]}, actor_id='fixture')
        db.add(revision); db.flush(); document.head_revision_id = revision.id
        return {'document_id': document.id, 'revision_id': revision.id, 'shot_id': 'missing'}
    binding = mutate(story)
    q = quote(case, storyboard=binding)
    assert q.status_code == 201, q.text
    if after_submit:
        job = post(case, '/jobs', {'quote_id': q.json()['id']}, 'generate').json()
    mutate(lambda db: setattr(db.get(models.StoryboardDocument, binding['document_id']), 'head_revision_id', 'different'))
    if not after_submit:
        assert post(case, '/jobs', {'quote_id': q.json()['id']}, 'generate').status_code == 409
    else:
        def check(db):
            row = db.get(models.Job, job['id'])
            with pytest.raises(Exception, match='分镜'):
                _validate_provider_policy(db, row, _provider_request(row, __import__('pathlib').Path('unused'), db))
        mutate(check)
