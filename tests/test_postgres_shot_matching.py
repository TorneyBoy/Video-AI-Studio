"""Real source-head and semantic-evidence races on disposable PG schemas."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app import matching_schemas as schemas, models, storyboard_schemas
from app.services import shot_matching as matching, storyboards
from app.services.editing_errors import EditingError
from test_postgres_completion import postgres_database as _postgres_database
from test_storyboards import content, analysis

postgres_database = _postgres_database


def setup(factory,job_id):
    with factory() as db:
        tenant=db.get(models.Job,job_id).tenant_id
        project=models.Project(tenant_id=tenant,name='Source races');db.add(project);db.flush()
        uri='https://fixtures.example.test/race.mp4';facts=analysis(uri);facts['shots']=[{'start':0,'end':10,'duration':10}]
        asset=models.Asset(tenant_id=tenant,project_id=project.id,name='Owned source',kind='video',uri=uri,
            metadata_json={'duration':10,'analysis':facts});db.add(asset);db.flush()
        story=storyboards.create_story(db,tenant,project.id,storyboard_schemas.StoryCreate(content=content()),'story','actor')
        report=matching.create_report(db,tenant,story['document_id'],schemas.MatchCreate(story_revision_id=story['revision_id'],asset_ids=[asset.id]),'report','actor')
        db.commit();return tenant,asset.id,story,report


@pytest.mark.parametrize('mode',['same_key','different_key','different_body'])
def test_sourcing_compare_and_swap_and_durable_replay(postgres_database,mode):
    factory,job_id=postgres_database;tenant,_,story,report=setup(factory,job_id);barrier=Barrier(2)
    def write(index):
        with factory() as db:
            choice={'shot_id':'opening','mode':'candidate','candidate_id':report['result']['items'][0]['candidates'][0]['id'],
                'reviewed':True,'note':'已核对自己的蓝色商品'+(str(index) if mode=='different_body' else '')}
            request=schemas.SourceSave(report_id=report['id'],choices=[choice])
            barrier.wait(timeout=10)
            try:
                value=matching.save_sources(db,tenant,story['document_id'],request,'race-'+str(index) if mode=='different_key' else 'race','actor')
                db.commit();return value['revision_id']
            except EditingError as error:
                db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,[0,1]))
    if mode=='same_key':assert results[0]==results[1]
    else:assert sum(row==('sourcing_revision_conflict' if mode=='different_key' else 'idempotency_input_conflict') for row in results)==1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.StoryboardSourceRevision))==1
        head=db.get(models.StoryboardSourceHead,story['document_id'])
        assert db.get(models.StoryboardSourceRevision,head.revision_id).number==1
        assert db.scalar(select(func.count()).select_from(models.Job))==1
        assert db.scalar(select(func.count()).select_from(models.UsageLedger))==0


def test_semantic_revision_creation_is_idempotent_under_concurrency(postgres_database):
    factory,job_id=postgres_database;tenant,asset,_,_=setup(factory,job_id);barrier=Barrier(2)
    def write(_):
        with factory() as db:
            request=schemas.SemanticSave(confirmed=True,spans=[{'id':'whole','visual':{'subject':'蓝色商品'}}])
            barrier.wait(timeout=10)
            value=matching.save_semantics(db,tenant,asset,request,'same','actor');db.commit();return value['revision_id']
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,[0,1]))
    assert results[0]==results[1]
    with factory() as db:assert db.scalar(select(func.count()).select_from(models.AssetSemanticRevision))==1
