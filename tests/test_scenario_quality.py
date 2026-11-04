from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.scenario_schemas import ScenarioBenchmark, ScenarioProfile, ScenarioQualityReview
from app.services.auto_edit import (AutoEditPlanner, ShotFeature, assert_scenario_timeline_sources,
                                   compile_auto_edit_timeline, shot_features_from_assets)
from app.services.scenario_profiles import (freeze_template_scenario, get_scenario_profile, list_scenario_profiles,
                                            resolve_scenario_binding, scenario_binding)
from app.services.scenario_quality import assess_scenario_quality, benchmark_summary, tuning_cases
from timeline_engine import TemplateDefinition


def template(scenario="product_showcase", **values):
    return TemplateDefinition.model_validate({"id": "test", "version": "1", "slots": [{"name": "main", "type": "video"}],
        "metadata": {"scenario": {"id": scenario, "version": "1"}}, **values})


def measured(id, quality, semantic, **extra):
    return ShotFeature(asset_id=id, duration=2, quality=quality, semantic_match=semantic,
                       available_features=("quality", "semantic_match"), **extra)


def review(profile="product_showcase"):
    return {"scenario": scenario_binding(get_scenario_profile(profile)), "output_sha256": "a" * 64,
            "reviewer_id": "owner", "rubric_version": "rubric-1",
            "dimensions": [{"dimension": d, "status": "passed", "score": 4, "evidence_ids": ["evidence-" + d]}
                           for d in ("facts", "technical", "narrative", "visual", "audio")]}


def case(id="case-a", split="tuning", sha="a", group="group-a", status="ready"):
    return {"id": id, "scenario": scenario_binding(get_scenario_profile("product_showcase")), "split": split,
            "status": status, "source_group_id": group, "source_sha256": [sha * 64],
            "reference_artifact_id": "human-reference", "requirements": ["保留商品形状和颜色"]}


def benchmark(*cases):
    return {"id": "pilot", "version": "1", "cases": list(cases)}


def test_profiles_are_independent_versioned_draft_definitions_not_acceptance():
    profiles = list_scenario_profiles()
    assert {row["id"] for row in profiles} == {"product_showcase", "spoken_explainer"}
    assert all(row["validation_status"] == "draft" and row["acceptance_report"] is None for row in profiles)
    original = get_scenario_profile("product_showcase")
    original.selection_weights["quality"] = 0
    assert get_scenario_profile("product_showcase").selection_weights["quality"] == .45
    assert get_scenario_profile("product_showcase").content_hash != original.content_hash
    assert get_scenario_profile("spoken_explainer").preserve_speech_meaning


@pytest.mark.parametrize("id,version", [("missing", "1"), ("product_showcase", "missing")])
def test_unknown_scene_or_version_rejected(id, version):
    with pytest.raises(ValueError):
        get_scenario_profile(id, version)


@pytest.mark.parametrize("change", [
    {"selection_weights": {"quality": True}}, {"selection_weights": {"quality": "1"}},
    {"selection_weights": {"quality": float("nan")}}, {"selection_weights": {"quality": .4}},
    {"selection_weights": {"untrusted_feature": 1}}, {"minimum_scores": {"quality": -1}},
    {"required_features": ["semantic_match", "semantic_match"]}, {"preserve_facts": False},
    {"validation_status": "verified"}, {"arbitrary": "unknown"}])
def test_scene_definition_refuses_invalid_or_unverified_policy(change):
    value = get_scenario_profile("product_showcase").model_dump(mode="json")
    with pytest.raises(ValidationError):
        ScenarioProfile.model_validate({**value, **change})


def test_frozen_scene_rejects_content_or_prompt_drift_and_missing_version():
    frozen = freeze_template_scenario(template())
    bound = frozen.metadata["scenario"]
    assert bound == scenario_binding(get_scenario_profile("product_showcase"))
    for field, wrong in (("content_hash", "0" * 64), ("prompt_version", "changed")):
        with pytest.raises(ValueError):
            resolve_scenario_binding({**bound, field: wrong})
    with pytest.raises(ValueError):
        resolve_scenario_binding({"id": "product_showcase"})


