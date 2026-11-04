"""Track editing and admission contracts; fixture sources never execute media."""
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import models
from app.database import get_db
from app.main import app
from app.services import render_features
from app.services.renderer import build_ffmpeg_command, RenderQualityError
from timeline_engine import Timeline
from timeline_engine.validation import validate_timeline
from test_edit_revisions import edit_case as _edit_case, save, timeline

edit_case = _edit_case


def source(case, kind='audio'):
    response = case['client'].post('/api/v1/assets', headers=case['headers'], json={
        'project_id': case['project'], 'name': 'Layer fixture', 'kind': kind,
        'uri': 'file:///__qa_no_media__/layer.' + ('png' if kind == 'image' else 'wav'),
        'metadata': {'duration': 12, 'qa_fixture': True, 'analysis': {'media': {'duration_seconds': 12}, 'audio': {'channels': 2}}}})
    assert response.status_code == 201, response.text
    return response.json()['id']


def audio(asset, **overrides):
    return {'op': 'save_audio_clip', 'clip_id': None, 'track_id': 'music', 'asset_id': asset,
            'start': 0.01, 'end': 4.01, 'source_start': 1.01, 'volume': 0.4, 'fade_in': 0.5, 'fade_out': 1, 'role': 'music', **overrides}


def overlay(asset, **overrides):
    return {'op': 'save_overlay_clip', 'clip_id': None, 'track_id': 'logo', 'asset_id': asset,
            'start': 0, 'end': 2, 'source_start': 0, 'placement': {'x': 20, 'y': 30, 'width': 200, 'height': 100},
            'opacity': 0.7, 'fit': 'cover', **overrides}


def saved(case, base, key, action, ids=None):
    response = save(case, base, key, [action], ids)
    assert response.status_code == 201, response.text
    return response.json()


def head(case):
    return case['client'].get('/api/v1/editing/' + case['revision']['document_id'], headers=case['headers']).json()


def render(case, revision, key='render-layer'):
    return case['client'].post(f"/api/v1/editing/{revision['document_id']}/revisions/{revision['revision_id']}/render",
                              headers={**case['headers'], 'Idempotency-Key': key}, json={'quality': 'draft'})


def test_audio_facts_frames_move_receipt_and_undo(edit_case):
    case = edit_case; first = case['revision']; asset = source(case)
    added = saved(case, first, 'audio', audio(asset), [asset]); raw = timeline(case, added)
    clip = raw['tracks'][-1]['clips'][0]
    assert clip['range'] == {'start': 1 / 30, 'end': 4}
    assert clip['source_start'] == 31 / 30 and clip['source_end'] == 5
    assert clip['volume'] == 0.4 and clip['effects'][0]['in_seconds'] == 0.5
    assert clip['metadata']['audio_role'] == 'music'
    assert added['editor']['tracks'][-1]['clips'][0]['composition']['audio_fade']['out_seconds'] == 1
    moved = saved(case, added, 'move-audio', audio(asset, clip_id=clip['id'], track_id='voice', start=0, end=3, source_start=2))
    moved_raw = timeline(case, moved)
    assert not moved_raw['tracks'][1]['clips']
    assert moved_raw['tracks'][-1]['clips'][0]['id'] == clip['id']
    assert saved(case, first, 'audio', audio(asset), [asset])['revision_id'] == added['revision_id']
    undone = saved(case, moved, 'undo-audio-move', {'op': 'undo'})
    assert timeline(case, undone)['tracks'] == raw['tracks'] and timeline(case, first)['tracks'] == raw['tracks'][:1]


@pytest.mark.parametrize('overrides,code', [
    ({'end': 20}, 'edit_layer_bounds'), ({'source_start': 11}, 'edit_source_window_exceeded'),
    ({'fade_in': 3, 'fade_out': 2}, 'edit_audio_fade_bounds'), ({'fade_in': 0.001}, 'edit_audio_fade_bounds')])
def test_invalid_audio_is_atomic(edit_case, overrides, code):
    case = edit_case; asset = source(case); first = case['revision']
    response = save(case, first, 'invalid-layer', [audio(asset, **overrides)], [asset])
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == code
    assert head(case)['revision_id'] == first['revision_id']


