"""Exact-output review, receipts and delivery gates; no real media or vendor calls."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import models
from app.api.storage import configured_storage_gateway
from app.database import get_db
from app.main import app
from app.services.completion import render_revision_binding
from app.services import artifact_reviews
from timeline_engine import Timeline, create_manifest
from test_edit_revisions import edit_case as _edit_case, save
from test_personal_identity import accounts as _accounts, register

edit_case = _edit_case
accounts = _accounts
SHA = 'b' * 64


class ReviewGateway:
    calls = 0
    hook = None

    def artifact_access(self, **kwargs):
        self.calls += 1
        if self.hook:
            self.hook()
        return {'artifact_id': kwargs['artifact_id'], 'url': 'https://fixture.invalid/short-lived-media',
                'purpose': kwargs['purpose'], 'expires_at': datetime.now(timezone.utc) + timedelta(seconds=60)}


def rendered(case, revision=None, key='review-render', quality='draft'):
    revision = revision or case['revision']
    response = case['client'].post(f"/api/v1/editing/{revision['document_id']}/revisions/{revision['revision_id']}/render",
        headers={**case['headers'], 'Idempotency-Key': key}, json={'quality': quality})
    assert response.status_code == 201, response.text
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        job = db.get(models.Job, response.json()['job']['id']); job.status = 'succeeded'
        manifest = job.payload['manifest']; uri = 'oss://fixture-bucket/qa/render-' + job.id
        qc = {'schema_version': 'media-quality/1', 'status': 'passed', 'object_uri': uri, 'output_sha256': SHA,
            'size_bytes': 1234, 'width': manifest['resolution']['width'], 'height': manifest['resolution']['height'],
            'duration_seconds': manifest['duration'], 'fps': manifest['fps'], 'has_audio': False,
            'video_codec': 'h264', 'container': 'mp4', 'inspector': 'metadata-fixture-only', 'inspector_version': '1'}
        row = models.Artifact(tenant_id=case['tenant'], project_id=case['project'], job_id=job.id, kind='render', uri=uri,
            metadata_json={'qa_fixture': True, 'quality': quality, 'content_hash': manifest['content_hash'],
                'quality_control': qc, 'output_sha256': SHA, **render_revision_binding(job)})
        db.add(row); db.flush()
        db.add(models.ArtifactStorage(artifact_id=row.id, tenant_id=case['tenant'], object_uri=uri + '/archive',
            sha256=SHA, size_bytes=1234, expires_at=datetime.now(timezone.utc) + timedelta(days=1), evidence_json={'qa_fixture': True}))
        db.commit(); return row.id
    finally:
        iterator.close()


@pytest.fixture
def review_case(edit_case):
    case = edit_case; case['artifact'] = rendered(case); case['gateway'] = ReviewGateway()
    app.dependency_overrides[configured_storage_gateway] = lambda: case['gateway']
    try:
        yield case
    finally:
        app.dependency_overrides.pop(configured_storage_gateway, None)


def report(case, artifact=None):
    response = case['client'].get('/api/v1/artifacts/' + (artifact or case['artifact']) + '/review', headers=case['headers'])
    assert response.status_code == 200, response.text
    return response.json()


def review(case, state=None, key='approve', decision='approved', artifact=None, **changes):
    state = state or report(case, artifact)
    return case['client'].post('/api/v1/artifacts/' + (artifact or case['artifact']) + '/reviews',
        headers={**case['headers'], 'Idempotency-Key': key}, json={'binding_hash': state['binding_hash'],
            'base_review_id': state['latest']['id'] if state['latest'] else None,
            'decision': decision, 'note': '', 'issues': [], **changes})


def access(case, purpose='download'):
    return case['client'].post('/api/v1/artifacts/' + case['artifact'] + '/access', headers=case['headers'], json={'purpose': purpose})


def mutate(case, change):
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        change(db); db.commit()
    finally:
        iterator.close()


def test_pending_can_play_and_owner_approval_opens_download_without_new_job_or_fee(review_case):
    case = review_case; state = report(case)
    assert state['status'] == 'pending' and state['can_preview'] and not state['can_download'] and state['revision_number'] == 1
    assert 'oss://' not in str(state) and 'object_uri' not in str(state)
    assert access(case, 'preview').status_code == 200
    assert access(case).status_code == 409 and case['gateway'].calls == 1
    result = review(case, state, note='已审阅画面、声音与字幕')
    assert result.status_code == 201, result.text
    assert datetime.fromisoformat(result.json()['review']['created_at']).utcoffset() == timedelta(0)
    assert result.json()['current']['can_download'] and result.json()['review']['binding_hash'] == state['binding_hash']
    assert access(case).status_code == 200
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        assert db.scalar(select(func.count()).select_from(models.Job)) == 2
        assert db.scalar(select(func.count()).select_from(models.UsageLedger)) == 0
        artifact = case['client'].get('/api/v1/artifacts/' + case['artifact'], headers=case['headers']).json()
        assert artifact['job_status'] == 'succeeded' and not artifact['job_policy_blocked'] and artifact['edit_revision_number'] == 1
        listed = case['client'].get('/api/v1/artifacts?project_id=' + case['project'], headers=case['headers']).json()
        assert next(item for item in listed if item['id'] == case['artifact'])['edit_revision_number'] == 1
    finally:
        iterator.close()


def test_cas_receipts_history_and_return_do_not_move_a_later_decision_backward(review_case):
    case = review_case; first = report(case)
    accepted = review(case, first).json()
    assert review(case, first, key='other-page', decision='changes_requested', note='需修订').status_code == 409
    returned = review(case, accepted['current'], key='return', decision='changes_requested', note='字幕需修订',
        issues=[{'category':'subtitle','severity':'error','message':'修订此句','start':1,'end':2}])
    assert returned.status_code == 201 and returned.json()['current']['status'] == 'changes_requested'
    assert access(case).status_code == 409
    replay = review(case, first)
    assert replay.status_code == 201 and replay.json()['review']['id'] == accepted['review']['id']
    assert replay.json()['current']['latest']['id'] == returned.json()['review']['id']
    assert review(case, first, note='different').status_code == 409
    receipt = case['client'].get(f"/api/v1/artifacts/{case['artifact']}/review-requests/approve", headers=case['headers']).json()
    assert receipt['found'] and receipt['current']['status'] == 'changes_requested'
    history = case['client'].get(f"/api/v1/artifacts/{case['artifact']}/reviews?skip=1&limit=1", headers=case['headers']).json()
    assert len(history) == 1 and history[0]['id'] == accepted['review']['id']


def test_new_edit_and_high_output_require_their_own_review(review_case):
    case = review_case; assert review(case).status_code == 201
    final = rendered(case, key='high', quality='high')
    assert report(case, final)['status'] == 'pending' and not report(case, final)['can_download']
    row = case['revision']['plan']['selected'][0]
    edited = save(case, case['revision'], 'new-edit', [{'op':'set_clip_audio','clip_id':row['clip_id'],'volume':0.2}])
    assert edited.status_code == 201
    new_output = rendered(case, edited.json(), key='new-version')
    new = report(case, new_output)
    assert new['revision_number'] == 2 and new['status'] == 'pending'
    assert new['binding_hash'] != report(case)['binding_hash']


@pytest.mark.parametrize('change', ['archive', 'quality', 'manifest', 'revision', 'revoked'])
def test_changed_evidence_or_policy_cannot_reuse_approval(review_case, change):
    case = review_case; before = report(case); assert review(case, before).status_code == 201
    def alter(db):
        row = db.get(models.Artifact, case['artifact'])
        if change == 'archive': db.get(models.ArtifactStorage,row.id).sha256 = 'c' * 64
        elif change == 'quality': row.metadata_json = {**row.metadata_json, 'quality_control': {**row.metadata_json['quality_control'], 'width': 320}}
        elif change == 'manifest':
            job = db.get(models.Job,row.job_id); job.payload = {**job.payload,'manifest':{**job.payload['manifest'],'duration':100}}
        elif change == 'revision': row.metadata_json = {**row.metadata_json,'timeline_hash':'0' * 64}
        else: row.delivery_status = 'revoked'
    mutate(case, alter)
    state = report(case)
    assert not state['can_download'] and not state['quality_ready']
    assert access(case).status_code in {403,409}
    assert case['gateway'].calls == 0
    assert review(case, before).json()['review']['number'] == 1, 'old receipt remains readable without granting delivery'


@pytest.mark.parametrize('decision', ['approved','changes_requested'])
def test_review_change_during_signing_returns_no_new_download_url(review_case, decision):
    case = review_case; approved = review(case).json()['current']
    def hook():
        case['gateway'].hook = None
        response = review(case, approved, key='during-sign', decision=decision, note='签名期间更新意见')
        assert response.status_code == 201
    case['gateway'].hook = hook
    result = access(case)
    assert result.status_code == 409 and 'short-lived-media' not in result.text


@pytest.mark.parametrize('changes', [
    {'decision':'changes_requested'},
    {'issues':[{'message':'Unresolved','severity':'error'}]},
    {'issues':[{'message':'Past end','severity':'warning','start':8.1}]},
    {'issues':[{'message':'Wrong order','severity':'warning','start':2,'end':1}]},
    {'actor_id':'forged-owner'},
])
def test_invalid_review_keeps_previous_state(review_case, changes):
    case = review_case; state = report(case)
    response = review(case, state, **changes)
    assert response.status_code == 422, response.text
    assert report(case)['latest'] is None


def test_cross_tenant_review_history_receipt_and_submission_are_hidden(review_case):
    case = review_case
    other = case['client'].post('/api/v1/tenants', json={'slug':'review-other','name':'Other'}).json()['id']
    for suffix in ['review','reviews','review-requests/approve']:
        response = case['client'].get(f"/api/v1/artifacts/{case['artifact']}/{suffix}", headers={'X-Tenant-ID':other})
        assert response.status_code == 404
    bad = {**case,'headers':{'X-Tenant-ID':other}}
    assert review(bad, report(case)).status_code == 404


def legacy_output(db, tenant_id, job=None):
    """A marked no-media fixture used for auth and PostgreSQL row-lock checks."""
    canonical = Timeline.model_validate({'schema_version':'1.0','id':'legacy-review-fixture','fps':30,'duration':2,
        'resolution':{'width':320,'height':240}, 'assets':[{'id':'source','type':'video','uri':'https://fixture.invalid/source'}],
        'tracks':[{'id':'main','type':'video','clips':[{'id':'clip','asset_id':'source','range':{'start':0,'end':2},'source_start':0,'source_end':2}]}]})
    manifest = create_manifest(canonical)
    if job is None:
        job = models.Job(tenant_id=tenant_id, type='render.high', status='succeeded', payload={}); db.add(job); db.flush()
    job.type = 'render.high'; job.status = 'succeeded'; job.payload = {'manifest':manifest.model_dump(mode='json')}
    uri = 'oss://fixture-bucket/qa/auth-' + job.id
    qc = {'schema_version':'media-quality/1','status':'passed','object_uri':uri,'output_sha256':SHA,'size_bytes':1234,
        'width':320,'height':240,'duration_seconds':2,'fps':30,'has_audio':False,'video_codec':'h264','container':'mp4',
        'inspector':'metadata-fixture-only','inspector_version':'1'}
    row = models.Artifact(tenant_id=tenant_id,job_id=job.id,kind='render',uri=uri,metadata_json={
        'qa_fixture':True,'content_hash':manifest.content_hash,'quality_control':qc,'output_sha256':SHA})
    db.add(row); db.flush(); db.add(models.ArtifactStorage(artifact_id=row.id,tenant_id=tenant_id,object_uri=uri+'/archive',
        sha256=SHA,size_bytes=1234,expires_at=datetime.now(timezone.utc)+timedelta(days=1),evidence_json={'qa_fixture':True}))
    db.commit(); return row.id


def test_real_personal_session_owner_can_self_review_but_editor_cannot_approve(accounts, monkeypatch):
    client, factory, settings = accounts
    monkeypatch.setattr(artifact_reviews,'get_settings',lambda:settings)
    user = register(client).json()['user']
    with factory() as db:
        artifact = legacy_output(db,user['tenant_id'])
    case = {'client':client,'headers':{},'artifact':artifact}
    state = report(case); assert state['can_review']
    approved = review(case,state)
    assert approved.status_code == 201 and approved.json()['review']['actor_id'] == user['id']
    with factory() as db:
        member = db.scalar(select(models.Membership).where(models.Membership.user_id == user['id']))
        member.role = 'editor'; db.commit()
    state = report(case); assert not state['can_review']
    denied = review(case,state,key='editor-forgery')
    assert denied.status_code == 403 and denied.json()['detail']['code'] == 'review_permission_denied'
    assert client.post(f'/api/v1/artifacts/{artifact}/reviews',headers={'X-Auth-Principal':user['id']},json={}).status_code == 400
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.ArtifactReview)) == 1
