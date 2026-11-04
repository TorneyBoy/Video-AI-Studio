import pytest
from timeline_engine import Timeline

from app.services.media_quality import (
    QualityFinding, assess_render_quality, assert_renderer_quality, audio_tail_finding,
    quality_timeline_hash, renderer_findings, validate_render_quality, visibility_timing_findings,
)
from app.services.providers import ProviderExecutionError


URI = 'oss://test/tenants/t/render/output.mp4'
SHA = 'a' * 64


def timeline():
    return Timeline.model_validate({'id': 'test', 'fps': 30, 'duration': 5,
        'resolution': {'width': 480, 'height': 832},
        'assets': [{'id': 'video', 'type': 'video', 'uri': 'oss://test/tenants/t/source.mp4'},
                   {'id': 'logo', 'type': 'image', 'uri': 'oss://test/tenants/t/logo.png'}],
        'tracks': [{'id': 'primary', 'type': 'video', 'clips': [
            {'id': 'source', 'asset_id': 'video', 'range': {'start': 0, 'end': 5}}]},
            {'id': 'overlay', 'type': 'overlay', 'clips': [
                {'id': 'logo', 'asset_id': 'logo', 'range': {'start': 0.5, 'end': 4.5}}]}]})


def report():
    return {'schema_version': 'media-quality/1', 'status': 'passed', 'object_uri': URI,
        'output_sha256': SHA, 'size_bytes': 100, 'width': 480, 'height': 832,
        'duration_seconds': 5, 'fps': 30, 'has_audio': True, 'video_codec': 'h264', 'container': 'mp4',
        'inspector': 'test-hosted-probe', 'inspector_version': '1'}


def test_known_ice_image_defect_is_located_and_does_not_restrict_other_renderers():
    value = timeline()
    items = renderer_findings(value, renderer_model_id='aliyun-ice-cloud-editing')
    assert len(items) == 1
    item = QualityFinding.model_validate(items[0])
    assert (item.code, item.severity, item.dimension) == ('ice_image_end_unverified', 'error', 'technical')
    assert (item.track_id, item.clip_id, item.start_seconds, item.end_seconds) == ('overlay', 'logo', 0.5, 4.5)
    assert item.evidence.source == 'acceptance_record'
    assert renderer_findings(value, renderer_model_id='another-renderer') == []
    with pytest.raises(ProviderExecutionError) as error:
        assert_renderer_quality(value, renderer_model_id='aliyun-ice-cloud-editing')
    assert error.value.code == 'renderer_quality_unverified'
    assert error.value.details['findings'] == items
    value.tracks[-1].muted = True
    assert renderer_findings(value, renderer_model_id='aliyun-ice-cloud-editing') == []
    value.tracks[-1].muted = False
    value.tracks[-1].clips[0].opacity = 0
    assert renderer_findings(value, renderer_model_id='aliyun-ice-cloud-editing') == []


def test_static_text_bounds_only_report_known_position_not_invented_glyph_measurements():
    value = timeline().model_dump(mode='json')
    value['tracks'] = [value['tracks'][0], {'id': 'text', 'type': 'subtitle', 'clips': [
        {'id': 'caption', 'text': '中文安全区', 'range': {'start': 1, 'end': 3}, 'placement': {'x': 480, 'y': 10}}]}]
    items = renderer_findings(value, renderer_model_id='other')
    assert items[0]['code'] == 'subtitle_origin_outside_canvas' and items[0]['severity'] == 'error'
    assert items[0]['evidence']['source'] == 'canonical_timeline'
    value['tracks'][1]['clips'][0]['placement'] = {'x': 20, 'y': 30, 'width': 470, 'height': 10}
    assert renderer_findings(value, renderer_model_id='other')[0]['severity'] == 'warning'
    value['tracks'][1]['clips'][0]['placement'] = {'x': 20, 'y': 30}
    assert renderer_findings(value, renderer_model_id='other') == []  # No assertion of glyph/legibility pass.


def test_quality_location_preserves_existing_canonical_ids_without_new_arbitrary_caps():
    value = timeline()
    value.tracks[-1].id = 'track-' + 'x' * 210
    value.tracks[-1].clips[0].id = 'clip-' + 'x' * 210
    finding = renderer_findings(value, renderer_model_id='aliyun-ice-cloud-editing')[0]
    assert finding['track_id'] == value.tracks[-1].id
    assert finding['clip_id'] == value.tracks[-1].clips[0].id


def test_probe_success_is_scoped_and_does_not_claim_visual_or_audio_content():
    result = assess_render_quality(report(), timeline=timeline(), output_uri=URI)
    assert result['scope'] == 'structural_probe' and result['status'] == 'passed'
    assert result['assessed_dimensions'] == ['technical']
    assert result['not_assessed_dimensions'] == ['facts', 'narrative', 'visual', 'audio']
    assert result['findings'] == []
    assert validate_render_quality(report(), timeline=timeline(), output_uri=URI) == report()


