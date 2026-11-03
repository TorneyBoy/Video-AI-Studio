"""Source evidence and explicit adoption, with no media/provider execution."""
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app import models
from app.services.auto_edit import AutoEditPlanner, ShotFeature
from app.services.shot_requirements import assess, storyboard_requirement, template_requirement
from app.matching_schemas import RequirementOverride
from timeline_engine import AssetType, TemplateDefinition
from test_storyboards import case as _case, content, create, database, reference, save

case = _case


@pytest.fixture
def matching(case):
    with database() as db:
        asset=db.get(models.Asset,case['asset']);meta=deepcopy(asset.metadata_json)
        meta['analysis']['shots']=[{'start':0,'end':10,'duration':10,'confidence':.8}]
        meta['user_tags']=['蓝色','商品'];asset.metadata_json=meta;db.commit()
    story=create(case).json()
    return {**case,'story':story}


def annotate(case,key='annotation',subject='自己的蓝色商品',base=None,spans=None):
    return case['client'].post('/api/v1/assets/'+case['asset']+'/semantics',headers={**case['headers'],'Idempotency-Key':key},
        json={'base_revision_id':base,'confirmed':True,'spans':spans or [{'id':'whole','visual':{'subject':subject},'tags':['蓝色']}],'note':'用户实际核对的描述'})


def report(case,key='report',story=None,asset_ids=None,overrides=None):
    return case['client'].post('/api/v1/storyboards/'+case['story']['document_id']+'/match-reports',
        headers={**case['headers'],'Idempotency-Key':key},json={'story_revision_id':(story or case['story'])['revision_id'],
            'asset_ids':asset_ids or [case['asset']],'overrides':overrides or {}})


def decisions(value,*,lock=False):
    return [{'shot_id':row['shot_id'],'mode':'candidate' if row['candidates'] else 'reshoot',
        'candidate_id':row['candidates'][0]['id'] if row['candidates'] else None,'locked':lock and bool(row['candidates']),
        'reviewed':True,'note':'已核对主体、蓝色与不可改变的细节'} for row in value['result']['items']]


def adopt(case,value,key='adopt',base=None,choices=None,**changes):
    return case['client'].post('/api/v1/storyboards/'+case['story']['document_id']+'/sourcing',
        headers={**case['headers'],'Idempotency-Key':key},json={'report_id':value['id'],'base_revision_id':base,
            'choices':choices if choices is not None else decisions(value),**changes})


def test_shared_template_and_story_requirements_are_honest_about_missing_semantics():
    template=TemplateDefinition.model_validate({'id':'one','version':'1','slots':[{'name':'body','type':'video',
        'constraints':{'min_duration':2,'allowed_asset_types':['video'],'required_metadata':['start']}}],
        'metadata':{'slot_policies':{'body':{'orientation':'portrait','required_tags':['蓝色']}}}})
    requirement=template_requirement(template.slots[0],template.metadata['slot_policies']['body'])
    result=assess(requirement,kind='video',duration=3,orientation='portrait',metadata={'start':0},tags=['蓝色'])
    assert result['verdict']=='supported'
    shots=[ShotFeature(asset_id='one',duration=3,asset_type=AssetType.VIDEO,orientation='portrait',metadata={'start':0,'_user_tags':['蓝色']})]
    assert AutoEditPlanner().plan(template,shots).selected[0].asset_id=='one'
    assert not AutoEditPlanner().plan(template,[ShotFeature(asset_id='unknown',duration=3,orientation='portrait',metadata={'start':0})]).selected
    body=content();body['facts']=[]
    need=storyboard_requirement({**body['shots'][0],'visual':{'subject':'自己的蓝色商品'},'constraints':[]},[],RequirementOverride())
    unknown=assess(need,kind='video',duration=5,orientation='portrait',visual={'subject':'蓝色商品'})
    assert not unknown['rejected'] and 'visual_description_differs:subject' in unknown['review']
    assert 'similarity' not in unknown


def test_frozen_report_annotation_change_and_required_review(matching):
    case=matching;first=report(case).json();candidate=first['result']['items'][0]['candidates'][0]
    assert candidate['semantic_provenance']=='unavailable' and candidate['verdict']=='needs_review'
    assert candidate['start_ms']==0 and candidate['end_ms']==4000
    assert annotate(case).status_code==200
    replay=report(case).json()
    assert replay['id']==first['id'] and replay['stale_inputs']
    assert replay['result']==first['result']
    assert adopt(case,first).status_code==409
    second=report(case,'new-report').json();candidate=second['result']['items'][0]['candidates'][0]
    assert candidate['semantic_provenance']=='user_confirmed'
    assert candidate['observed_visual']['subject']=='自己的蓝色商品'
    assert 'product_facts_require_visual_review' in candidate['review']
    bad=decisions(second);bad[0]['reviewed']=False
    assert adopt(case,second,choices=bad).status_code==422
    bad[0]['reviewed']=True;bad[0]['note']=''
    assert adopt(case,second,choices=bad).status_code==422
    good=adopt(case,second)
    assert good.status_code==200,good.text
    assert good.json()['content']['all_material'] and good.json()['inputs_current']
    with database() as db:
        assert db.scalar(select(func.count()).select_from(models.Job))==0
        assert db.scalar(select(func.count()).select_from(models.UsageLedger))==0