def test_scene_selection_distinguishes_visual_evidence_from_spoken_semantic_priority():
    shots = [measured("a-sharp", 1, .6, motion=1, face_sharpness=1, emotion=1), measured("b-relevant", .4, .9)]
    product = AutoEditPlanner().plan(template(), shots)
    spoken = AutoEditPlanner().plan(template("spoken_explainer"), shots)
    assert product.selected[0].asset_id == "a-sharp"
    assert spoken.selected[0].asset_id == "b-relevant"
    assert set(product.selected[0].rationale) == {"quality", "semantic_match"}
    assert product.scenario == scenario_binding(get_scenario_profile("product_showcase"))
    assert all("motion" not in row.rationale and "emotion" not in row.rationale for row in product.selected)


@pytest.mark.parametrize("features", [None, (), ("quality",)])
def test_explicit_scene_never_uses_missing_measurements_as_measured_zero(features):
    shot = ShotFeature(asset_id="source", duration=2, quality=1, semantic_match=1, available_features=features)
    plan = AutoEditPlanner().plan(template(), [shot])
    assert not plan.selected and plan.missing_required_slots == ("main",)
    assert any("不能冒充零分" in value for value in plan.warnings)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_explicit_scene_rejects_invalid_observed_measurements(score):
    plan = AutoEditPlanner().plan(template(), [measured("source", score, .8)])
    assert not plan.selected


def test_scene_weights_cannot_be_silently_replaced_by_planner_override():
    with pytest.raises(ValueError, match="versioned"):
        AutoEditPlanner({"motion": 1}).plan(template(), [measured("source", .8, .8)])


def test_legacy_template_and_unobserved_source_order_behavior_remain_available():
    value = template().model_copy(update={"metadata": {}})
    plan = AutoEditPlanner().plan(value, [ShotFeature(asset_id="source", duration=2, available_features=())])
    assert len(plan.selected) == 1 and plan.selected[0].selection_basis == "source_order" and plan.scenario is None


def test_scenario_binding_enters_compiled_timeline_without_inventing_analysis():
    value = freeze_template_scenario(template(duration=1))
    asset = SimpleNamespace(id="source", name="source", kind="video", uri="oss://fixture/source", metadata_json={"duration": 2,
        "analysis": {"shots": [{"start": 0, "end": 2, "duration": 2, "quality": .8, "semantic_match": .9}]}})
    before = deepcopy(asset.metadata_json)
    timeline = compile_auto_edit_timeline(value, [{"slot": "main", "asset_id": "source#shot-0"}], [asset], timeline_id="test")
    assert timeline.metadata["scenario"] == value.metadata["scenario"]
    assert timeline.tracks[0].clips[0].source_end == 1
    assert asset.metadata_json == before


def test_spoken_scene_does_not_arbitrarily_cut_a_long_utterance_to_target_duration():
    asset = SimpleNamespace(id="source", name="source", kind="video", uri="oss://fixture/source", metadata_json={"duration": 5})
    with pytest.raises(ValueError, match="完整讲话"):
        compile_auto_edit_timeline(template("spoken_explainer", duration=3),
            [{"slot": "main", "asset_id": "source#shot-0"}], [asset], timeline_id="test")


