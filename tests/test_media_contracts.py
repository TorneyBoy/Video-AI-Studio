from copy import deepcopy
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from timeline_engine import Timeline

from app.services.analysis_contract import require_planning_analysis, validate_hosted_analysis
from app.services.media_quality import validate_render_quality
from app.services.providers import ProviderExecutionError
from media_gateway.credentials import oss_credentials
from media_gateway.ice_timeline import ICECompileError, compile_ice_timeline


URI = "oss://fixture-bucket/tenants/tenant-1/uploads/asset-1/source"


def analysis():
    return {"schema_version": "media-analysis/2", "provider": "fixture-hosted-analysis", "source": {"uri": URI},
            "media": {"status": "ok", "duration_seconds": 10, "size_bytes": 100, "format": "mp4"},
            "video": {"width": 720, "height": 1280},
            "shots": [{"start": 0, "end": 4, "duration": 4, "confidence": 0.8}, {"start": 4, "end": 10, "duration": 6}],
            "shot_analysis": {"status": "ok", "provider": "fixture", "version": "1"},
            "transcription": {"status": "not_requested", "provider": "fixture", "segments": []}}


def timeline():
    return Timeline.model_validate({"id": "timeline-1", "fps": 30, "duration": 10,
        "resolution": {"width": 720, "height": 1280}, "assets": [{"id": "a", "type": "video", "uri": URI}],
        "tracks": [{"id": "video", "type": "video", "clips": [
            {"id": "c1", "asset_id": "a", "range": {"start": 0, "end": 4}, "source_start": 1, "source_end": 5},
            {"id": "c2", "asset_id": "a", "range": {"start": 4, "end": 10}, "source_start": 4, "source_end": 10}]}]})


@pytest.mark.parametrize("unaccepted", ["image", "audio"])
def test_unaccepted_cloud_features_are_rejected_before_submission(unaccepted):
    value = timeline().model_dump(mode="json")
    if unaccepted == "image":
        value["assets"][0]["type"] = "image"
    else:
        value["tracks"].append({"id": "sound", "type": "audio", "clips": [
            {"id": "sound-1", "asset_id": "a", "range": {"start": 0, "end": 10}}]})
    with pytest.raises(ICECompileError, match="require cloud acceptance"):
        compile_ice_timeline(Timeline.model_validate(value), bucket="fixture-bucket", region="cn-beijing")


def test_analysis_source_ranges_and_partial_coverage_are_explicit():
    result = validate_hosted_analysis(analysis(), source_uri=URI)
    assert result["completeness"] == "complete"
    assert "quality" not in result["shots"][0]  # No fabricated semantic scores.
    partial = analysis()
    partial["shot_analysis"]["status"] = "partial"
    assert validate_hosted_analysis(partial, source_uri=URI)["completeness"] == "partial"
    with pytest.raises(ProviderExecutionError, match="Complete shot"):
        require_planning_analysis(partial, source_uri=URI, kind="video/mp4")


@pytest.mark.parametrize("change", ["source", "overlap", "out_of_bounds", "nan", "empty_shots", "transcript"])
def test_analysis_rejects_misbound_or_impossible_observations(change):
    value = analysis()
    if change == "source": value["source"]["uri"] += "-other"
    elif change == "overlap": value["shots"][1] = {"start": 3, "end": 9, "duration": 6}
    elif change == "out_of_bounds": value["shots"][1] = {"start": 4, "end": 11, "duration": 7}
    elif change == "nan": value["media"]["duration_seconds"] = float("nan")
    elif change == "empty_shots": value["shots"] = []
    elif change == "transcript": value["transcription"]["segments"] = [{"start": 9, "end": 12, "text": "too late"}]
    with pytest.raises(ProviderExecutionError): validate_hosted_analysis(value, source_uri=URI)


