"""Real version transactions for ASR originals, corrections and text exports."""
import hashlib
import json

import pytest
from sqlalchemy import select

from app import models
from app.database import get_db
from app.main import app
from app.services.worker import process_queued_auto_edit_jobs
from test_edit_revisions import edit_case as _edit_case, save, timeline

edit_case = _edit_case


@pytest.fixture
def subtitle_case(edit_case):
    case = edit_case; source_id = case['revision']['plan']['selected'][0]['source_asset_id']
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        asset = db.get(models.Asset, source_id); metadata = dict(asset.metadata_json)
        analysis = dict(metadata['analysis']); analysis['transcription'] = {'status': 'ok', 'provider': 'fixture-asr', 'version': '1', 'language': 'zh',
            'segments': [{'start': 0, 'end': 2, 'text': '第一句原文'}, {'start': 2, 'end': 4, 'text': '第二句原文'}]}
        asset.metadata_json = {**metadata, 'analysis': analysis}; db.commit()
    finally:
        iterator.close()
    job = case['client'].post('/api/v1/jobs/auto-edit', headers={**case['headers'], 'Idempotency-Key': 'subtitle-plan'},
        json={'project_id': case['project'], 'template_id': case['template'], 'asset_ids': [asset['id'] for asset in case['assets']]}).json()
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        assert process_queued_auto_edit_jobs(db)[0]['status'] == 'succeeded'
    finally:
        iterator.close()
    case = {**case, 'source_id': source_id, 'job': job, 'revision': case['client'].post(f"/api/v1/jobs/{job['id']}/editing", headers=case['headers']).json()}
    return case


def _post(case, base, key, action):
    response = save(case, base, key, [action]); assert response.status_code == 201, response.text
    return response.json()


def _cues(revision):
    return [clip for track in revision['editor']['tracks'] if track['type'] == 'subtitle' for clip in track['clips']]


def _originals(case, revision):
    response = case['client'].get(f"/api/v1/editing/{revision['document_id']}/revisions/{revision['revision_id']}/transcripts?limit=1", headers=case['headers'])
    assert response.status_code == 200, response.text
    return response.json()


def _save_cue(cue, **changes):
    return {'op': 'save_subtitle', 'cue_id': cue['id'], 'track_id': 'subtitles', 'text': cue['text'],
        'start': cue['range']['start'], 'end': cue['range']['end'], 'anchor_mode': 'keep', **changes}


def test_capture_before_import_freezes_asr_and_repeated_import_preserves_corrections(subtitle_case):
    case = subtitle_case; first = case['revision']; assert _originals(case, first)['total'] == 2
    second = case['client'].get(f"/api/v1/editing/{first['document_id']}/revisions/{first['revision_id']}/transcripts?skip=1&limit=1", headers=case['headers']).json()
    assert second['total'] == 2 and [row['text'] for row in second['items']] == ['第二句原文']
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        asset = db.get(models.Asset, case['source_id']); raw = json.loads(json.dumps(asset.metadata_json))
        raw['analysis']['transcription']['segments'][0]['text'] = '后来重新识别的不同原文'; asset.metadata_json = raw; db.commit()
        jobs_before = len(list(db.scalars(select(models.Job.id))))
    finally:
        iterator.close()
    imported = _post(case, first, 'import', {'op': 'import_transcript'})
    cues = _cues(imported); assert [cue['text'] for cue in cues] == ['第一句原文', '第二句原文']
    revised = _post(case, imported, 'correct', _save_cue(cues[0], text='第一句人工修订'))
    again = _post(case, revised, 'import-again', {'op': 'import_transcript'})
    assert [cue['text'] for cue in _cues(again)] == ['第一句人工修订', '第二句原文']
    assert _cues(again)[0]['subtitle']['origin']['text'] == '第一句原文'
    assert _cues(again)[0]['subtitle']['edited']
    assert _originals(case, again)['items'][0]['text'] == '第一句原文'
    assert _originals(case, again)['items'][0]['transcript_hash'] == _originals(case, first)['items'][0]['transcript_hash']
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        assert len(list(db.scalars(select(models.Job.id)))) == jobs_before
    finally:
        iterator.close()


