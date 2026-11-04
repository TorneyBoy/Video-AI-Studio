"""Pure ICE effect lowering, with no SDK calls or media acceptance claims."""
from copy import deepcopy

import pytest
from timeline_engine import Timeline

from media_gateway.ice_timeline import ICECompileError, OPTIONAL_FEATURES, compile_ice_timeline


def scene():
    return {"id": "effects", "fps": 30, "duration": 8,
            "resolution": {"width": 720, "height": 1280},
            "assets": [{"id": "video", "type": "video", "uri": "oss://fixture-bucket/tenants/t/video"},
                       {"id": "audio", "type": "audio", "uri": "oss://fixture-bucket/tenants/t/audio"},
                       {"id": "image", "type": "image", "uri": "oss://fixture-bucket/tenants/t/image"}],
            "tracks": [{"id": "main", "type": "video", "clips": [
                {"id": "first", "asset_id": "video", "range": {"start": 0, "end": 4}, "source_start": 1, "source_end": 5},
                {"id": "second", "asset_id": "video", "range": {"start": 4, "end": 8}, "source_start": 5, "source_end": 9}]}]}


def compile(value, features=tuple(OPTIONAL_FEATURES)):
    return compile_ice_timeline(Timeline.model_validate(value), bucket="fixture-bucket", region="cn-beijing", enabled_features=features)


def sound(*, fade=True):
    return {"id": "voice", "asset_id": "audio", "range": {"start": 2, "end": 6}, "source_start": 3, "source_end": 7,
            "volume": 0.5, "effects": [{"type": "audio_fade", "schema_version": "audio-envelope/1", "in_seconds": 0.5, "out_seconds": 1}] if fade else []}


def test_independent_audio_mixing_keeps_source_offsets_timeline_and_fade_curve():
    value = scene()
    value["tracks"] += [{"id": "voice", "type": "audio", "clips": [sound()]},
                        {"id": "music", "type": "audio", "muted": True, "clips": [
                            {"id": "music-clip", "asset_id": "video", "range": {"start": 0, "end": 8}, "volume": 2}]}]
    original = deepcopy(value)
    compiled, _ = compile(value)
    voice = compiled["AudioTracks"][0]["AudioTrackClips"][0]
    assert (voice["In"], voice["Out"], voice["TimelineIn"], voice["TimelineOut"]) == (3, 7, 2, 6)
    assert voice["Effects"] == [{"Type": "Volume", "Gain": 0.5},
                                {"Type": "AFade", "SubType": "In", "Duration": 0.5, "Curve": "tri"},
                                {"Type": "AFade", "SubType": "Out", "Duration": 1, "Curve": "tri"}]
    assert compiled["AudioTracks"][1]["AudioTrackClips"][0]["Effects"] == [{"Type": "Volume", "Gain": 0}]
    assert compiled["VideoTracks"][0]["VideoTrackClips"][0]["Effects"] == [{"Type": "Volume", "Gain": 1}]
    assert value == original


@pytest.mark.parametrize("image_volume", [0, 1])
def test_still_image_overlay_uses_declared_order_duration_placement_and_fit(image_volume):
    value = scene()
    value["tracks"][0]["clips"][0].update(asset_id="image", source_start=0, source_end=None,
        effects=[{"type": "frame_fit", "schema_version": "frame-fit/1", "mode": "cover"}])
    value["tracks"] += [
        {"id": "upper", "type": "overlay", "metadata": {"z_index": 8}, "clips": [
            {"id": "upper-clip", "asset_id": "image", "range": {"start": 3, "end": 5}, "volume": image_volume,
             "opacity": 0.5, "placement": {"x": 1, "y": 80, "width": 200, "height": 300},
             "effects": [{"type": "frame_fit", "schema_version": "frame-fit/1", "mode": "stretch"}]}]},
        {"id": "lower", "type": "overlay", "metadata": {"z_index": 2}, "clips": [
            {"id": "lower-clip", "asset_id": "video", "range": {"start": 1, "end": 3}, "source_start": 4, "source_end": 6, "volume": 0}]}]
    result, _ = compile(value)
    base, lower, upper = [track["VideoTrackClips"][0] for track in result["VideoTracks"]]
    assert base["Type"] == "Image" and base["Duration"] == 4 and base["AdaptMode"] == "Cover"
    assert "In" not in base and "Out" not in base
    assert lower["In"] == 4 and lower["TimelineIn"] == 1 and lower["Effects"][0]["Gain"] == 0
    assert upper["Type"] == "Image" and upper["Duration"] == 2
    assert (upper["X"], upper["Y"], upper["Width"], upper["Height"]) == (1 / 720, 80, 200, 300)
    assert upper["Opacity"] == 0.5 and upper["AdaptMode"] == "Fill"


