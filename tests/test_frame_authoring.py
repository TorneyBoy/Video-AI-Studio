"""Authoring must retain every selected shot without reading beyond its source."""
from types import SimpleNamespace

import pytest

from app.services.auto_edit import AutoEditPlanner, compile_auto_edit_timeline, shot_features_from_assets
from timeline_engine import SlotConstraints, SlotType, TemplateDefinition, TemplateSlot
from timeline_engine.timing import FrameClock, allocate_frames


def _case(windows, duration, fps=30):
    asset = SimpleNamespace(id='source', name='Frame fixture', kind='video/mp4', uri='https://fixture.invalid/frames.mp4',
        metadata_json={'duration': max(end for _, end in windows), 'analysis': {
            'shots': [{'start': start, 'end': end, 'duration': end - start} for start, end in windows]}})
    template = TemplateDefinition(id='frames', version='1', fps=fps, duration=duration,
        slots=[TemplateSlot(name='sequence', type=SlotType.VIDEO, constraints=SlotConstraints(max_items=len(windows)))])
    plan = AutoEditPlanner().plan(template, shot_features_from_assets([asset]))
    return template, asset, [row.__dict__ for row in plan.selected]


def test_three_shots_in_one_tenth_second_keep_one_frame_each():
    template, asset, selected = _case([(0, 1), (1, 3), (3, 6)], 0.1)
    timeline = compile_auto_edit_timeline(template, selected, [asset], timeline_id='three-frames')
    clips = timeline.tracks[0].clips
    assert len(clips) == len(selected) == 3
    assert timeline.duration == 0.1
    assert [clip.source_start for clip in clips] == [0, 1, 3]
    assert [clip.range.end - clip.range.start for clip in clips] == pytest.approx([1 / 30] * 3)
    assert all(clip.source_end - clip.source_start == pytest.approx(1 / 30) for clip in clips)


def test_target_cannot_silently_drop_a_selected_shot():
    template, asset, selected = _case([(0, 1), (1, 2), (2, 3), (3, 4)], 0.1)
    with pytest.raises(ValueError, match='每个入选镜头'):
        compile_auto_edit_timeline(template, selected, [asset], timeline_id='too-short')


def test_subframe_shot_is_not_stretched_to_old_fifty_millisecond_floor():
    template, asset, selected = _case([(0, 0.02), (1, 2)], 1)
    assert [row['asset_id'] for row in selected] == ['source#shot-1']
    with pytest.raises(ValueError, match='不能隐式延长'):
        compile_auto_edit_timeline(template, [{'asset_id': 'source#shot-0'}], [asset], timeline_id='tiny')


@pytest.mark.parametrize('fps', [24, 25, 29.97, 30000 / 1001, 30, 60])
def test_fractional_frame_edges_stay_inside_the_measured_source(fps):
    windows = [(0.017, 1.123), (2.001, 3.876), (5.003, 7.777)]
    template, asset, selected = _case(windows, 3.07, fps)
    timeline = compile_auto_edit_timeline(template, selected, [asset], timeline_id='fractional')
    clock = FrameClock(fps)
    clips = timeline.tracks[0].clips
    assert len(clips) == 3
    assert timeline.duration <= 3.07 + 1e-10
    assert clock.floor(timeline.duration) == clock.floor(3.07)
    assert clips[0].range.start == 0
    for index, (clip, (start, end)) in enumerate(zip(clips, windows)):
        assert start <= clip.source_start < clip.source_end <= end
        assert clip.source_end - clip.source_start == pytest.approx(clip.range.end - clip.range.start)
        assert abs(clip.source_start * fps - round(clip.source_start * fps)) < 1e-8
        if index:
            assert clip.range.start == clips[index - 1].range.end
    assert clips[-1].range.end == timeline.duration
    again = compile_auto_edit_timeline(template, selected, [asset], timeline_id='fractional')
    assert again == timeline


def test_override_must_fit_measured_shot_before_frame_rounding():
    template, asset, selected = _case([(1, 2)], 0.5)
    selected[0].update(source_start=0.9, source_end=2)
    with pytest.raises(ValueError, match='exceeds'):
        compile_auto_edit_timeline(template, selected, [asset], timeline_id='escape')


@pytest.mark.parametrize('capacities', [[1], [1, 1, 1], [1, 1000, 1], [7, 3, 2, 1], [100, 200, 300]])
def test_allocation_conserves_frames_with_mixed_source_capacities(capacities):
    for total in sorted({len(capacities), (len(capacities) + sum(capacities)) // 2, sum(capacities)}):
        result = allocate_frames(capacities, total)
        assert sum(result) == total
        assert len(result) == len(capacities)
        assert all(1 <= value <= capacity for value, capacity in zip(result, capacities))


@pytest.mark.parametrize('fps', [0, -1, 241, float('nan'), float('inf')])
def test_invalid_frame_rates_fail_before_any_authoring(fps):
    with pytest.raises(ValueError):
        FrameClock(fps)