def spoken_asset():
    uri = "oss://test-bucket/source.mp4"
    value = {"schema_version": "media-analysis/2", "provider": "fixture-analysis", "source": {"uri": uri},
        "media": {"status": "ok", "duration_seconds": 3.0, "size_bytes": 100, "format": "mp4"},
        "video": {"width": 480, "height": 832}, "audio": {"sample_rate": 48000, "channels": 1},
        "shots": [{"start": 0.0, "end": 3.0, "duration": 3.0, "quality": .8, "semantic_match": .9}],
        "shot_analysis": {"status": "ok", "provider": "fixture-cuts", "version": "1"},
        "transcription": {"status": "ok", "provider": "fixture-asr", "version": "1",
            "word_alignment": {"status": "ok", "provider": "fixture-aligner", "version": "1"},
            "segments": [{"id": "sentence-0", "start": .5, "end": 1.5, "text": "Complete sentence.", "boundary_safe": True}],
            "words": [{"id": "word-a", "segment_id": "sentence-0", "start": .55, "end": .8, "text": "Complete"},
                      {"id": "word-b", "segment_id": "sentence-0", "start": 1.0, "end": 1.4, "text": "sentence."}]}}
    return SimpleNamespace(id="source", name="source", kind="video/mp4", uri=uri,
                           metadata_json={"duration": 3, "analysis": value})


def test_spoken_planner_and_compiler_consume_source_bound_complete_speech_units():
    asset = spoken_asset()
    profile = get_scenario_profile("spoken_explainer")
    features = shot_features_from_assets([asset], scenario=profile)
    assert features[0].asset_id == "source#speech-0" and features[0].duration == 1
    plan = AutoEditPlanner().plan(template(profile.id, duration=1), features)
    assert plan.selected[0].asset_id == "source#speech-0"
    timeline = compile_auto_edit_timeline(template(profile.id, duration=1),
        [row.__dict__ for row in plan.selected], [asset], timeline_id="speech-test")
    clip = timeline.tracks[0].clips[0]
    assert clip.source_start == .5 and clip.source_end == 1.5
    assert clip.metadata["speech_unit"]["word_ids"] == ["word-a", "word-b"]
    assert "text" not in clip.metadata["speech_unit"]
    assert timeline.metadata["scenario"]["id"] == "spoken_explainer"


def test_spoken_units_keep_source_order_after_quality_based_selection():
    asset = spoken_asset()
    raw = asset.metadata_json["analysis"]
    raw["transcription"]["segments"] = []
    raw["transcription"]["words"] = []
    for n in range(3):
        raw["transcription"]["segments"].append({"id": f"s-{n}", "start": float(n), "end": float(n + 1),
            "text": f"Sentence {n}.", "boundary_safe": True})
        raw["transcription"]["words"].append({"id": f"w-{n}", "segment_id": f"s-{n}",
            "start": n + .1, "end": n + .8, "text": "Sentence."})
    features = shot_features_from_assets([asset], scenario=get_scenario_profile("spoken_explainer"))
    features = [replace(item, quality=score) for item, score in zip(features, (.4, .95, .7))]
    value = template("spoken_explainer", slots=[{"name": "sequence", "type": "video", "constraints": {"max_items": 3}}], duration=3)
    plan = AutoEditPlanner().plan(value, list(reversed(features)))
    assert [row.asset_id for row in plan.selected] == ["source#speech-0", "source#speech-1", "source#speech-2"]
    timeline = compile_auto_edit_timeline(value, [row.__dict__ for row in plan.selected], [asset], timeline_id="ordered")
    assert [clip.source_start for clip in timeline.tracks[0].clips] == [0, 1, 2]


@pytest.mark.parametrize("change", ["trim", "word_evidence", "source_id", "scene_hash", "scene_missing", "speed", "measurement"])
def test_final_scene_guard_rejects_manual_bypass_or_copied_stale_evidence(change):
    asset = spoken_asset(); value = template("spoken_explainer", duration=1)
    plan = AutoEditPlanner().plan(value, shot_features_from_assets([asset], scenario=get_scenario_profile("spoken_explainer")))
    original = compile_auto_edit_timeline(value, [row.__dict__ for row in plan.selected], [asset], timeline_id="guard")
    canonical = original.model_copy(deep=True)
    clip = canonical.tracks[0].clips[0]
    if change == "trim":
        clip.source_end = 1.4; clip.range.end = .9
    elif change == "word_evidence":
        clip.metadata["speech_unit"]["word_ids"] = ["not-this-utterance"]
    elif change == "source_id":
        clip.metadata["source_shot_id"] = "other#speech-0"
    elif change == "scene_hash":
        canonical.metadata["scenario"]["content_hash"] = "0" * 64
    elif change == "scene_missing":
        canonical.metadata.pop("scenario")
    elif change == "speed":
        clip.range.end = .5
    else:
        asset.metadata_json["analysis"]["shots"][0].pop("semantic_match")
    with pytest.raises(ValueError):
        assert_scenario_timeline_sources(canonical, [asset], value)


