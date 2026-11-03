"""Real authoring transactions; fixture media metadata never executes a provider."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import func, select

from app import editing_schemas as schemas, models
from app.database import get_db
from app.main import app
from app.services.clip_authoring import apply_clip_action, editor_view
from app.services.editing_errors import EditingError
from timeline_engine import Timeline
from timeline_engine.timing import FrameClock
from test_edit_revisions import edit_case as _edit_case, save, timeline

edit_case = _edit_case


def _head(case):
    return case['client'].get(f"/api/v1/editing/{case['revision']['document_id']}", headers=case['headers']).json()


def _saved(case, base, key, action):
    response = save(case, base, key, [action])
    assert response.status_code == 201, response.text
    return response.json()


def test_trim_is_inward_frame_aligned_and_keeps_original_snapshot(edit_case):
    case = edit_case; first = case['revision']; before = timeline(case, first)
    clip = first['plan']['selected'][0]
    edited = _saved(case, first, 'trim', {'op': 'trim_clip', 'clip_id': clip['clip_id'], 'source_start': 1.01, 'source_end': 3.99})
    actual = timeline(case, edited); rows = edited['plan']['selected']; clock = FrameClock(30)
    assert rows[0]['source_start'] == clock.seconds(31)
    assert rows[0]['source_end'] == clock.seconds(119)
    assert rows[0]['window_start'] == clip['window_start'] and rows[0]['window_end'] == clip['window_end']
    assert rows[0]['selection_id'] == clip['selection_id']
    assert rows[1]['timeline_start'] == rows[0]['timeline_end'] == clock.seconds(88)
    assert actual['duration'] == clock.seconds(208)
    assert edited['template']['duration'] == actual['duration']
    assert timeline(case, first) == before
    assert edited['editor']['duration'] == actual['duration']
    assert not any(value in json.dumps(edited['editor']) for value in ['file://', 'oss://', 'https://', 'metadata'])


@pytest.mark.parametrize('action,code', [
    ({'op': 'trim_clip', 'source_start': 0, 'source_end': 5}, 'edit_source_window_exceeded'),
    ({'op': 'trim_clip', 'source_start': 0.01, 'source_end': 0.02}, 'edit_clip_too_short'),
    ({'op': 'split_clip', 'offset_seconds': 4}, 'edit_split_bounds'),
    ({'op': 'move_clip', 'to_index': 10}, 'edit_order_bounds'),
])
def test_invalid_clip_operation_keeps_head_and_old_content(edit_case, action, code):
    case = edit_case; first = case['revision']; before = timeline(case, first)
    response = save(case, first, 'invalid', [{**action, 'clip_id': first['plan']['selected'][0]['clip_id']}])
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == code
    assert _head(case)['revision_id'] == first['revision_id']
    assert timeline(case, first) == before


def test_split_keeps_later_locked_clip_and_stable_receipt(edit_case):
    case = edit_case; first = case['revision']; a, b = first['plan']['selected']
    locked = _saved(case, first, 'lock-body', {'op': 'set_lock', 'selection_id': b['selection_id'], 'locked': True})
    before = timeline(case, locked)['tracks'][0]['clips'][1]
    action = {'op': 'split_clip', 'clip_id': a['clip_id'], 'offset_seconds': 1.75}
    split = _saved(case, locked, 'split', action); actual = timeline(case, split)
    clips = actual['tracks'][0]['clips']; rows = split['plan']['selected']
    assert len(rows) == len(clips) == 3
    assert clips[0]['source_end'] == clips[1]['source_start'] == FrameClock(30).seconds(52)
    assert clips[0]['range']['end'] == clips[1]['range']['start']
    assert clips[2] == before
    assert rows[2]['locked'] and not rows[0]['locked'] and not rows[1]['locked']
    assert len({row['selection_id'] for row in rows}) == 3
    assert len({clip['id'] for clip in clips}) == 3
    replay = _saved(case, locked, 'split', action)
    assert replay['revision_id'] == split['revision_id']


@pytest.mark.parametrize('op', ['trim_clip', 'move_clip', 'remove_clip'])
def test_ripple_cannot_shift_a_locked_neighbor(edit_case, op):
    case = edit_case; first = case['revision']; a, b = first['plan']['selected']
    locked = _saved(case, first, 'locked-neighbor', {'op': 'set_lock', 'selection_id': b['selection_id'], 'locked': True})
    action = {'op': op, 'clip_id': a['clip_id']}
    if op == 'trim_clip':
        action.update(source_start=1, source_end=4)
    elif op == 'move_clip':
        action.update(to_index=1)
    response = save(case, locked, 'shift-locked', [action])
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'edit_selection_locked'
    assert _head(case)['revision_id'] == locked['revision_id']


def test_precise_resize_keeps_trimmed_intervals_and_volume_in_manifest(edit_case):
    case = edit_case; first = case['revision']; row = first['plan']['selected'][0]
    trimmed = _saved(case, first, 'shorten', {'op': 'trim_clip', 'clip_id': row['clip_id'], 'source_start': 1, 'source_end': 3})
    volume = _saved(case, trimmed, 'gain', {'op': 'set_clip_audio', 'clip_id': row['clip_id'], 'volume': 0.35})
    larger = _saved(case, volume, 'larger', {'op': 'configure', 'target_duration': 12, 'aspect_ratio': '1:1'})
    content = timeline(case, larger)
    assert content['duration'] == 6
    assert content['tracks'][0]['clips'][0]['source_start'] == 1
    assert content['tracks'][0]['clips'][0]['source_end'] == 3
    assert content['tracks'][0]['clips'][0]['volume'] == 0.35
    shorter = _saved(case, larger, 'shorter', {'op': 'configure', 'target_duration': 3, 'aspect_ratio': '1:1'})
    exact = timeline(case, shorter)
    assert exact['duration'] == 3 and exact['tracks'][0]['clips'][0]['volume'] == 0.35
    assert exact['tracks'][0]['clips'][0]['source_start'] == 1
    assert exact['tracks'][0]['clips'][0]['source_end'] <= 3
    rendered = case['client'].post(f"/api/v1/editing/{shorter['document_id']}/revisions/{shorter['revision_id']}/render",
        headers={**case['headers'], 'Idempotency-Key': 'precise-render'}, json={'quality': 'draft'})
    assert rendered.status_code == 201, rendered.text
    assert rendered.json()['manifest']['source']['timeline'] == exact
    assert rendered.json()['job']['payload']['timeline_revision_id'] == shorter['revision_id']


def test_remove_required_slot_blocks_render_without_allocating_job(edit_case):
    case = edit_case; first = case['revision']; row = first['plan']['selected'][0]
    removed = _saved(case, first, 'remove-hook', {'op': 'remove_clip', 'clip_id': row['clip_id']})
    assert removed['plan']['missing_required_slots'] == ['hook']
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        before = db.scalar(select(func.count()).select_from(models.Job))
        response = case['client'].post(f"/api/v1/editing/{removed['document_id']}/revisions/{removed['revision_id']}/render",
            headers={**case['headers'], 'Idempotency-Key': 'incomplete'}, json={'quality': 'draft'})
        assert response.status_code == 422, response.text
        assert db.scalar(select(func.count()).select_from(models.Job)) == before
    finally:
        iterator.close()


def test_later_invalid_command_rolls_back_earlier_in_memory_trim(edit_case):
    case = edit_case; first = case['revision']; a = first['plan']['selected'][0]
    response = save(case, first, 'atomic', [{'op': 'trim_clip', 'clip_id': a['clip_id'], 'source_start': 1, 'source_end': 3},
        {'op': 'move_clip', 'clip_id': a['clip_id'], 'to_index': 99}])
    assert response.status_code == 422
    assert _head(case)['revision_id'] == first['revision_id']
    assert timeline(case, first)['tracks'][0]['clips'][0]['source_start'] == 0


def test_absolute_other_tracks_are_preserved_or_explicitly_block_shortening(edit_case):
    case = edit_case; first = case['revision']; raw = timeline(case, first)
    raw['tracks'].append({'id': 'subtitle', 'type': 'subtitle', 'clips': [
        {'id': 'cue', 'text': 'Untouched', 'range': {'start': 6, 'end': 8}}]})
    canonical = Timeline.model_validate(raw); row = first['plan']['selected'][0]
    action = schemas.TrimClip(op='trim_clip', clip_id=row['clip_id'], source_start=1, source_end=3)
    with pytest.raises(EditingError, match='其他轨道超出新时长'):
        apply_clip_action(canonical, first['plan']['selected'], action, intent='other-track')
    assert canonical.model_dump(mode='json')['tracks'][-1]['clips'][0]['text'] == 'Untouched'
    muted = apply_clip_action(canonical, first['plan']['selected'], schemas.SetClipAudio(op='set_clip_audio', clip_id=row['clip_id'], volume=0), intent='mute')
    assert muted.tracks[-1] == canonical.tracks[-1]
    view = editor_view(muted.model_dump(mode='json'))
    assert view['tracks'][0]['clips'][0]['volume'] == 0
    assert view['tracks'][-1]['clips'][0]['text'] == 'Untouched'
    assert canonical == Timeline.model_validate(deepcopy(raw))


def test_readonly_editor_view_does_not_copy_font_locations_or_media_grants():
    value = {'fps': 30, 'duration': 2, 'resolution': {'width': 1080, 'height': 1920},
        'assets': [{'id': 'source', 'uri': 'https://private.invalid/media?signature=secret'}],
        'tracks': [{'id': 'sub', 'type': 'subtitle', 'clips': [{'id': 'cue', 'text': 'Private project text',
            'range': {'start': 0, 'end': 1}, 'style': {'font_size': 48, 'font_file': 'https://private.invalid/font?signature=secret'}}]}]}
    view = editor_view(value)
    assert 'private.invalid' not in json.dumps(view)
    assert view['tracks'][0]['clips'][0]['style'] == {'font_size': 48}
    assert value['tracks'][0]['clips'][0]['style']['font_file'].startswith('https://')
