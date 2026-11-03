"""Real PostgreSQL authoring races on unique disposable schemas."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app import models
from app.storyboard_schemas import ReferenceCreate, StoryCreate, StorySave
from app.services.editing_errors import EditingError
from app.services.storyboards import create_reference, create_story, save_story
from test_postgres_completion import postgres_database as _postgres_database
from test_storyboards import content

postgres_database = _postgres_database


def setup(factory, job_id):
    with factory() as db:
        tenant = db.get(models.Job,job_id).tenant_id
        project=models.Project(tenant_id=tenant,name='Storyboard races');db.add(project);db.flush()
        asset=models.Asset(tenant_id=tenant,project_id=project.id,name='Reference',kind='video',
            uri='https://fixtures.example.test/source.mp4',metadata_json={'duration':10});db.add(asset);db.commit()
        return tenant,project.id,asset.id


@pytest.mark.parametrize('mode',['same_key','different_key','different_body'])
def test_revision_races_only_publish_one_new_head(postgres_database,mode):
    factory,job_id=postgres_database;tenant,project,_=setup(factory,job_id)
    with factory() as db:
        first=create_story(db,tenant,project,StoryCreate(content=content()),'initial','actor');db.commit()
    barrier=Barrier(2)
    def write(index):
        with factory() as db:
            changed=content();changed['shots'][0]['voiceover']='new-'+str(index if mode=='different_body' else 0)
            request=StorySave(base_revision_id=first['revision_id'],content=changed)
            barrier.wait(timeout=10)
            try:
                result=save_story(db,tenant,first['document_id'],request,'race-'+str(index) if mode=='different_key' else 'race','actor')
                db.commit();return result['revision_id']
            except EditingError as error:
                db.rollback();return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(write,[0,1]))
    if mode=='same_key':
        assert results[0]==results[1]
    else:
        code='storyboard_revision_conflict' if mode=='different_key' else 'idempotency_input_conflict'
        assert sum(row==code for row in results)==1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.StoryboardRevision))==2
        document=db.get(models.StoryboardDocument,first['document_id'])
        assert db.get(models.StoryboardRevision,document.head_revision_id).number==2
        assert db.get(models.StoryboardRevision,first['revision_id']).content_json['shots'][0]['voiceover']=='这是自己的文案'


def test_duplicate_reference_and_document_creation_are_serialized(postgres_database):
    factory,job_id=postgres_database;tenant,project,asset=setup(factory,job_id)
    barrier=Barrier(2)
    def write(_):
        with factory() as db:
            barrier.wait(timeout=10)
            reference=create_reference(db,tenant,project,ReferenceCreate(asset_id=asset,start_ms=0,end_ms=5000),'reference','actor')
            result=create_story(db,tenant,project,StoryCreate(reference_id=reference.id,content=content()),'create','actor')
            db.commit();return reference.id,result['document_id'],result['revision_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(write,[0,1]))
    assert results[0]==results[1]
    with factory() as db:
        for model in (models.ReferenceAnalysis,models.ReferenceUsage,models.StoryboardDocument,models.StoryboardRevision):
            assert db.scalar(select(func.count()).select_from(model))==1
