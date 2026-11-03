"""Real PostgreSQL edit-head races; never contacts a supplier."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from timeline_engine import TemplateDefinition
from app import editing_schemas, models, schemas
from app.api import routers
from app.api.routers import _render_timeline_job
from app.services.auto_edit import AutoEditPlanner, compile_auto_edit_timeline, shot_features_from_assets
from app.services.editing import EditingError, apply_edit, initialize_document
from test_postgres_completion import postgres_database as _postgres_database

postgres_database = _postgres_database


def _document(factory, existing_job):
    with factory() as db:
        tenant = db.get(models.Job, existing_job).tenant_id
        project = models.Project(tenant_id=tenant, name='Edit race'); db.add(project); db.flush()
        asset = models.Asset(tenant_id=tenant, project_id=project.id, name='Source', kind='video/mp4',
            uri='https://fixture.invalid/owned.mp4', metadata_json={'duration': 4,
                'analysis': {'shots': [{'start': 0, 'end': 4, 'duration': 4}]}})
        db.add(asset); db.flush()
        template = TemplateDefinition.model_validate({'id': 'short', 'version': '1', 'duration': 4,
            'slots': [{'name': 'hook', 'type': 'video'}]})
        plan = AutoEditPlanner().plan(template, shot_features_from_assets([asset]))
        job = models.Job(tenant_id=tenant, project_id=project.id, type='auto_edit.plan', status='succeeded',
            payload={'plan': {'selected': [row.__dict__ for row in plan.selected], 'warnings': list(plan.warnings)}})
        db.add(job); db.flush()
        canonical = compile_auto_edit_timeline(template, [row.__dict__ for row in plan.selected], [asset], timeline_id=models.new_id())
        timeline = models.Timeline(id=canonical.id, tenant_id=tenant, project_id=project.id, name='Source timeline',
            fps=canonical.fps, duration_ms=4000, timeline_json=canonical.model_dump(mode='json'))
        db.add(timeline); db.flush()
        revision = initialize_document(db, job=job, timeline_row=timeline, assets=[asset], template=template)
        db.commit()
        return tenant, revision.document_id, revision.id, revision.plan_json['selected'][0]['selection_id'], timeline.id


@pytest.mark.parametrize('same_request', [False, True])
def test_parallel_editors_commit_one_head_and_replay_same_request_once(postgres_database, same_request):
    factory, existing = postgres_database
    _, document_id, base_id, selection_id, _ = _document(factory, existing)
    barrier = Barrier(2)
    request = editing_schemas.EditApplyRequest.model_validate({'base_revision_id': base_id,
        'operations': [{'op': 'set_lock', 'selection_id': selection_id, 'locked': True}]})

    def write(index):
        with factory() as db:
            document = db.get(models.EditDocument, document_id)
            barrier.wait(timeout=10)
            try:
                revision = apply_edit(db, document=document, request=request,
                                      key='same' if same_request else f'page-{index}', actor=f'page-{index}')
                db.commit(); return revision.id
            except EditingError as error:
                db.rollback(); return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, index) for index in range(2)]
        results = [future.result(timeout=25) for future in futures]
    if same_request:
        assert results[0] == results[1] and results[0] != base_id
    else:
        assert results.count('edit_revision_conflict') == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.PlanRevision)) == 2
        assert db.scalar(select(func.count()).select_from(models.TimelineRevision)) == 2
        head = db.get(models.EditHead, document_id)
        assert head.number == 2 and head.revision_id in results
        assert db.get(models.PlanRevision, base_id).plan_json['selected'][0]['locked'] is False


def test_rendering_a_revision_during_head_change_keeps_the_requested_snapshot(postgres_database):
    factory, existing = postgres_database
    tenant, document_id, base_id, selection_id, timeline_id = _document(factory, existing)
    barrier = Barrier(2)

    def edit():
        with factory() as db:
            document = db.get(models.EditDocument, document_id)
            barrier.wait(timeout=10)
            result = apply_edit(db, document=document, key='edit', actor='editor',
                request=editing_schemas.EditApplyRequest.model_validate({'base_revision_id': base_id,
                    'operations': [{'op': 'set_lock', 'selection_id': selection_id, 'locked': True}]}))
            db.commit(); return result.id

    def render():
        with factory() as db:
            row = db.get(models.Timeline, timeline_id)
            barrier.wait(timeout=10)
            return _render_timeline_job(db, row, schemas.RenderRequest(quality='draft'), tenant_id=tenant, key='preview')['job']
    with ThreadPoolExecutor(max_workers=2) as pool:
        edit_future, render_future = pool.submit(edit), pool.submit(render)
        new_revision = edit_future.result(timeout=25); job = render_future.result(timeout=25)
    assert job.payload['timeline_revision_id'] == base_id
    assert job.payload['manifest']['timeline_id'] == timeline_id
    with factory() as db:
        assert db.get(models.EditHead, document_id).revision_id == new_revision != base_id
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.type == 'render.draft')) == 1


def test_same_plan_key_with_concurrent_different_parameters_does_not_return_an_unrelated_plan(postgres_database, monkeypatch):
    factory, existing = postgres_database
    tenant, document_id, base_id, _, _ = _document(factory, existing)
    with factory() as db:
        document = db.get(models.EditDocument, document_id)
        base = db.get(models.PlanRevision, base_id)
        template = models.Template(tenant_id=tenant, project_id=document.project_id, name='Race', version='1', content=base.template_json)
        db.add(template); db.commit()
        project_id, template_id = document.project_id, template.id
        asset_ids = [row['id'] for row in base.sources_json]
    barrier = Barrier(2); local_state = local()
    original_lookup = routers._existing_job_for_key

    def lookup(db, owner, key, expected_type=None):
        result = original_lookup(db, owner, key, expected_type)
        if key == 'racing-plan' and not getattr(local_state, 'waited', False):
            local_state.waited = True; barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(routers, '_existing_job_for_key', lookup)

    def submit(duration):
        with factory() as db:
            try:
                result = routers.create_auto_edit_job(schemas.AutoEditCreate(project_id=project_id,
                    template_id=template_id, asset_ids=asset_ids, target_duration=duration),
                    tenant_id=tenant, db=db, idempotency_header='racing-plan')
                return result.id
            except HTTPException as error:
                db.rollback(); return error.detail['code']
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, value) for value in (2, 4)]
        results = [future.result(timeout=25) for future in futures]
    assert results.count('idempotency_input_conflict') == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.Job).where(models.Job.idempotency_key == 'racing-plan')) == 1