def test_probe_mismatches_are_separate_findings_without_echoing_private_urls():
    value = report()
    value.update(object_uri=URI + '?private-token=fixture', width=720, fps=24, duration_seconds=2)
    result = assess_render_quality(value, timeline=timeline(), output_uri=URI)
    assert result['status'] == 'failed'
    assert {item['code'] for item in result['findings']} == {
        'output_object_binding', 'output_width', 'output_duration', 'output_frame_rate'}
    assert 'private-token' not in repr(result)
    assert all(item['start_seconds'] == 0 and item['end_seconds'] == 5 for item in result['findings'])
    with pytest.raises(ProviderExecutionError) as error:
        validate_render_quality(value, timeline=timeline(), output_uri=URI)
    assert error.value.details['assessment'] == result


@pytest.mark.parametrize('field,value', [('width', True), ('height', '832'), ('has_audio', 'true'), ('fps', float('nan'))])
def test_untrusted_probe_values_cannot_be_coerced_to_success(field, value):
    changed = report(); changed[field] = value
    result = assess_render_quality(changed, timeline=timeline(), output_uri=URI)
    assert result['status'] == 'failed' and result['findings'][0]['code'] == 'probe_report_invalid'


def visibility(value):
    return {'schema_version': 'clip-visibility/1', 'output_sha256': SHA,
        'timeline_hash': quality_timeline_hash(value), 'clip_id': 'logo', 'fps': value.fps,
        'unobscured': True, 'checked_frames': list(range(value.frame_count)),
        'visible_frames': list(range(15, 135)), 'evidence_ref': 'local-inspector/full-frame-metrics'}


def test_full_frame_evidence_locates_the_actual_one_frame_image_overrun():
    value = timeline(); observation = visibility(value)
    assert visibility_timing_findings(observation, timeline=value, output_sha256=SHA) == []
    observation['visible_frames'].append(135)
    findings = visibility_timing_findings(observation, timeline=value, output_sha256=SHA)
    assert len(findings) == 1
    item = findings[0]
    assert item['code'] == 'clip_visible_outside_interval'
    assert (item['start_seconds'], item['end_seconds']) == (4.5, 136 / 30)
    assert item['evidence']['source'] == 'frame_measurement' and not item['evidence']['expected']


@pytest.mark.parametrize('change', ['wrong_hash', 'wrong_timeline', 'wrong_fps', 'sparse', 'duplicate', 'negative', 'boolean', 'occluded', 'implied_unobscured'])
def test_partial_or_misbound_frame_measurements_never_count_as_exact_quality(change):
    value = timeline(); observation = visibility(value)
    if change == 'wrong_hash': observation['output_sha256'] = 'b' * 64
    elif change == 'wrong_timeline': observation['timeline_hash'] = 'b' * 64
    elif change == 'wrong_fps': observation['fps'] = 25
    elif change == 'sparse': observation['checked_frames'] = [0, 15, 134, 135, 149]
    elif change == 'duplicate': observation['visible_frames'].append(134)
    elif change == 'negative': observation['visible_frames'].insert(0, -1)
    elif change == 'boolean': observation['visible_frames'][0] = True
    elif change == 'occluded': observation['unobscured'] = False
    elif change == 'implied_unobscured': observation['unobscured'] = 1
    with pytest.raises(ValueError):
        visibility_timing_findings(observation, timeline=value, output_sha256=SHA)


def test_declared_audio_presence_is_separate_from_tail_measurements():
    finding = audio_tail_finding(expected_end_seconds=5.2, observed_end_seconds=5.136,
        tolerance_seconds=0.02, evidence_ref='audio-inspector/full-decode', track_id='voice')
    assert finding['code'] == 'audio_stream_tail_short' and finding['dimension'] == 'audio'
    assert (finding['start_seconds'], finding['end_seconds']) == (5.136, 5.2)
    assert '不直接证明丢字' in finding['message']
    assert audio_tail_finding(expected_end_seconds=5.2, observed_end_seconds=5.136,
        tolerance_seconds=0.064, evidence_ref='audio-inspector/full-decode') is None


@pytest.mark.parametrize('tolerance', [float('nan'), -1, 0.251, True])
def test_audio_tail_policy_must_be_bounded_and_explicit(tolerance):
    with pytest.raises(ValueError):
        audio_tail_finding(expected_end_seconds=5.2, observed_end_seconds=5.136,
            tolerance_seconds=tolerance, evidence_ref='inspector')