def test_old_snapshot_without_asr_captures_current_only_on_explicit_import(edit_case):
    case = edit_case; first = case['revision']; source = first['plan']['selected'][0]['source_asset_id']
    assert _originals(case, first)['total'] == 0
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        asset = db.get(models.Asset, source); raw = json.loads(json.dumps(asset.metadata_json))
        raw['analysis']['transcription'] = {'status': 'ok', 'segments': [{'start': 0, 'end': 1, 'text': '明确读取的旧来源原文'}]}
        asset.metadata_json = raw; db.commit()
    finally:
        iterator.close()
    imported = _post(case, first, 'legacy-import', {'op': 'import_transcript'})
    assert _cues(imported)[0]['text'] == '明确读取的旧来源原文'
    assert _originals(case, first)['total'] == 0
    assert _originals(case, imported)['total'] == 1


def test_split_adjacent_source_parts_merge_one_sentence_and_follow_future_split(subtitle_case):
    case = subtitle_case; first = case['revision']; clip_id = first['plan']['selected'][0]['clip_id']
    split = _post(case, first, 'split-before-asr', {'op': 'split_clip', 'clip_id': clip_id, 'offset_seconds': 1})
    imported = _post(case, split, 'merge-import', {'op': 'import_transcript'})
    cues = _cues(imported); assert len(cues) == 2
    assert cues[0]['range'] == {'start': 0, 'end': 2}
    assert len(cues[0]['subtitle']['anchor']['parts']) == 2
    later = _post(case, imported, 'split-again', {'op': 'split_clip', 'clip_id': clip_id, 'offset_seconds': 0.5})
    assert _cues(later)[0]['range'] == {'start': 0, 'end': 2}
    assert len(_cues(later)[0]['subtitle']['anchor']['parts']) == 3
    assert _cues(later)[0]['text'] == '第一句原文'
    repeat = _post(case, later, 'import-after-split', {'op': 'import_transcript'})
    assert len(_cues(repeat)) == 2, 'new split descendants already covered by a source cue must not re-import duplicates'


def test_trim_moves_anchors_and_removes_only_cues_whose_source_was_removed(subtitle_case):
    case = subtitle_case; first = case['revision']; imported = _post(case, first, 'import-trim', {'op': 'import_transcript'})
    clip_id = first['plan']['selected'][0]['clip_id']
    trimmed = _post(case, imported, 'trim-with-subs', {'op': 'trim_clip', 'clip_id': clip_id, 'source_start': 0, 'source_end': 1.5})
    cues = _cues(trimmed); assert len(cues) == 1
    assert cues[0]['range'] == {'start': 0, 'end': 1.5}
    assert cues[0]['subtitle']['partial']
    assert trimmed['editor']['duration'] == 5.5
    assert _originals(case, trimmed)['total'] == 2
    restored = _post(case, trimmed, 'undo-trim', {'op': 'undo'})
    assert len(_cues(restored)) == 2
    assert timeline(case, imported)['duration'] == 8


def test_deletion_tombstone_is_not_undone_by_new_import(subtitle_case):
    case = subtitle_case; imported = _post(case, case['revision'], 'import-delete', {'op': 'import_transcript'})
    removed = _post(case, imported, 'delete-first', {'op': 'delete_subtitle', 'cue_id': _cues(imported)[0]['id']})
    again = _post(case, removed, 'after-delete-import', {'op': 'import_transcript'})
    assert [cue['text'] for cue in _cues(again)] == ['第二句原文']
    assert len(_cues(imported)) == 2


def test_linked_caption_discontinuity_rejects_reorder_without_overwriting(subtitle_case):
    case = subtitle_case; first = case['revision']; clip_id = first['plan']['selected'][0]['clip_id']
    split = _post(case, first, 'split-order', {'op': 'split_clip', 'clip_id': clip_id, 'offset_seconds': 1})
    imported = _post(case, split, 'import-order', {'op': 'import_transcript'})
    response = save(case, imported, 'discontinuous', [{'op': 'move_clip', 'clip_id': clip_id, 'to_index': 2}])
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == 'subtitle_anchor_discontinuous'
    assert case['client'].get(f"/api/v1/editing/{imported['document_id']}", headers=case['headers']).json()['revision_id'] == imported['revision_id']


def test_absolute_caption_requires_explicit_time_edit_and_other_track_bounds(subtitle_case):
    case = subtitle_case; imported = _post(case, case['revision'], 'import-absolute', {'op': 'import_transcript'})
    cue = _cues(imported)[0]
    response = save(case, imported, 'keep-but-move', [_save_cue(cue, start=0.5)])
    assert response.status_code == 422 and response.json()['detail']['code'] == 'subtitle_anchor_time_change'
    detached = _post(case, imported, 'detach', _save_cue(cue, start=0.5, anchor_mode='absolute'))
    assert not _cues(detached)[0]['subtitle'].get('anchor')
    assert _cues(detached)[0]['range']['start'] == 0.5


