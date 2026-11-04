from timeline_engine import AssetType, SlotConstraints, SlotType, TemplateDefinition, TemplateSlot
from types import SimpleNamespace
from itertools import permutations

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


def test_required_slots_share_candidates_without_greedy_starvation():
    template = TemplateDefinition.model_validate({'id': 'global', 'version': '1', 'slots': [
        {'name': 'opening', 'type': 'video', 'constraints': {'max_items': 2}},
        {'name': 'product', 'type': 'video', 'constraints': {'required_metadata': ['product']}}]})
    shots = [ShotFeature(asset_id='product', duration=4, quality=1, metadata={'product': True}),
             ShotFeature(asset_id='other', duration=4, quality=.5)]
    plan = AutoEditPlanner().plan(template, shots)
    assert [(row.slot, row.asset_id) for row in plan.selected] == [('opening', 'other'), ('product', 'product')]
    assert not plan.missing_required_slots
    assert AutoEditPlanner().plan(template, list(reversed(shots))) == plan


def test_optional_slot_before_required_cannot_take_its_only_source():
    template = TemplateDefinition.model_validate({'id': 'required-first', 'version': '1', 'slots': [
        {'name': 'decoration', 'type': 'video', 'required': False, 'constraints': {'max_items': 2}},
        {'name': 'hero', 'type': 'video', 'constraints': {'required_metadata': ['hero']}}]})
    plan = AutoEditPlanner().plan(template, [
        ShotFeature(asset_id='a', duration=2, quality=1, metadata={'hero': True}),
        ShotFeature(asset_id='b', duration=2, quality=.5)])
    assert [(row.slot, row.asset_id) for row in plan.selected] == [('decoration', 'b'), ('hero', 'a')]
    assert not plan.missing_required_slots


def test_multiple_required_items_are_reassigned_as_one_global_matching():
    template = TemplateDefinition.model_validate({'id': 'quotas', 'version': '1', 'slots': [
        {'name': 'sequence', 'type': 'video', 'constraints': {'min_items': 2, 'max_items': 2}},
        {'name': 'logo', 'type': 'video', 'constraints': {'required_metadata': ['logo']}}]})
    plan = AutoEditPlanner().plan(template, [
        ShotFeature(asset_id='a', duration=2, quality=1, metadata={'logo': True}),
        ShotFeature(asset_id='b', duration=2, quality=.8), ShotFeature(asset_id='c', duration=2, quality=.6)])
    assert [(row.slot, row.asset_id) for row in plan.selected] == [('sequence', 'b'), ('sequence', 'c'), ('logo', 'a')]
    assert not plan.missing_required_slots


def test_unfillable_optional_minimum_rolls_back_its_entire_assignment():
    template = TemplateDefinition.model_validate({'id': 'optional', 'version': '1', 'slots': [
        {'name': 'hero', 'type': 'video'},
        {'name': 'extras', 'type': 'video', 'required': False, 'constraints': {'min_items': 2, 'max_items': 3}}]})
    plan = AutoEditPlanner().plan(template, [ShotFeature(asset_id='a', duration=2, quality=1),
                                           ShotFeature(asset_id='b', duration=2, quality=.5)])
    assert [(row.slot, row.asset_id) for row in plan.selected] == [('hero', 'a')]
    assert not plan.missing_required_slots


def test_impossible_required_quotas_keep_honest_missing_slots_and_unique_sources():
    template = TemplateDefinition.model_validate({'id': 'missing', 'version': '1', 'slots': [
        {'name': 'one', 'type': 'video'}, {'name': 'two', 'type': 'video'}]})
    shot = ShotFeature(asset_id='only', duration=2)
    plan = AutoEditPlanner().plan(template, [shot, shot])
    assert len(plan.selected) == 1
    assert plan.missing_required_slots == ('two',)


def test_unbounded_slot_keeps_all_eligible_sources_in_stable_score_order():
    template = TemplateDefinition.model_validate({'id': 'unbounded', 'version': '1', 'slots': [
        {'name': 'sequence', 'type': 'video', 'constraints': {'max_items': None}}]})
    plan = AutoEditPlanner().plan(template, [ShotFeature(asset_id='a', duration=2, quality=.2),
        ShotFeature(asset_id='b', duration=2, quality=.8), ShotFeature(asset_id='c', duration=2, quality=.5)])
    assert [row.asset_id for row in plan.selected] == ['b', 'c', 'a']


def test_required_slot_feasibility_matches_exhaustive_small_assignment_oracle():
    template = TemplateDefinition.model_validate({'id': 'oracle', 'version': '1', 'slots': [
        {'name': 'slot' + str(index), 'type': 'video', 'constraints': {'required_metadata': ['slot' + str(index)]}}
        for index in range(3)]})
    for graph in range(1 << 9):
        edges = [[bool(graph & (1 << (slot * 3 + shot))) for shot in range(3)] for slot in range(3)]
        feasible = any(all(edges[slot][shot] for slot, shot in enumerate(order))
                       for order in permutations(range(3)))
        shots = [ShotFeature(asset_id=str(shot), duration=2,
            metadata={'slot' + str(slot): True for slot in range(3) if edges[slot][shot]}) for shot in range(3)]
        plan = AutoEditPlanner().plan(template, shots)
        assert len({row.asset_id for row in plan.selected}) == len(plan.selected)
        assert (not plan.missing_required_slots) == feasible, graph