@pytest.mark.parametrize("change", ["safe_flag", "words", "alignment", "wrong_source", "picture_quality", "semantic_score"])
def test_spoken_pipeline_does_not_invent_speech_boundaries_or_visual_semantic_scores(change):
    asset = spoken_asset()
    raw = asset.metadata_json["analysis"]
    if change == "safe_flag":
        raw["transcription"]["segments"][0].pop("boundary_safe")
    elif change == "words":
        raw["transcription"]["words"] = []
    elif change == "alignment":
        raw["transcription"].pop("word_alignment")
    elif change == "wrong_source":
        raw["source"]["uri"] += "-different"
    else:
        raw["shots"][0].pop("quality" if change == "picture_quality" else "semantic_match")
    features = shot_features_from_assets([asset], scenario=get_scenario_profile("spoken_explainer"))
    assert not AutoEditPlanner().plan(template("spoken_explainer"), features).selected


@pytest.mark.parametrize("change", ["short_target", "selected_subwindow", "frame_cuts_word", "missing_unit"])
def test_spoken_complete_unit_cannot_be_shortened_reframed_or_fabricated(change):
    asset = spoken_asset()
    selected = {"slot": "main", "asset_id": "source#speech-0"}
    value = template("spoken_explainer", duration=1)
    if change == "short_target":
        value = template("spoken_explainer", duration=.5)
    elif change == "selected_subwindow":
        selected.update(source_start=.6, source_end=1.4)
    elif change == "frame_cuts_word":
        segment = asset.metadata_json["analysis"]["transcription"]["segments"][0]
        segment["end"] = 1.501
        asset.metadata_json["analysis"]["transcription"]["words"][-1]["end"] = 1.501
    else:
        selected["asset_id"] = "source#speech-20"
    with pytest.raises(ValueError):
        compile_auto_edit_timeline(value, [selected], [asset], timeline_id="unsafe")


def test_probe_only_real_asset_remains_unready_for_scene_semantic_selection():
    asset = SimpleNamespace(id="real-cup", kind="video/mp4", metadata_json={"duration": 5,
        "analysis": {"shots": [], "shot_analysis": {"status": "not_requested"}}})
    plan = AutoEditPlanner().plan(template(), shot_features_from_assets([asset]))
    assert not plan.selected and plan.missing_required_slots == ("main",)
    assert asset.metadata_json["analysis"]["shots"] == []


def test_review_passes_only_all_reviewed_dimensions_and_still_not_release_admission():
    result = assess_scenario_quality(review())
    assert result["verdict"] == "passed" and result["profile_validation_status"] == "draft"
    assert result["commercial_or_release_admission"] is False


@pytest.mark.parametrize("severity,verdict", [("error", "failed"), ("warning", "needs_review")])
def test_measured_renderer_findings_cannot_be_erased_by_passed_human_dimensions(severity, verdict):
    finding = {"code": "image_end_mismatch", "severity": severity, "dimension": "technical",
               "message": "图片结束边界不符合时间线", "evidence": {"source": "frame_measurement",
               "reference": "inspector-result-1", "expected": 30, "observed": 31, "unit": "frames"}}
    result = assess_scenario_quality(review(), technical_findings=[finding])
    assert result["verdict"] == verdict and result["findings"][0]["evidence"]["observed"] == 31


@pytest.mark.parametrize("failed", ["facts", "technical", "narrative", "visual", "audio"])
def test_quality_dimension_failure_cannot_be_hidden_by_other_high_scores(failed):
    value = review()
    for row in value["dimensions"]:
        row["score"] = 5
        if row["dimension"] == failed:
            row["status"] = "failed"
    result = assess_scenario_quality(value)
    assert result["verdict"] == "failed" and result["failed_dimensions"] == [failed]