def test_source_capture_ownership_and_track_overlap_are_explicit(edit_case):
    case = edit_case; first = case['revision']; asset = source(case)
    assert save(case, first, 'uncaptured', [audio(asset)]).status_code == 404
    added = saved(case, first, 'captured', audio(asset), [asset])
    assert save(case, added, 'overlap', [audio(asset, start=1, end=3)]).status_code == 422
    mixed = saved(case, added, 'separate', audio(asset, track_id='voice', start=1, end=3))
    assert {'audio_tracks', 'multi_audio_tracks', 'audio_fades'} <= set(render_features.advanced_features(timeline(case, mixed)))


def test_image_overlay_placement_mute_order_delete_and_restore(edit_case):
    case = edit_case; first = case['revision']; asset = source(case, 'image')
    added = saved(case, first, 'logo', overlay(asset), [asset]); raw = timeline(case, added)
    assert next(item for item in added['editor']['sources'] if item['id'] == asset)['has_audio'] is False
    clip = raw['tracks'][-1]['clips'][0]
    assert clip['source_end'] is None and clip['volume'] == 0
    assert clip['placement']['x'] == 20 and clip['opacity'] == 0.7
    assert clip['effects'] == [{'type': 'frame_fit', 'schema_version': 'frame-fit/1', 'mode': 'cover'}]
    invalid = save(case, added, 'off-canvas', [overlay(asset, clip_id=clip['id'], placement={'x': 1000, 'y': 0, 'width': 200, 'height': 100})])
    assert invalid.status_code == 422
    muted = saved(case, added, 'hidden', {'op': 'set_track_mute', 'track_id': 'logo', 'muted': True})
    assert timeline(case, muted)['tracks'][-1]['muted']
    removed = saved(case, muted, 'delete-logo', {'op': 'delete_layer_track', 'track_id': 'logo'})
    assert len(timeline(case, removed)['tracks']) == 1
    restored = saved(case, removed, 'restore-logo', {'op': 'restore', 'revision_id': added['revision_id']})
    assert timeline(case, restored)['tracks'] == raw['tracks']


def test_transition_is_real_overlap_clear_and_locked_protection(edit_case):
    case = edit_case; first = case['revision']; row = first['plan']['selected'][1]; original = timeline(case, first)
    action = {'op': 'set_transition', 'clip_id': row['clip_id'], 'type': 'dissolve', 'duration': 0.51}
    added = saved(case, first, 'transition', action); raw = timeline(case, added)
    assert raw['duration'] == added['template']['duration'] == 7.5
    assert raw['tracks'][0]['clips'][1]['range'] == {'start': 3.5, 'end': 7.5}
    for before, after in zip(original['tracks'][0]['clips'], raw['tracks'][0]['clips']):
        assert (before['source_start'], before['source_end']) == (after['source_start'], after['source_end'])
    cleared = saved(case, added, 'no-transition', {**action, 'type': 'none', 'duration': 0})
    assert timeline(case, cleared)['duration'] == 8
    locked = saved(case, cleared, 'locked', {'op': 'set_lock', 'selection_id': row['selection_id'], 'locked': True})
    assert save(case, locked, 'locked-transition', [action]).status_code == 409
    assert save(case, locked, 'locked-track', [{'op': 'set_track_mute', 'track_id': raw['tracks'][0]['id'], 'muted': True}]).status_code == 409


def test_transition_does_not_crop_fixed_layer_and_split_retains_incoming_only(edit_case):
    case = edit_case; first = case['revision']; row = first['plan']['selected'][1]
    asset = source(case, 'image'); added = saved(case, first, 'fixed-layer', overlay(asset, start=7, end=8), [asset])
    action = {'op': 'set_transition', 'clip_id': row['clip_id'], 'type': 'fade', 'duration': 0.5}
    assert save(case, added, 'fixed-bounds', [action]).status_code == 422
    deleted = saved(case, added, 'remove-fixed', {'op': 'delete_layer_track', 'track_id': 'logo'})
    transition = saved(case, deleted, 'fade', action)
    split = saved(case, transition, 'split', {'op': 'split_clip', 'clip_id': row['clip_id'], 'offset_seconds': 2})
    clips = timeline(case, split)['tracks'][0]['clips']
    assert clips[1]['transition']['duration'] == 0.5 and clips[2]['transition'] is None
    assert timeline(case, split)['duration'] == 7.5


