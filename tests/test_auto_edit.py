from timeline_engine import AssetType, SlotConstraints, SlotType, TemplateDefinition, TemplateSlot
from types import SimpleNamespace

import pytest

from app.services.auto_edit import AutoEditPlanner, ShotFeature, compile_auto_edit_timeline, shot_features_from_assets


def test_auto_edit_plan_is_scored_and_explainable():
    template = TemplateDefinition(
        id="social",
        version="1",
        slots=[TemplateSlot(name="hook", type=SlotType.VIDEO, constraints=SlotConstraints(allowed_asset_types=[AssetType.VIDEO]))],
    )
    plan = AutoEditPlanner().plan(template, [
        ShotFeature(asset_id="b", duration=2, quality=0.9),
        ShotFeature(asset_id="a", duration=2, quality=0.5, motion=1.0),
    ])
    assert plan.selected[0].asset_id == "a"
    assert plan.selected[0].rationale["motion"] == 0.25
    assert not plan.warnings


def test_missing_detector_features_are_not_quality_zero_and_shots_keep_natural_order():
    asset = SimpleNamespace(id='source', kind='video/mp4', metadata_json={'analysis': {'shots': [
        {'start': i, 'end': i + 1, 'duration': 1, 'confidence': 0.9} for i in range(12)]}})
    template = TemplateDefinition(id='all', version='1', slots=[TemplateSlot(name='sequence', type=SlotType.VIDEO,
        constraints=SlotConstraints(allowed_asset_types=[AssetType.VIDEO], max_items=12))])
    plan = AutoEditPlanner().plan(template, list(reversed(shot_features_from_assets([asset]))))
    assert [row.asset_id for row in plan.selected] == [f'source#shot-{i}' for i in range(12)]
    assert all(not row.rationale and row.selection_basis == 'source_order' for row in plan.selected)
    assert any('没有画质' in warning for warning in plan.warnings)


@pytest.mark.parametrize('requested,actual,source_end', [(2, 2, 4), (4, 4, 6), (12, 4, 6)])
def test_target_duration_trims_without_stretching_past_available_source(requested, actual, source_end):
    asset = SimpleNamespace(id='source', name='clip', kind='video/mp4', uri='oss://fixture/source',
        metadata_json={'duration': 10, 'analysis': {'shots': [{'start': 2, 'end': 6, 'duration': 4}]}})
    template = TemplateDefinition(id='short', version='1', duration=requested, slots=[TemplateSlot(name='hook', type=SlotType.VIDEO)])
    plan = AutoEditPlanner().plan(template, shot_features_from_assets([asset]))
    timeline = compile_auto_edit_timeline(template, [row.__dict__ for row in plan.selected], [asset], timeline_id='test')
    clip = timeline.tracks[0].clips[0]
    assert timeline.duration == actual
    assert clip.source_start == 2 and clip.source_end == source_end
    assert clip.source_end - clip.source_start == pytest.approx(clip.range.end - clip.range.start)
    assert any('不补帧或变速' in warning for warning in plan.warnings) == (requested > 4)
