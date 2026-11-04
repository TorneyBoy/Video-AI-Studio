"""Conservative speech edit windows from explicit, source-bound hosted evidence."""
from __future__ import annotations

import hashlib

from .analysis_contract import validate_hosted_analysis
from .providers import ProviderExecutionError


def speech_windows_from_asset(asset) -> list[dict]:
    """Never turn an arbitrary ASR chunk, probe or silence guess into safe speech.

    ``boundary_safe`` is an analyser assertion preserved in the frozen source
    analysis. These windows constrain editing; they do not certify meaning or
    replace the project's human/content quality review.
    """
    raw = (asset.metadata_json or {}).get("analysis")
    if not isinstance(raw, dict) or raw.get("schema_version") != "media-analysis/2":
        return []
    try:
        analysis = validate_hosted_analysis(raw, source_uri=asset.uri)
    except ProviderExecutionError:
        return []
    transcript = analysis["transcription"]
    if transcript["status"] != "ok" or (transcript.get("word_alignment") or {}).get("status") != "ok":
        return []
    words = transcript.get("words") or []
    result = []
    for segment in transcript["segments"]:
        if segment.get("boundary_safe") is not True or not segment.get("id"):
            continue
        associated = [word for word in words if word.get("segment_id") == segment["id"]]
        if not associated:
            continue
        # A segment must fully enclose its measured words. No frame snapping or
        # source truncation may silently discard the first/last spoken word.
        if any(word["start"] < segment["start"] or word["end"] > segment["end"] for word in associated):
            continue
        result.append({"id": segment["id"], "start": segment["start"], "end": segment["end"],
            "evidence_ref": f"transcript:{asset.id}:segment:{segment['id']}",
            "word_ids": [word["id"] for word in associated],
            "word_start": min(word["start"] for word in associated),
            "word_end": max(word["end"] for word in associated),
            "text_sha256": hashlib.sha256(segment["text"].encode()).hexdigest(),
            **({"speaker_id": segment["speaker_id"]} if segment.get("speaker_id") else {})})
    return sorted(result, key=lambda row: (row["start"], row["end"], row["id"]))