def test_unreviewed_or_missing_dimensions_require_review_not_an_invented_zero():
    value = review()
    value["dimensions"] = value["dimensions"][:-1]
    value["dimensions"][0] = {"dimension": "facts", "status": "unreviewed"}
    result = assess_scenario_quality(value)
    assert result["verdict"] == "needs_review" and set(result["missing_dimensions"]) == {"facts", "audio"}
    assert result["dimensions"][0]["score"] is None


def test_not_applicable_audio_requires_a_reason_and_is_forbidden_for_spoken():
    value = review()
    value["dimensions"][-1] = {"dimension": "audio", "status": "not_applicable", "note": "已确认无声商品展示",
                                "evidence_ids": ["confirmed-brief"]}
    assert assess_scenario_quality(value)["verdict"] == "passed"
    value["scenario"] = scenario_binding(get_scenario_profile("spoken_explainer"))
    assert assess_scenario_quality(value)["verdict"] == "failed"


@pytest.mark.parametrize("mutate", ["duplicate", "no_evidence", "unknown_score", "spoof_hash", "extra"])
def test_quality_review_rejects_ambiguous_or_false_evidence_shape(mutate):
    value = review()
    if mutate == "duplicate":
        value["dimensions"][-1] = deepcopy(value["dimensions"][0])
    elif mutate == "no_evidence":
        value["dimensions"][0]["evidence_ids"] = []
    elif mutate == "unknown_score":
        value["dimensions"][0].update(status="unreviewed")
    elif mutate == "spoof_hash":
        value["output_sha256"] = "not-a-sha256"
    else:
        value["executed_by_provider"] = True
    with pytest.raises(ValidationError):
        ScenarioQualityReview.model_validate(value)


def test_draft_benchmark_cases_do_not_claim_real_collected_projects():
    value = case(status="planned")
    value.update(source_sha256=[], reference_artifact_id=None)
    result = benchmark_summary(benchmark(value))
    assert result["case_count"] == result["planned_cases"] == 1 and result["ready_cases"] == 0
    assert result["real_media_acceptance"] is False
    with pytest.raises(ValueError):
        tuning_cases(benchmark(value), ["case-a"])


@pytest.mark.parametrize("field", ["content_hash", "prompt_version"])
def test_quality_review_and_ready_benchmark_require_frozen_scene_evidence(field):
    value = review()
    value["scenario"].pop(field)
    with pytest.raises(ValidationError):
        ScenarioQualityReview.model_validate(value)
    sample = case()
    sample["scenario"].pop(field)
    with pytest.raises(ValidationError):
        ScenarioBenchmark.model_validate(benchmark(sample))


@pytest.mark.parametrize("changes", [{"source_sha256": []}, {"reference_artifact_id": None}])
def test_ready_benchmark_requires_real_sources_and_reference(changes):
    with pytest.raises(ValidationError):
        ScenarioBenchmark.model_validate(benchmark({**case(), **changes}))


@pytest.mark.parametrize("same_hash,same_group", [(True, False), (False, True), (True, True)])
def test_benchmark_prevents_holdout_leakage_even_after_reencoding(same_hash, same_group):
    other = case("case-b", "holdout", sha="a" if same_hash else "b", group="group-a" if same_group else "group-b")
    with pytest.raises(ValidationError, match="leakage"):
        ScenarioBenchmark.model_validate(benchmark(case(), other))


@pytest.mark.parametrize("split", ["validation", "holdout"])
def test_prompt_optimizer_cannot_consume_evaluation_or_holdout_sources(split):
    other = case("case-b", split, sha="b", group="group-b")
    value = benchmark(case(), other)
    assert tuning_cases(value, ["case-a"])[0]["id"] == "case-a"
    with pytest.raises(ValueError, match="only ready tuning"):
        tuning_cases(value, ["case-b"])