@pytest.mark.parametrize('type,duration,index', [('fade', 4, 1), ('fade', 0.001, 1), ('fade', 0.5, 0)])
def test_invalid_transition_keeps_head(edit_case, type, duration, index):
    case = edit_case; first = case['revision']
    result = save(case, first, 'invalid-transition', [{'op': 'set_transition', 'clip_id': first['plan']['selected'][index]['clip_id'], 'type': type, 'duration': duration}])
    assert result.status_code == 422 and head(case)['revision_id'] == first['revision_id']


def test_unsupported_render_allocates_neither_job_nor_reservation(edit_case, monkeypatch):
    case = edit_case; asset = source(case); added = saved(case, case['revision'], 'layer', audio(asset), [asset])
    monkeypatch.setattr(render_features, 'capability_catalog', lambda: {'capabilities': []})
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        before = [db.scalar(select(func.count()).select_from(table)) for table in (models.Job, models.UsageLedger)]
        result = render(case, added)
        assert result.status_code == 422 and result.json()['detail']['code'] == 'render_features_unavailable'
        assert [db.scalar(select(func.count()).select_from(table)) for table in (models.Job, models.UsageLedger)] == before
        assert head(case)['editor']['advanced_support']['supported'] is False
    finally:
        iterator.close()


def test_one_available_service_must_cover_every_effect_and_receipt_survives_withdrawal(edit_case, monkeypatch):
    case = edit_case; asset = source(case); added = saved(case, case['revision'], 'layer', audio(asset), [asset])
    base = {'id': 'verified', 'provider': 'fixture', 'model_id': 'fixture', 'model_version': '1', 'capability': 'video_editing', 'available': True,
            'price': {'version': 'fixture-only', 'currency': 'USD', 'per_minute': None, 'per_request': 0, 'billing_unit': 'request'}}
    entries = [{**base, 'limits': {'features': ['audio_tracks']}}, {**base, 'id': 'second', 'limits': {'features': ['audio_fades']}}]
    monkeypatch.setattr(render_features, 'capability_catalog', lambda: {'capabilities': entries})
    assert not render_features.render_support(timeline(case, added))['supported']
    entries[:] = [{**base, 'limits': {'features': ['audio_tracks', 'audio_fades'],
                                     'feature_combinations': [['audio_tracks', 'audio_fades']]}}]
    result = render(case, added)
    assert result.status_code == 201, result.text
    assert result.json()['job']['payload']['capability_snapshot']['id'] == 'verified'
    entries.clear()
    assert render(case, added).json()['job']['id'] == result.json()['job']['id']
    assert render(case, added, 'new-receipt').status_code == 422


def test_dev_adapter_refuses_unimplemented_effects_instead_of_ignoring(edit_case):
    case = edit_case; asset = source(case); added = saved(case, case['revision'], 'layer', audio(asset), [asset])
    with pytest.raises(RenderQualityError, match='audio effects'):
        build_ffmpeg_command(Timeline.model_validate(timeline(case, added)), '/__qa_no_media__/never.mp4')
    first = case['revision']; raw = timeline(case, first)
    raw['metadata']['composition'] = {'transition_policy': 'overlap'}
    raw['tracks'][0]['clips'][1]['transition'] = {'type': 'fade', 'duration': 0.5}
    raw['tracks'][0]['clips'][1]['range'] = {'start': 3.5, 'end': 7.5}; raw['duration'] = 7.5
    with pytest.raises(RenderQualityError, match='overlap transitions'):
        build_ffmpeg_command(Timeline.model_validate(raw), '/__qa_no_media__/never.mp4')


def test_aspect_change_cannot_displace_existing_overlay(edit_case):
    case = edit_case; asset = source(case, 'image'); first = case['revision']
    added = saved(case, first, 'lower-logo', overlay(asset, placement={'x': 20, 'y': 1600, 'width': 200, 'height': 100}), [asset])
    response = save(case, added, 'square', [{'op': 'configure', 'target_duration': 8, 'aspect_ratio': '1:1'}])
    assert response.status_code == 422 and response.json()['detail']['code'] == 'edit_overlay_bounds'
    assert head(case)['revision_id'] == added['revision_id']