def test_lock_unlock_cas_history_and_original_receipt(matching):
    case=matching;value=report(case).json();rows=decisions(value,lock=True)
    first=adopt(case,value,choices=rows).json()
    changed=[{'shot_id':'opening','mode':'reshoot','note':'补拍同样蓝色商品'}]
    denied=adopt(case,value,'replace',base=first['revision_id'],choices=changed)
    assert denied.status_code==409 and denied.json()['detail']['code']=='sourcing_locked'
    second=adopt(case,value,'unlock',base=first['revision_id'],choices=changed,unlock_shot_ids=['opening']).json()
    assert second['number']==2 and not second['content']['all_material']
    replay=adopt(case,value,choices=rows).json()
    assert replay['revision_id']==first['revision_id'] and replay['head_revision_id']==second['revision_id']
    assert adopt(case,value,'stale',base=first['revision_id'],choices=changed).status_code==409
    receipt=case['client'].get('/api/v1/storyboards/'+case['story']['document_id']+'/source-requests/adopt',headers=case['headers']).json()
    assert receipt['found'] and receipt['result']['revision_id']==first['revision_id']
    history=case['client'].get('/api/v1/storyboards/'+case['story']['document_id']+'/source-revisions',headers=case['headers']).json()
    assert [row['number'] for row in history]==[2,1] and history[1]['content']['choices'][0]['locked']
    manifest=case['client'].get('/api/v1/storyboards/'+case['story']['document_id']+'/reshoot-manifest',headers=case['headers']).json()
    assert manifest['items'][0]['shot_id']=='opening' and '商品为蓝色' in manifest['text']


def test_explicit_exclusions_duration_and_no_silent_reference_use(matching):
    case=matching
    assert reference(case).status_code==200
    value=report(case).json()
    assert value['result']['items'][0]['candidates']==[]
    assert value['result']['excluded_assets'][0]['reason']=='reference_only'
    with database() as db:
        db.get(models.ReferenceUsage,case['asset']).allow_output=True;db.commit()
    value=report(case,'excluded-tag',overrides={'opening':{'excluded_tags':['蓝色']}}).json()
    assert not value['result']['items'][0]['candidates']
    assert value['result']['items'][0]['excluded_counts']['excluded_tag_present']==1
    value=report(case,'wrong-kind',overrides={'opening':{'media_types':['image']}}).json()
    assert value['result']['items'][0]['excluded_counts']['media_type_mismatch']==1
    with database() as db:
        asset=db.get(models.Asset,case['asset']);meta=deepcopy(asset.metadata_json)
        meta['analysis']['shots']=[{'start':0,'end':2,'duration':2}];asset.metadata_json=meta;db.commit()
    value=report(case,'short').json()
    assert not value['result']['items'][0]['candidates'] and value['result']['items'][0]['excluded_counts']['source_too_short']==1


def test_source_mutation_invalidates_kept_binding_and_gap_export(matching):
    case=matching;value=report(case).json();first=adopt(case,value).json()
    with database() as db:
        asset=db.get(models.Asset,case['asset']);asset.uri='https://fixtures.example.test/replaced.mp4';db.commit()
    current=case['client'].get('/api/v1/storyboards/'+case['story']['document_id']+'/sourcing',headers=case['headers']).json()
    assert not current['inputs_current']
    assert case['client'].get('/api/v1/storyboards/'+case['story']['document_id']+'/reshoot-manifest',headers=case['headers']).status_code==409
    assert adopt(case,value,'keep',base=first['revision_id'],choices=[{'shot_id':'opening','mode':'keep'}]).status_code==409


def test_repeated_ranges_need_explicit_reuse_and_diversity_is_proposal_only(matching):
    case=matching;body=deepcopy(case['story']['content'])
    body['shots'][0]['duration_ms']=2000;body['shots'].append({**deepcopy(body['shots'][0]),'id':'second'})
    case['story']=save(case,case['story'],body,'two-shots').json()
    value=report(case).json()
    assert value['result']['items'][1]['candidates'][0]['diversity']['already_recommended_source']
    response=adopt(case,value)
    assert response.status_code==422 and response.json()['detail']['code']=='sourcing_reuse_confirmation'
    approved=adopt(case,value,allow_reuse=True)
    assert approved.status_code==200 and approved.json()['content']['reused_intervals']==[['opening','second']]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(models.StoryboardSourceRevision))==1


def test_changed_story_cannot_silently_reuse_locked_source(matching):
    case=matching;value=report(case).json();first=adopt(case,value,choices=decisions(value,lock=True)).json()
    body=deepcopy(case['story']['content']);body['shots'][0]['duration_ms']=3000;body['target_duration_ms']=3000
    case['story']=save(case,case['story'],body,'new-duration').json()
    assert adopt(case,value,'old-story',base=first['revision_id'],choices=decisions(value,lock=True)).status_code==409
    new=report(case,'new-duration-report').json()
    assert adopt(case,new,'keep-incompatible',base=first['revision_id'],choices=[{'shot_id':'opening','mode':'keep','locked':True}]).status_code==409
    result=adopt(case,new,'new-choice',base=first['revision_id'],choices=decisions(new),unlock_shot_ids=['opening'])
    assert result.status_code==200 and result.json()['story_current']