@pytest.mark.parametrize('changes,code', [
    ({'start': 0, 'end': 9}, 'subtitle_time_bounds'),
    ({'start': 0.001, 'end': 0.005}, 'subtitle_time_bounds'),
    ({'text': '   '}, 'subtitle_text_invalid'),
    ({'position': {'x': 2000, 'y': 100}}, 'subtitle_position_bounds'),
    ({'start': 1, 'end': 3}, 'subtitle_overlap'),
])
def test_invalid_cue_edit_preserves_old_revision(subtitle_case, changes, code):
    case = subtitle_case; imported = _post(case, case['revision'], 'import-invalid', {'op': 'import_transcript'})
    response = save(case, imported, 'invalid-cue', [_save_cue(_cues(imported)[0], anchor_mode='absolute', **changes)])
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == code
    assert case['client'].get(f"/api/v1/editing/{imported['document_id']}", headers=case['headers']).json()['revision_id'] == imported['revision_id']


def test_export_is_fixed_version_utf8_plain_cue_text_and_tenant_scoped(subtitle_case):
    case = subtitle_case; imported = _post(case, case['revision'], 'import-export', {'op': 'import_transcript'})
    cue = _cues(imported)[0]
    revised = _post(case, imported, 'export-text', _save_cue(cue, text='中文 & <b>literal</b>\n\n下一行', style={'font_size': 56, 'font_color': '#FF0000'}, position={'x': 100, 'y': 1500}))
    prefix = f"/api/v1/editing/{revised['document_id']}/revisions/{revised['revision_id']}"
    srt = case['client'].get(prefix + '/subtitles?format=srt', headers=case['headers']).json()
    assert srt['content'].startswith('1\n00:00:00,000 --> 00:00:02,000\n')
    assert '中文 &amp; &lt;b&gt;literal&lt;/b&gt;\n\u00a0\n下一行' in srt['content']
    assert srt['sha256'] == hashlib.sha256(srt['content'].encode('utf-8')).hexdigest()
    assert srt['timeline_hash'] == revised['timeline_hash'] and srt['cue_count'] == 2
    vtt = case['client'].get(prefix + '/subtitles?format=vtt', headers=case['headers']).json()
    assert vtt['content'].startswith('WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\n')
    assert 'font-size' not in vtt['content']
    newer = _post(case, revised, 'change-after-export', _save_cue(_cues(revised)[0], text='更新文字'))
    assert newer['revision_id'] != revised['revision_id']
    assert case['client'].get(prefix + '/subtitles?format=srt', headers=case['headers']).json() == srt
    other = case['client'].post('/api/v1/tenants', json={'slug': 'subtitle-outsider', 'name': 'Other'}).json()['id']
    for path in ['/subtitles?format=srt', '/transcripts']:
        assert case['client'].get(prefix + path, headers={'X-Tenant-ID': other}).status_code == 404


def test_save_and_delete_are_atomic_idempotent_version_requests(subtitle_case):
    case = subtitle_case; first = case['revision']
    manual = {'op': 'save_subtitle', 'text': '手动添加', 'start': 4, 'end': 5, 'anchor_mode': 'clip',
        'anchor_clip_id': first['plan']['selected'][1]['clip_id']}
    created = _post(case, first, 'manual', manual)
    assert _cues(created)[0]['text'] == '手动添加'
    assert _post(case, first, 'manual', manual)['revision_id'] == created['revision_id']
    response = save(case, created, 'atomic-sub', [{'op': 'delete_subtitle', 'cue_id': _cues(created)[0]['id']}, {'op': 'delete_subtitle', 'cue_id': 'missing'}])
    assert response.status_code == 404
    assert len(_cues(case['client'].get(f"/api/v1/editing/{created['document_id']}", headers=case['headers']).json())) == 1


def test_no_transcript_does_not_trigger_any_new_recognition_job(edit_case):
    case = edit_case; response = _post(case, case['revision'], 'empty-asr', {'op': 'import_transcript'})
    assert not _cues(response)
    assert any('未调用新识别' in warning for warning in response['plan']['warnings'])
    assert case['client'].get(f"/api/v1/editing/{response['document_id']}/revisions/{response['revision_id']}/subtitles", headers=case['headers']).status_code == 422
