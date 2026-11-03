"""Validate hosted render measurements against the immutable render timeline."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from timeline_engine import Timeline, TrackType

from .providers import ProviderExecutionError


class MediaQualityReport(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    schema_version: Literal["media-quality/1"]
    status: Literal["passed"]
    object_uri: str
    output_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    fps: float = Field(gt=0)
    has_audio: bool
    video_codec: Literal["h264", "avc1"]
    container: Literal["mp4"]
    inspector: str = Field(min_length=1, max_length=120)
    inspector_version: str = Field(min_length=1, max_length=120)


def validate_render_quality(value: dict, *, timeline: Timeline, output_uri: str) -> dict:
    try:
        report = MediaQualityReport.model_validate(value)
        if report.object_uri != output_uri:
            raise ValueError("quality report refers to a different object")
        if (report.width, report.height) != (timeline.resolution.width, timeline.resolution.height):
            raise ValueError("render dimensions differ from the timeline")
        if abs(report.duration_seconds - timeline.duration) > max(0.1, 2.0 / timeline.fps):
            raise ValueError("render duration differs from the timeline")
        if abs(report.fps - timeline.fps) > 0.02:
            raise ValueError("render frame rate differs from the timeline")
        explicit_audio = any(track.type == TrackType.AUDIO and not track.muted and
                             any(clip.volume > 0 for clip in track.clips) for track in timeline.tracks)
        if explicit_audio and not report.has_audio:
            raise ValueError("render lost the requested audio track")
    except ValueError as exc:
        raise ProviderExecutionError("Rendered media does not satisfy its quality contract",
                                     code="provider_render_quality_invalid", capability="video_editing") from exc
    result = report.model_dump(mode="json")
    result["output_sha256"] = report.output_sha256.lower()
    return result
