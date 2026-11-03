"""Delivery metadata and share authorization; all media/gateway facts are fixtures."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from app import models
from app.api import delivery as delivery_api
from app.config import Settings
from app.database import get_db
from app.main import app
from app.services.editing import digest
from test_artifact_reviews import review_case as _review_case, report, review, rendered, mutate, legacy_output
from test_artifact_reviews import edit_case as _edit_case
from test_edit_revisions import save
from test_personal_identity import accounts as _accounts, register

review_case=_review_case
edit_case=_edit_case
accounts=_accounts
SECRET="metadata-fixture-only-share-signing-secret-32chars"


@pytest.fixture
def delivery_case(review_case,monkeypatch):
    case=review_case;settings=Settings(artifact_share_secret=SECRET,auth_public_origin="http://testserver")
    monkeypatch.setattr(delivery_api,"get_settings",lambda:settings)
    case["share_settings"]=settings
    assert review(case,note="QA fixture approval").status_code==201
    return case


def create(case,key="share",**changes):
    state=report(case)
    return case["client"].post(f"/api/v1/artifacts/{case['artifact']}/shares",headers={**case['headers'],'Idempotency-Key':key},
        json={'binding_hash':state['binding_hash'],'review_id':state['latest']['id'],'label':'QA share',**changes})


def token(response):
    assert response.status_code==201,response.text
    return urlsplit(response.json()['url']).fragment


def public(case,credential,path="inspect",headers=None,**extra):
    return case['client'].post('/api/v1/shared/'+path,headers=headers or {},json={'token':credential,**extra})


def test_share_scope_is_explicit_and_only_hashes_persist(delivery_case):
    case=delivery_case;created=create(case);credential=token(created)
    assert created.headers['cache-control']=='no-store' and '#sg1.' in created.json()['url']
    assert public(case,credential).status_code==401
    assert public(case,credential,headers={'X-Tenant-ID':'other'}).status_code==404
    result=public(case,credential,headers=case['headers'])
    assert result.status_code==200,result.text
    assert result.json()['review_status']=='approved'
    assert not any(value in result.text for value in ['artifact_id','tenant_id','job_id','QA fixture approval','oss://','file:///'])
    iterator=app.dependency_overrides[get_db]();db=next(iterator)
    try:
        row=db.get(models.ArtifactShare,created.json()['id'])
        assert row.token_hash==hashlib.sha256(credential.encode()).hexdigest()
        assert credential not in str(row.__dict__) and SECRET not in str(row.__dict__)
    finally: iterator.close()
    repeated=create(case)
    assert repeated.json()['id']==created.json()['id'] and repeated.json()['url']==created.json()['url']
    assert create(case,label='Other').status_code==409


def test_anyone_scope_is_token_only_and_preview_does_not_grant_download(delivery_case):
    case=delivery_case;credential=token(create(case,audience='anyone_with_link'))
    assert public(case,credential).status_code==200
    assert public(case,credential,path='access',purpose='preview').status_code==200
    assert public(case,credential,path='access',purpose='download').status_code==403
    assert public(case,credential,path='file',kind='manifest').status_code==403
    assert public(case,credential+'x').status_code==404
    assert case['gateway'].calls==1


def test_public_manifest_is_redacted_and_private_export_is_hash_bound(delivery_case):
    case=delivery_case;state=report(case)
    private=case['client'].get(f"/api/v1/artifacts/{case['artifact']}/delivery-file?expected_binding={state['binding_hash']}",headers=case['headers'])
    assert private.status_code==200,private.text
    value=private.json();internal=json.loads(value['content'])
    assert hashlib.sha256(value['content'].encode()).hexdigest()==value['sha256']
    assert internal['manifest_hash']==digest({key:val for key,val in internal.items() if key!='manifest_hash'})
    assert internal['binding_hash']==state['binding_hash'] and internal['sources'] and internal['review']['actor_id']
    assert 'oss://' not in value['content'] and 'file:///' not in value['content']
    share=create(case,audience='anyone_with_link',allow_download=True);credential=token(share)
    exported=public(case,credential,path='file',kind='manifest')
    assert exported.status_code==200,exported.text
    assert exported.json()['filename']=='video-delivery.json'
    body=json.loads(exported.json()['content'])
    assert body['visibility']=='public' and body['review_status']=='approved'
    assert not any(field in body for field in ['artifact_id','job_id','binding','sources','timeline','model','review','cost_records'])
    assert not any(value in exported.text for value in ['tenant_id','QA fixture approval','oss://','file:///'])
    assert public(case,credential,path='file',kind='srt').status_code==403
    assert case['client'].get(f"/api/v1/artifacts/{case['artifact']}/delivery-file?expected_binding="+'0'*64,headers=case['headers']).status_code==409


@pytest.mark.parametrize('change',['revoke','expire','key','review','archive'])
def test_expiry_revocation_evidence_and_rotation_block_new_authorization(delivery_case,change,monkeypatch):
    case=delivery_case;created=create(case,audience='anyone_with_link');credential=token(created);share_id=created.json()['id']
    if change=='revoke':
        path=f"/api/v1/artifacts/{case['artifact']}/shares/{share_id}/revoke"
        first=case['client'].post(path,headers=case['headers']);second=case['client'].post(path,headers=case['headers'])
        assert first.json()['revoked_at']==second.json()['revoked_at']
        assert create(case,audience='anyone_with_link').json()['url'] is None
    elif change=='expire': mutate(case,lambda db:setattr(db.get(models.ArtifactShare,share_id),'expires_at',datetime.now(timezone.utc)-timedelta(seconds=1)))
    elif change=='key': monkeypatch.setattr(delivery_api,'get_settings',lambda:Settings(artifact_share_secret=SECRET+'rotated',auth_public_origin='http://testserver'))
    elif change=='review': assert review(case,key='replace-approval',note='New explicit approval').status_code==201
    else: mutate(case,lambda db:setattr(db.get(models.ArtifactStorage,case['artifact']),'sha256','c'*64))
    assert public(case,credential).status_code==404
    assert public(case,credential,path='access').status_code==404
    assert case['gateway'].calls==0


def test_share_revoked_during_signing_returns_no_url_and_lifetime_is_clipped(delivery_case):
    case=delivery_case;expiry=datetime.now(timezone.utc)+timedelta(seconds=70)
    mutate(case,lambda db:setattr(db.get(models.ArtifactStorage,case['artifact']),'expires_at',expiry))
    created=create(case,audience='anyone_with_link',allow_download=True,expires_in_seconds=604800);credential=token(created)
    assert datetime.fromisoformat(created.json()['expires_at'])<=expiry
    def hook():
        case['gateway'].hook=None
        assert case['client'].post(f"/api/v1/artifacts/{case['artifact']}/shares/{created.json()['id']}/revoke",headers=case['headers']).status_code==200
    case['gateway'].hook=hook
    result=public(case,credential,path='access',purpose='download')
    assert result.status_code==404 and 'short-lived-media' not in result.text


def test_compare_is_saved_metadata_and_lists_page_by_cursor(delivery_case):
    case=delivery_case;first=case['revision'];clip=first['plan']['selected'][0]['clip_id']
    updated=save(case,first,'gain-change',[{'op':'set_clip_audio','clip_id':clip,'volume':0.25}])
    assert updated.status_code==201,updated.text
    second=rendered(case,updated.json(),key='new-compared-output')
    comparison=case['client'].get(f"/api/v1/projects/{case['project']}/works/compare?left={case['artifact']}&right={second}",headers=case['headers'])
    assert comparison.status_code==200,comparison.text
    value=comparison.json();assert value['left']['revision_number']==1 and value['right']['revision_number']==2
    assert value['changes'][0]['fields']['volume']=={'before':1.0,'after':0.25}
    assert 'timeline' not in value['left'] and 'oss://' not in comparison.text and 'file:///' not in comparison.text
    url=f"/api/v1/projects/{case['project']}/works?limit=1"
    page=case['client'].get(url,headers=case['headers']).json()
    assert page['items'][0]['artifact_id']==second and page['has_more']
    following=case['client'].get(url+'&cursor='+page['next_cursor'],headers=case['headers']).json()
    assert following['items'][0]['artifact_id']==case['artifact'] and not following['has_more']
    approved=case['client'].get(f"/api/v1/projects/{case['project']}/works?state=approved",headers=case['headers']).json()
    assert [item['artifact_id'] for item in approved['items']]==[case['artifact']]
    assert case['client'].get(url+'&cursor=invalid',headers=case['headers']).status_code==422


def test_share_static_page_hides_credentials_from_referrers_and_schema_errors(delivery_case):
    case=delivery_case;page=case['client'].get('/share')
    assert page.headers['referrer-policy']=='no-referrer' and "frame-ancestors 'none'" in page.headers['content-security-policy']
    credential=token(create(case,audience='anyone_with_link'))
    bad=case['client'].post('/api/v1/shared/inspect',json={'token':credential,'forbidden':'not-echoed'})
    assert bad.status_code==422 and credential not in bad.text and 'not-echoed' not in bad.text
    assert bad.headers['cache-control']=='no-store' and bad.headers['referrer-policy']=='no-referrer'


def test_filtered_empty_page_retains_cursor_without_skipping_old_matches(delivery_case):
    case=delivery_case
    def seed(db):
        old=db.get(models.Artifact,case['artifact']); job=db.get(models.Job,old.job_id)
        now=datetime.now(timezone.utc)
        job.created_at=now-timedelta(days=1)
        for index in range(205):
            db.add(models.Job(tenant_id=case['tenant'],project_id=case['project'],type='render.high',status='running',
                created_at=now-timedelta(seconds=index),payload={}))
    mutate(case,seed)
    url=f"/api/v1/projects/{case['project']}/works?state=approved&limit=1"
    page=case['client'].get(url,headers=case['headers']).json()
    assert page['items']==[] and page['has_more'] and page['scanned']==200 and page['total_jobs']==206
    final=case['client'].get(url+'&cursor='+page['next_cursor'],headers=case['headers']).json()
    assert [row['artifact_id'] for row in final['items']]==[case['artifact']] and not final['has_more']
    assert final['total_outputs']==1


def test_public_file_rechecks_revocation_after_export(delivery_case,monkeypatch):
    from app.services import artifact_shares
    case=delivery_case;created=create(case,audience='anyone_with_link',allow_download=True);credential=token(created)
    original=artifact_shares.delivery_file
    def export_then_revoke(*args,**kwargs):
        result=original(*args,**kwargs)
        assert case['client'].post(f"/api/v1/artifacts/{case['artifact']}/shares/{created.json()['id']}/revoke",headers=case['headers']).status_code==200
        return result
    monkeypatch.setattr(artifact_shares,'delivery_file',export_then_revoke)
    result=public(case,credential,path='file',kind='manifest')
    assert result.status_code==404 and 'manifest_hash' not in result.text


def test_subtitle_export_uses_exact_output_revision_not_current_edit_head(delivery_case):
    case=delivery_case;first=case['revision']
    saved=save(case,first,'delivery-caption',[{'op':'save_subtitle','track_id':'subtitles',
        'text':'Approved output caption','start':0,'end':1,'anchor_mode':'absolute'}])
    assert saved.status_code==201,saved.text
    artifact=rendered(case,saved.json(),key='subtitle-output')
    state=report(case,artifact)
    assert review(case,state=state,artifact=artifact,key='approve-subtitle').status_code==201
    cue_id=next(clip['id'] for track in saved.json()['editor']['tracks'] if track['type']=='subtitle' for clip in track['clips'] if clip['text']=='Approved output caption')
    updated=save(case,saved.json(),'later-caption',[{'op':'save_subtitle','cue_id':cue_id,'track_id':'subtitles',
        'text':'后续修改，不属于旧成片','start':0,'end':1,'anchor_mode':'keep'}])
    assert updated.status_code==201,updated.text
    result=case['client'].get(f'/api/v1/artifacts/{artifact}/delivery-file?kind=srt',headers=case['headers'])
    assert result.status_code==200,result.text
    assert 'Approved output caption' in result.json()['content'] and '后续修改' not in result.json()['content']
    assert hashlib.sha256(result.json()['content'].encode()).hexdigest()==result.json()['sha256']
    # A different project and tenant cannot compare or export this output.
    def new_project(db):
        row=models.Project(tenant_id=case['tenant'],name='Other project');db.add(row);db.flush();case['other_project']=row.id
        assert db.scalar(select(models.Artifact.id).where(models.Artifact.id==artifact))==artifact
    mutate(case,new_project)
    assert case['client'].get(f"/api/v1/projects/{case['other_project']}/works/compare?left={artifact}&right={case['artifact']}",headers=case['headers']).status_code==404
    assert case['client'].get(f'/api/v1/artifacts/{artifact}/delivery-file',headers={'X-Tenant-ID':'another-tenant'}).status_code==404


def test_real_session_share_owner_editor_and_unauthenticated_boundaries(accounts,monkeypatch):
    from app.services import artifact_reviews
    client,factory,settings=accounts
    monkeypatch.setattr(artifact_reviews,'get_settings',lambda:settings)
    monkeypatch.setattr(delivery_api,'get_settings',lambda:Settings(artifact_share_secret=SECRET,auth_public_origin='http://testserver'))
    user=register(client).json()['user']
    with factory() as db: artifact=legacy_output(db,user['tenant_id'])
    case={'client':client,'headers':{},'artifact':artifact}
    assert review(case).status_code==201
    created=create(case);credential=token(created);share_id=created.json()['id']
    assert public(case,credential).status_code==200
    with factory() as db:
        member=db.scalar(select(models.Membership).where(models.Membership.user_id==user['id']));member.role='editor';db.commit()
    assert create(case,key='editor-forgery').status_code==403
    assert client.get(f'/api/v1/artifacts/{artifact}/shares').status_code==403
    assert client.post(f'/api/v1/artifacts/{artifact}/shares/{share_id}/revoke').status_code==403
    assert public(case,credential).status_code==200  # Explicit workspace audience permits members to view.
    client.cookies.clear()
    assert public(case,credential).status_code==401
    assert client.get(f'/api/v1/artifacts/{artifact}/shares').status_code==401