@pytest.mark.parametrize("transition", ["fade", "wipeleft", "wiperight"])
def test_incoming_overlap_is_lowered_once_on_preceding_cloud_clip(transition):
    value = scene()
    value["metadata"] = {"composition": {"schema_version": "composition/1", "transition_policy": "overlap"}}
    value["tracks"][0]["clips"][1].update(range={"start": 3.5, "end": 7.5}, transition={"type": transition, "duration": 0.5})
    value["duration"] = 7.5
    result, output = compile(value)
    first, second = result["VideoTracks"][0]["VideoTrackClips"]
    assert first["Effects"][-1] == {"Type": "Transition", "SubType": transition, "Duration": 0.5}
    assert second["Effects"] == [{"Type": "Volume", "Gain": 1}]
    assert all("TimelineIn" not in row and "TimelineOut" not in row for row in (first, second))
    assert (first["Out"] - first["In"]) + (second["Out"] - second["In"]) - 0.5 == output["MaxDuration"] == 7.5
    assert "DLTransition" not in str(result)


def test_unaccepted_effects_and_unmapped_transitions_never_become_new_cloud_behavior():
    value = scene()
    value["tracks"].append({"id": "sound", "type": "audio", "clips": [sound()]})
    with pytest.raises(ICECompileError, match="require cloud acceptance"):
        compile(value, ())
    with pytest.raises(ICECompileError, match="require cloud acceptance"):
        compile(value, ("audio_tracks",))
    value = scene()
    value["tracks"][0]["clips"][1]["transition"] = {"type": "fade", "duration": 0.5}
    with pytest.raises(ICECompileError, match="explicit overlap"):
        compile(value)
    value["metadata"] = {"composition": {"transition_policy": "overlap"}}
    value["tracks"][0]["clips"][1].update(range={"start": 3.5, "end": 7.5}, transition={"type": "dissolve", "duration": 0.5})
    value["duration"] = 7.5
    with pytest.raises(ICECompileError):
        compile(value)


@pytest.mark.parametrize("change", ["unknown", "version", "extra", "overrun", "nonfinite", "duplicate", "wrong_track", "gain"])
def test_unrepresentable_or_malformed_effects_are_rejected(change):
    value = scene(); audio = sound()
    value["tracks"].append({"id": "voice", "type": "audio", "clips": [audio]})
    effect = audio["effects"][0]
    if change == "unknown": effect["type"] = "mystery"
    elif change == "version": effect["schema_version"] = "audio-envelope/2"
    elif change == "extra": effect["speed"] = 2
    elif change == "overrun": effect["out_seconds"] = 4
    elif change == "nonfinite": effect["in_seconds"] = float("nan")
    elif change == "duplicate": audio["effects"].append(deepcopy(effect))
    elif change == "wrong_track": value["tracks"][0]["clips"][0]["effects"] = [deepcopy(effect)]
    elif change == "gain": audio["volume"] = 11
    with pytest.raises(ICECompileError):
        compile(value)


def test_hidden_overlay_does_not_leak_and_text_under_media_is_explicitly_rejected():
    value = scene()
    value["tracks"] += [
        {"id": "text", "type": "subtitle", "metadata": {"z_index": 2}, "clips": [
            {"id": "text-clip", "text": "中文", "range": {"start": 0, "end": 8}}]},
        {"id": "overlay", "type": "overlay", "metadata": {"z_index": 3}, "muted": True, "clips": [
            {"id": "overlay-clip", "asset_id": "image", "volume": 0, "range": {"start": 0, "end": 8}}]}]
    result, _ = compile(value)
    assert len(result["VideoTracks"]) == 1
    assert result["SubtitleTracks"][0]["SubtitleTrackClips"][0]["Content"] == "中文"
    value["tracks"][-1]["muted"] = False
    with pytest.raises(ICECompileError, match="above a text layer"):
        compile(value)


def test_media_placement_cannot_be_partial_or_nonfinite():
    value = scene()
    for placement in ({"width": 200}, {"x": float("inf")}, {"x": -1}, {"x": 600, "width": 200, "height": 300}):
        value["tracks"][0]["clips"][0]["placement"] = placement
        with pytest.raises(ICECompileError):
            compile(value)


def test_subprecision_overlap_is_rejected_instead_of_becoming_a_cloud_cut():
    value = scene()
    value["metadata"] = {"composition": {"transition_policy": "overlap"}}
    value["tracks"][0]["clips"][1].update(range={"start": 3.99999, "end": 7.99999},
                                           transition={"type": "fade", "duration": 0.00001})
    value["duration"] = 7.99999
    with pytest.raises(ICECompileError, match="timestamp precision"):
        compile(value)
