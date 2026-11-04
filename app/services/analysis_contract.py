"""Versioned, metadata-only media analysis accepted from hosted services."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .providers import ProviderExecutionError
from .remote_uri import validate_remote_uri


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class Source(AnalysisModel):
    uri: str


class MediaFacts(AnalysisModel):
    status: Literal["ok"]
    duration_seconds: float = Field(gt=0)
    size_bytes: int = Field(gt=0)
    format: str = Field(min_length=1, max_length=100)


class VideoFacts(AnalysisModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    codec: str | None = None
    frame_rate: str | float | None = None


class AudioFacts(AnalysisModel):
    codec: str | None = None
    sample_rate: float | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)


class Shot(AnalysisModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    duration: float = Field(gt=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    thumbnail_uri: str | None = None
    quality: float | None = Field(default=None, ge=0, le=1)
    motion: float | None = Field(default=None, ge=0, le=1)
    face_sharpness: float | None = Field(default=None, ge=0, le=1)
    emotion: float | None = Field(default=None, ge=0, le=1)
    semantic_match: float | None = Field(default=None, ge=0, le=1)
    orientation: Literal["portrait", "landscape", "square"] | None = None

    @model_validator(mode="after")
    def boundaries(self):
        if self.end <= self.start or abs(self.duration - (self.end - self.start)) > 0.02:
            raise ValueError("shot bounds and duration differ")
        if self.thumbnail_uri:
            validate_remote_uri(self.thumbnail_uri, field="thumbnail_uri")
        return self


class Stage(AnalysisModel):
    status: Literal["ok", "partial", "failed", "unavailable", "not_requested"]
    provider: str = Field(min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=100)
    failure_code: str | None = Field(default=None, max_length=100)


class TranscriptSegment(AnalysisModel):
    id: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=20000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    speaker_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    # An external analyser must explicitly attest this boundary. Ordinary ASR
    # chunk boundaries are not automatically safe semantic edit boundaries.
    boundary_safe: bool | None = Field(default=None, strict=True)


class TranscriptWord(AnalysisModel):
    id: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    segment_id: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    start: float = Field(ge=0, strict=True)
    end: float = Field(gt=0, strict=True)
    text: str = Field(min_length=1, max_length=1000)
    confidence: float | None = Field(default=None, ge=0, le=1, strict=True)
    speaker_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")


class Transcript(Stage):
    language: str | None = Field(default=None, max_length=30)
    segments: list[TranscriptSegment] = Field(default_factory=list, max_length=20000)
    words: list[TranscriptWord] | None = Field(default=None, max_length=100000)
    word_alignment: Stage | None = None


class MediaAnalysisV2(AnalysisModel):
    schema_version: Literal["media-analysis/2"]
    provider: str = Field(min_length=1, max_length=100)
    source: Source
    media: MediaFacts
    video: VideoFacts | None = None
    audio: AudioFacts | None = None
    shots: list[Shot] = Field(default_factory=list, max_length=20000)
    shot_analysis: Stage
    transcription: Transcript
    warnings: list[str] = Field(default_factory=list, max_length=100)
    completeness: Literal["complete", "partial"] = "complete"

    @model_validator(mode="after")
    def coherent(self):
        if self.video is None and self.audio is None:
            raise ValueError("media has neither video nor audio facts")
        duration, previous = self.media.duration_seconds, 0.0
        for shot in self.shots:
            if shot.start < previous - 0.02 or shot.end > duration + 0.02:
                raise ValueError("shots overlap or exceed media duration")
            previous = shot.end
        if self.shot_analysis.status == "ok" and self.video and not self.shots:
            raise ValueError("successful video shot analysis has no shots")
        if self.shots and self.shot_analysis.status not in {"ok", "partial"}:
            raise ValueError("shot data conflicts with stage status")
        for row in self.transcription.segments:
            if row.end <= row.start or row.end > duration + 0.1:
                raise ValueError("transcription segment exceeds media bounds")
        segments = self.transcription.segments
        ids = [row.id for row in segments if row.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("transcription segment identities are duplicated")
        words, alignment = self.transcription.words, self.transcription.word_alignment
        if words is not None:
            if self.transcription.status not in {"ok", "partial"} or alignment is None:
                raise ValueError("word timings require explicit transcription/alignment evidence")
            if words and alignment.status not in {"ok", "partial"}:
                raise ValueError("word data conflicts with alignment status")
            word_ids = [word.id for word in words if word.id is not None]
            if len(word_ids) != len(set(word_ids)):
                raise ValueError("transcription word identities are duplicated")
            by_segment = {row.id: row for row in segments if row.id}
            previous_start = -1.0
            for word in words:
                if word.start < previous_start or word.end <= word.start or word.end > duration + 0.1:
                    raise ValueError("word timings are unordered or outside media bounds")
                previous_start = word.start
                if word.segment_id is not None:
                    segment = by_segment.get(word.segment_id)
                    if (segment is None or word.start < segment.start - 0.02 or word.end > segment.end + 0.02
                            or word.speaker_id and segment.speaker_id and word.speaker_id != segment.speaker_id):
                        raise ValueError("word timing/identity conflicts with its segment")
        if alignment and alignment.status == "ok" and not words:
            raise ValueError("successful word alignment has no words")
        stages = (self.shot_analysis, self.transcription, *((alignment,) if alignment else ()))
        self.completeness = "partial" if any(stage.status not in {"ok", "not_requested"} for stage in stages) else "complete"
        return self


def validate_hosted_analysis(value: dict, *, source_uri: str) -> dict:
    try:
        analysis = MediaAnalysisV2.model_validate(value)
        if analysis.source.uri != source_uri:
            raise ValueError("analysis source differs from the submitted object")
        validate_remote_uri(analysis.source.uri, field="source.uri")
    except ValueError as exc:
        raise ProviderExecutionError("Hosted analysis does not satisfy the versioned media contract",
                                     code="provider_analysis_invalid", capability="media_analysis") from exc
    result = analysis.model_dump(mode="json", exclude_none=True)
    # Stable bookkeeping IDs are not inferred speech or quality measurements.
    # Existing sentence-only payloads retain their original output shape.
    transcript = result["transcription"]
    if transcript.get("words"):
        for group, prefix in (("segments", "s"), ("words", "w")):
            for index, row in enumerate(transcript[group]):
                if not row.get("id"):
                    identity = [source_uri, analysis.provider, transcript.get("version"), group, index, row]
                    token = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                                      separators=(",", ":")).encode()).hexdigest()[:24]
                    row["id"] = f"{prefix}-{token}"
    return result


def require_planning_analysis(analysis: dict, *, source_uri: str, kind: str) -> None:
    value = validate_hosted_analysis(analysis, source_uri=source_uri)
    if kind.startswith("video") and value["shot_analysis"]["status"] != "ok":
        raise ProviderExecutionError("Complete shot analysis is required before automatic editing",
                                     code="analysis_not_ready", capability="media_analysis")