@pytest.mark.parametrize('spans',[
    [{'id':'incomplete','start_ms':1000}],
    [{'id':'out','start_ms':0,'end_ms':11000}],
    [{'id':'a','start_ms':0,'end_ms':5000},{'id':'b','start_ms':4000,'end_ms':6000}],
    [{'id':'a'},{'id':'b'}],
])
def test_invalid_annotation_ranges_do_not_create_versions(matching,spans):
    assert annotate(matching,spans=spans).status_code==422
    with database() as db:
        assert db.scalar(select(func.count()).select_from(models.AssetSemanticRevision))==0


def test_annotation_versions_replay_preserves_original_and_partial_evidence(matching):
    case=matching;first=annotate(case).json()
    second=annotate(case,'second',base=first['revision_id'],spans=[{'id':'whole','visual':{'subject':'自己的蓝色商品'}},
        {'id':'part','start_ms':1000,'end_ms':6000,'visual':{'action':'转动'}}]).json()
    replay=annotate(case).json()
    assert replay['revision_id']==first['revision_id'] and replay['head_revision_id']==second['revision_id']
    response=report(case).json()
    partial=[row for row in response['result']['items'][0]['candidates'] if row['annotation_span_id']=='part']
    assert partial and partial[0]['observed_visual']['subject']=='自己的蓝色商品'
    assert partial[0]['observed_visual']['action']=='转动'


def test_foreign_project_ids_candidates_and_receipts_are_private(matching):
    case=matching;semantic=annotate(case).json();value=report(case).json();source=adopt(case,value).json()
    other=case['client'].post('/api/v1/tenants',json={'name':'Other','slug':'matching-other'}).json()['id'];headers={'X-Tenant-ID':other}
    for path in ['/assets/'+case['asset']+'/semantics','/assets/'+case['asset']+'/semantic-requests/annotation',
        '/storyboards/'+case['story']['document_id']+'/match-reports/'+value['id'],
        '/storyboards/'+case['story']['document_id']+'/source-requests/adopt',
        '/storyboards/'+case['story']['document_id']+'/reshoot-manifest']:
        assert case['client'].get('/api/v1'+path,headers=headers).status_code==404
    assert semantic['revision_id'] and source['revision_id']
    bad=decisions(value);bad[0]['candidate_id']='foreign-or-fabricated'
    assert adopt(case,value,'forged',base=source['revision_id'],choices=bad).status_code==422


def test_duplicate_asset_aliases_do_not_bypass_source_reuse_check(matching):
    case=matching
    with database() as db:
        original=db.get(models.Asset,case['asset'])
        alias=models.Asset(tenant_id=case['tenant'],project_id=case['project'],name='Alias of same object',kind=original.kind,
            uri=original.uri,metadata_json=deepcopy(original.metadata_json));db.add(alias);db.commit();alias_id=alias.id
    body=deepcopy(case['story']['content']);body['shots'][0]['duration_ms']=2000
    body['shots'].append({**deepcopy(body['shots'][0]),'id':'second'})
    case['story']=save(case,case['story'],body,'two-alias-shots').json()
    value=report(case,asset_ids=[case['asset'],alias_id]).json()
    first,second=value['result']['items']
    one=next(row for row in first['candidates'] if row['asset_id']==case['asset'])
    two=next(row for row in second['candidates'] if row['asset_id']==alias_id)
    assert one['source_group']==two['source_group'] and two['diversity']['already_recommended_source']
    rows=decisions(value);rows[0]['candidate_id']=one['id'];rows[1]['candidate_id']=two['id']
    denied=adopt(case,value,choices=rows)
    assert denied.status_code==422 and denied.json()['detail']['code']=='sourcing_reuse_confirmation'


def test_image_hold_and_semantics_range_rules(matching):
    case=matching
    with database() as db:
        image=models.Asset(tenant_id=case['tenant'],project_id=case['project'],name='Own product photo',kind='image/png',
            uri='https://fixtures.example.test/product.png',metadata_json={});db.add(image);db.commit();image_id=image.id
    image_case={**case,'asset':image_id}
    assert annotate(image_case,spans=[{'id':'range','start_ms':0,'end_ms':4000}]).status_code==422
    assert annotate(image_case).status_code==200
    value=report(case,asset_ids=[image_id],overrides={'opening':{'media_types':['image']}}).json()
    candidate=value['result']['items'][0]['candidates'][0]
    assert candidate['kind']=='image' and candidate['end_ms'] is None and candidate['duration_provenance']=='image_hold'
    result=adopt(case,value)
    assert result.status_code==200 and result.json()['content']['choices'][0]['binding']['kind']=='image'
