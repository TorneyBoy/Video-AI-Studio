"""Accepted individual effects cannot silently imply accepted combinations."""
import pytest

from app.services import render_features
from app.services.editing_errors import EditingError


def entry(identifier, features, combinations=None):
    limits = {"features": features}
    if combinations is not None:
        limits["feature_combinations"] = combinations
    return {"id": identifier, "available": True, "capability": "video_editing", "provider": identifier,
            "model_id": "editor", "model_version": "1", "limits": limits}


def mixed_timeline():
    return {"tracks": [{"type": "audio", "clips": [{"effects": [{"type": "audio_fade"}]}]}]}


def test_only_one_accepted_combination_on_one_pinned_provider_covers_render(monkeypatch):
    entries = [entry("separate", ["audio_tracks", "audio_fades"], [["audio_tracks"], ["audio_fades"]])]
    monkeypatch.setattr(render_features, "capability_catalog", lambda: {"capabilities": entries})
    result = render_features.render_support(mixed_timeline())
    assert not result["supported"] and result["missing_features"] == []
    assert result["missing_combination"] == ["audio_fades", "audio_tracks"]
    with pytest.raises(EditingError) as error:
        render_features.require_render_support(mixed_timeline())
    assert error.value.details["missing_combination"] == ["audio_fades", "audio_tracks"]
    entries.append(entry("accepted", ["audio_tracks", "audio_fades"], [["audio_tracks", "audio_fades", "multi_audio_tracks"]]))
    assert render_features.render_support(mixed_timeline())["selected"]["id"] == "accepted"
    assert not render_features.render_support(mixed_timeline(), provider="separate")["supported"]
    assert not render_features.render_support(mixed_timeline(), provider="accepted", model_version="old")["supported"]


def test_legacy_single_feature_and_base_timeline_remain_compatible(monkeypatch):
    monkeypatch.setattr(render_features, "capability_catalog", lambda: {"capabilities": [entry("legacy", ["audio_tracks"])]})
    assert render_features.render_support({"tracks": [{"type": "audio", "clips": [{}]}]})["supported"]
    assert render_features.render_support({"tracks": []})["supported"]


def test_unknown_effect_never_becomes_supported_by_an_unrelated_combination(monkeypatch):
    monkeypatch.setattr(render_features, "capability_catalog", lambda: {"capabilities": [
        entry("accepted", ["audio_tracks", "audio_fades"], [["audio_tracks", "audio_fades"]])]})
    raw = mixed_timeline(); raw["tracks"][0]["clips"][0]["effects"].append({"type": "mystery"})
    assert "effect_mystery" in render_features.render_support(raw)["missing_features"]