def test_three_way_transition_rejected_without_changing_previous_version(edit_case):
    case = edit_case; first = case['revision']; row = first['plan']['selected'][0]
    split = saved(case, first, 'three', {'op': 'split_clip', 'clip_id': row['clip_id'], 'offset_seconds': 2})
    rows = split['plan']['selected']
    one = saved(case, split, 'first-transition', {'op': 'set_transition', 'clip_id': rows[1]['clip_id'], 'type': 'fade', 'duration': 1.5})
    response = save(case, one, 'three-way', [{'op': 'set_transition', 'clip_id': rows[2]['clip_id'], 'type': 'fade', 'duration': 1}])
    assert response.status_code == 422 and response.json()['detail']['code'] == 'transition_three_way_overlap'
    assert head(case)['revision_id'] == one['revision_id']


def test_new_dispatch_rechecks_features_but_known_remote_poll_remains_recoverable(edit_case, monkeypatch):
    from app.services import completion
    from app.services.providers import ProviderRequest, ProviderExecutionError
    case = edit_case; raw = timeline(case, case['revision'])
    raw['tracks'].append({'id': 'music', 'type': 'audio', 'clips': []})
    raw['tracks'][-1]['clips'] = [{**raw['tracks'][0]['clips'][0], 'id': 'audio'}]
    request = ProviderRequest(request_id='fixture-dispatch', capability='video_editing', inputs={'timeline': raw}, provider='fixture')
    monkeypatch.setattr(completion, 'lock_execution', lambda *_: None)
    monkeypatch.setattr(render_features, 'capability_catalog', lambda: {'capabilities': []})
    router = SimpleNamespace(choose=lambda _: SimpleNamespace(name='fixture'))
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        job = db.get(models.Job, case['job']['id']); ticket = SimpleNamespace(generation=1)
        with pytest.raises(ProviderExecutionError, match='独立音轨'):
            completion.prepare_provider_call(db, job, ticket, request, router)
        assert db.scalar(select(func.count()).select_from(models.ProviderOperation)) == 0
        operation = models.ProviderOperation(job_id=job.id, tenant_id=job.tenant_id, request_id=request.request_id,
            provider_key='fixture', external_job_id='existing-remote', state='pending')
        db.add(operation); db.commit()
        polling = completion.prepare_provider_call(db, job, ticket, request, router)
        assert polling.parameters['provider_job_id'] == 'existing-remote'
    finally:
        iterator.close()


def test_unicode_and_unknown_effects_need_explicit_feature_acceptance(edit_case, monkeypatch):
    raw = timeline(edit_case, edit_case['revision'])
    raw['tracks'].append({'id': 'subtitles', 'type': 'subtitle', 'clips': [{'text': '中文修订'}]})
    raw['tracks'][0]['clips'][0]['effects'] = [{'type': 'unknown_custom_effect'}]
    monkeypatch.setattr(render_features, 'capability_catalog', lambda: {'capabilities': []})
    assert {'unicode_subtitles', 'effect_unknown_custom_effect'} <= set(render_features.render_support(raw)['missing_features'])


def test_overlap_policy_cannot_lie_about_source_duration_or_output_layout(edit_case):
    raw = timeline(edit_case, edit_case['revision'])
    raw['metadata']['composition'] = {'schema_version': 'composition/1', 'transition_policy': 'overlap'}
    raw['tracks'][0]['clips'][1]['transition'] = {'type': 'fade', 'duration': 0.5}
    report = validate_timeline(Timeline.model_validate(raw))
    assert any(item.code == 'transition_layout' for item in report.errors)
    raw['tracks'][0]['clips'][1]['range'] = {'start': 3.5, 'end': 7.5}; raw['duration'] = 7.5
    assert validate_timeline(Timeline.model_validate(raw)).valid
    raw['tracks'][0]['clips'][1]['source_end'] -= 0.5
    assert any(item.code == 'transition_source_timing' for item in validate_timeline(Timeline.model_validate(raw)).errors)