def quality():
    return {"schema_version": "media-quality/1", "status": "passed", "object_uri": URI,
            "output_sha256": "a" * 64, "size_bytes": 100, "width": 720, "height": 1280,
            "duration_seconds": 10, "fps": 30, "has_audio": False, "video_codec": "h264", "container": "mp4",
            "inspector": "fixture-probe", "inspector_version": "1"}


@pytest.mark.parametrize("change", ["object", "resolution", "long", "short", "fps", "hash"])
def test_render_report_must_match_exact_immutable_target(change):
    value = quality()
    if change == "object": value["object_uri"] += "-other"
    elif change == "resolution": value["height"] = 720
    elif change == "long": value["duration_seconds"] = 15
    elif change == "short": value["duration_seconds"] = 2
    elif change == "fps": value["fps"] = 24
    elif change == "hash": value["output_sha256"] = "not-a-hash"
    with pytest.raises(ProviderExecutionError): validate_render_quality(value, timeline=timeline(), output_uri=URI)


def test_render_report_preserves_measured_audio_and_hash():
    assert validate_render_quality(quality(), timeline=timeline(), output_uri=URI)["output_sha256"] == "a" * 64
    value = timeline().model_dump(mode="json")
    value["tracks"].append({"id": "audio", "type": "audio", "clips": [
        {"id": "audio1", "asset_id": "a", "range": {"start": 0, "end": 10}}]})
    with pytest.raises(ProviderExecutionError): validate_render_quality(quality(), timeline=Timeline.model_validate(value), output_uri=URI)


def test_ice_compiler_preserves_selected_source_and_output_bounds():
    value = timeline()
    before = deepcopy(value.model_dump())
    compiled, output = compile_ice_timeline(value, bucket="fixture-bucket", region="cn-beijing")
    first, second = compiled["VideoTracks"][0]["VideoTrackClips"]
    assert (first["In"], first["Out"], first["TimelineIn"], first["TimelineOut"]) == (1, 5, 0, 4)
    assert second["In"] == 4 and second["TimelineOut"] == 10
    assert first["MediaURL"].startswith("https://fixture-bucket.oss-cn-beijing.aliyuncs.com/")
    assert output["Width"] == 720 and output["MaxDuration"] == 10
    assert output["Video"] == {"Fps": 30, "Codec": "H.264", "Crf": 23}
    assert value.model_dump() == before


@pytest.mark.parametrize("change", ["transition", "other_bucket", "signed_url", "short_source", "gap"])
def test_ice_rejects_unrepresented_edits_before_submission(change):
    value = timeline().model_dump(mode="json")
    if change == "transition": value["tracks"][0]["clips"][0]["transition"] = {"type": "fade", "duration": 0.5}
    elif change == "other_bucket": value["assets"][0]["uri"] = URI.replace("fixture-bucket", "other-bucket")
    elif change == "signed_url": value["assets"][0]["uri"] = URI + "?secret-signature=1"
    elif change == "short_source": value["tracks"][0]["clips"][0]["source_end"] = 2
    elif change == "gap": value["tracks"][0]["clips"][1]["range"]["start"] = 5
    with pytest.raises(ICECompileError): compile_ice_timeline(Timeline.model_validate(value), bucket="fixture-bucket", region="cn-beijing")


def test_ecs_role_adapter_refreshes_whole_credentials_without_persisting_keys():
    class FakeRole:
        calls = 0
        def get_credentials(self):
            self.calls += 1
            return SimpleNamespace(get_access_key_id=lambda: f"fixture-{self.calls}",
                get_access_key_secret=lambda: "fixture-secret", get_security_token=lambda: f"token-{self.calls}",
                get_expiration=lambda: int(datetime.now(timezone.utc).timestamp()) + 3600)
    provider = FakeRole()
    adapter = oss_credentials("ecs_ram_role", role_name="VideoAIStudioStorageRole", role_provider=provider)
    assert adapter.get_credentials().access_key_id == "fixture-1"
    assert adapter.get_credentials().security_token == "token-2"
