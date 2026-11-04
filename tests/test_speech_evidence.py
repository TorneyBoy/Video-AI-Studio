from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.analysis_contract import validate_hosted_analysis
from app.services.providers import ProviderExecutionError
from app.services.speech_evidence import speech_windows_from_asset


URI = "oss://evidence/tenants/a/speech.mp4"


def analysis():
    return {"schema_version": "media-analysis/2", "provider": "test-hosted-analysis", "source": {"uri": URI},
        "media": {"status": "ok", "duration_seconds": 3.0, "size_bytes": 100, "format": "mp4"},
        "video": {"width": 480, "height": 832}, "audio": {"sample_rate": 48000, "channels": 1},
        "shots": [{"start": 0.0, "end": 3.0, "duration": 3.0}],
        "shot_analysis": {"status": "ok", "provider": "test-cuts", "version": "1"},
        "transcription": {"status": "ok", "provider": "test-asr", "version": "1", "language": "en",
            "word_alignment": {"status": "ok", "provider": "test-aligner", "version": "1"},
            "segments": [{"id": "intro", "start": 0.0, "end": 1.2, "text": "Keep the claim.",
                          "speaker_id": "speaker-0", "boundary_safe": True}],
            "words": [{"segment_id": "intro", "start": 0.05, "end": 0.3, "text": "Keep"},
                      {"segment_id": "intro", "start": 0.35, "end": 0.5, "text": "the"},
                      {"segment_id": "intro", "start": 0.6, "end": 1.1, "text": "claim."}]}}


def asset(value):
    return SimpleNamespace(id="asset-a", uri=URI, metadata_json={"analysis": value})


def test_explicit_speech_window_preserves_source_bounds_and_stable_word_evidence():
    raw = analysis()
    normalized = validate_hosted_analysis(raw, source_uri=URI)
    assert validate_hosted_analysis(raw, source_uri=URI) == normalized
    assert validate_hosted_analysis(normalized, source_uri=URI) == normalized
    window, = speech_windows_from_asset(asset(raw))
    assert (window["start"], window["end"]) == (0.0, 1.2)
    assert window["id"] == "intro" and window["evidence_ref"] == "transcript:asset-a:segment:intro"
    assert len(window["word_ids"]) == 3 and len(set(window["word_ids"])) == 3
    assert len(window["text_sha256"]) == 64 and "text" not in window
    assert "id" not in raw["transcription"]["words"][0]


@pytest.mark.parametrize("change", ["ordinary_chunk", "unsafe_chunk", "alignment_missing", "alignment_partial", "wrong_source", "words_missing"])
def test_missing_or_unsafe_evidence_never_fabricates_a_speech_window(change):
    raw = analysis()
    if change == "ordinary_chunk":
        raw["transcription"]["segments"][0].pop("boundary_safe")
    elif change == "unsafe_chunk":
        raw["transcription"]["segments"][0]["boundary_safe"] = False
    elif change == "alignment_missing":
        raw["transcription"].pop("word_alignment")
    elif change == "alignment_partial":
        raw["transcription"]["word_alignment"]["status"] = "partial"
    elif change == "wrong_source":
        raw["source"]["uri"] += "-changed"
    else:
        raw["transcription"].pop("words")
    assert speech_windows_from_asset(asset(raw)) == []


@pytest.mark.parametrize("change", ["out_of_media", "unordered", "outside_segment", "unknown_segment", "speaker_conflict", "boolean_time", "duplicate_word", "duplicate_segment", "empty_aligned"])
def test_invalid_alignment_is_rejected_before_it_can_drive_a_cut(change):
    raw = analysis()
    words = raw["transcription"]["words"]
    if change == "out_of_media":
        words[-1]["end"] = 4.0
    elif change == "unordered":
        words.reverse()
    elif change == "outside_segment":
        words[-1]["end"] = 1.5
    elif change == "unknown_segment":
        words[-1]["segment_id"] = "missing"
    elif change == "speaker_conflict":
        words[-1]["speaker_id"] = "speaker-1"
    elif change == "boolean_time":
        words[0]["start"] = False
    elif change == "duplicate_word":
        words[0]["id"] = words[1]["id"] = "same-word"
    elif change == "duplicate_segment":
        raw["transcription"]["segments"].append(deepcopy(raw["transcription"]["segments"][0]))
    else:
        raw["transcription"]["words"] = []
    with pytest.raises(ProviderExecutionError) as exc:
        validate_hosted_analysis(raw, source_uri=URI)
    assert exc.value.code == "provider_analysis_invalid"
    assert speech_windows_from_asset(asset(raw)) == []


def test_sentence_only_legacy_analysis_stays_readable_without_inventing_word_alignment():
    raw = analysis()
    for field in ("words", "word_alignment"):
        raw["transcription"].pop(field)
    for field in ("id", "speaker_id", "boundary_safe"):
        raw["transcription"]["segments"][0].pop(field)
    normalized = validate_hosted_analysis(raw, source_uri=URI)
    assert "words" not in normalized["transcription"]
    assert "word_alignment" not in normalized["transcription"]
    assert "id" not in normalized["transcription"]["segments"][0]
    assert speech_windows_from_asset(asset(raw)) == []


def test_even_tolerated_probe_rounding_does_not_cut_a_measured_word():
    raw = analysis()
    raw["transcription"]["segments"][0]["start"] = 0.06
    validate_hosted_analysis(raw, source_uri=URI)  # Allowed 10ms aligner rounding.
    assert speech_windows_from_asset(asset(raw)) == []


def test_frozen_transcript_v2_keeps_word_ids_and_speaker_boundaries():
    from app.services.subtitle_authoring import capture_transcript
    raw = analysis()
    captured = capture_transcript(raw)
    assert captured["schema_version"] == "transcript/2"
    assert captured["segments"][0]["id"] == "intro"
    assert captured["segments"][0]["speaker_id"] == "speaker-0"
    assert captured["segments"][0]["boundary_safe"] is True
    assert all(word["id"] and word["segment_id"] == "intro" for word in captured["words"])
    assert capture_transcript(raw) == captured
    changed = deepcopy(raw)
    changed["transcription"]["words"][-1]["end"] = 1.12
    assert capture_transcript(changed)["hash"] != captured["hash"]
