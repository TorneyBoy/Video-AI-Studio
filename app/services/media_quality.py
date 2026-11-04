"""Validate hosted render measurements against the immutable render timeline."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator
from timeline_engine import AssetType, Timeline, TrackType
from timeline_engine.timing import FrameClock

from .providers import ProviderExecutionError


class QualityEvidence(BaseModel):
    """Bounded, persistable evidence metadata; never media URLs or SDK errors."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source: Literal["canonical_timeline", "hosted_probe", "acceptance_record", "frame_measurement",
                    "audio_measurement", "manual_review"]
    reference: str | None = Field(default=None, max_length=200, pattern=r"^[\w./:-]+$")
    expected: StrictInt | StrictFloat | StrictStr | StrictBool | None = None
    observed: StrictInt | StrictFloat | StrictStr | StrictBool | None = None
    unit: Literal["seconds", "frames", "pixels", "fps", "bytes", "boolean", "codec", "container", "sha256"] | None = None


class QualityFinding(BaseModel):
    """A located failure or review requirement, not an opaque overall score.

    Time ranges are half-open and in output timeline seconds. Sources identify
    whether a finding is derived from intent, a probe or an actual measurement;
    a canonical layout observation must never masquerade as rendered pixels.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    severity: Literal["error", "warning"]
    dimension: Literal["facts", "technical", "narrative", "visual", "audio"]
    message: str = Field(min_length=1, max_length=400)
    # Canonical IDs predate this report contract and have no 200-char cap.
    # Preserve the exact ID so a finding still locates an existing edit.
    track_id: str | None = None
    clip_id: str | None = None
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)
    evidence: QualityEvidence

    @model_validator(mode="after")
    def ordered(self):
        if (self.start_seconds is None) != (self.end_seconds is None):
            raise ValueError("a finding range requires both endpoints")
        if self.start_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("finding end must follow start")
        return self


RenderFinding = QualityFinding
ICE_RENDER_MODEL = "aliyun-ice-cloud-editing"
ICE_IMAGE_TIMING_EVIDENCE = "ice-image-boundary-20261104"


def artifact_quality_findings(metadata: dict) -> list[dict]:
    """Merge every persisted technical finding without hiding a second report.

    Both report fields can coexist on a recovered render. A nonempty structural
    report must not mask renderer errors. Invalid persisted reports fail closed
    at the review/delivery boundary rather than being treated as no findings.
    """
    metrics = metadata.get("metrics")
    if metrics is None:
        metrics = {}
    if not isinstance(metrics, dict):
        raise ValueError("invalid artifact metrics")
    assessment = metrics.get("render_quality_assessment")
    if assessment is not None:
        if not isinstance(assessment, dict) or assessment.get("schema_version") != "render-quality-assessment/1" or assessment.get("scope") != "structural_probe" or assessment.get("status") not in {"passed", "failed"} or "findings" not in assessment:
            raise ValueError("invalid render quality assessment")
    else:
        assessment = {}
    result = []
    seen = set()
    for values in (assessment.get("findings", []), metrics.get("renderer_quality_findings", [])):
        if not isinstance(values, list):
            raise ValueError("invalid persisted quality findings")
        for value in values:
            finding = QualityFinding.model_validate(value).model_dump(mode="json", exclude_none=True)
            key = json.dumps(finding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key not in seen:
                seen.add(key)
                result.append(finding)
    if assessment:
        structural_errors = any(QualityFinding.model_validate(value).severity == "error" for value in assessment.get("findings", []))
        if (assessment["status"] == "failed") != structural_errors:
            raise ValueError("contradictory render quality assessment")
    return result


def renderer_findings(timeline: Timeline | dict, *, renderer_model_id: str | None) -> list[dict]:
    """Network-free admission findings for known renderer limitations.

    Native ICE Image timing failed the real boundary experiment. This denies
    new execution even if an operator accidentally enables image_clips; it does
    not alter timestamps, historical jobs, or another renderer's image support.
    """
    canonical = timeline if isinstance(timeline, Timeline) else Timeline.model_validate(timeline)
    assets = {item.id: item for item in canonical.assets}
    findings = []
    width, height = canonical.resolution.width, canonical.resolution.height
    for track in canonical.tracks:
        if track.muted and track.type in {TrackType.SUBTITLE, TrackType.OVERLAY}:
            continue
        for clip in track.clips:
            if clip.opacity == 0:
                continue
            location = dict(track_id=track.id, clip_id=clip.id,
                            start_seconds=clip.range.start, end_seconds=clip.range.end)
            asset = assets.get(clip.asset_id)
            if (renderer_model_id == ICE_RENDER_MODEL and asset and asset.type == AssetType.IMAGE
                    and track.type in {TrackType.VIDEO, TrackType.OVERLAY}):
                findings.append(QualityFinding(code="ice_image_end_unverified", severity="error", dimension="technical",
                    message="ICE原生图片结束边界尚未通过逐帧验收，请使用已验收的渲染方案。",
                    **location, evidence=QualityEvidence(source="acceptance_record", reference=ICE_IMAGE_TIMING_EVIDENCE,
                                                        expected="half_open_frame_interval", observed="no_exact_candidate")))
            if clip.text is not None and clip.placement:
                position = clip.placement
                if position.x >= width or position.y >= height:
                    findings.append(QualityFinding(code="subtitle_origin_outside_canvas", severity="error", dimension="visual",
                        message="字幕起点完全位于画幅之外，请调整字幕位置。", **location,
                        evidence=QualityEvidence(source="canonical_timeline", expected="origin_inside_canvas",
                                                 observed="origin_outside_canvas", unit="pixels")))
                elif (position.x < 0 or position.y < 0
                      or position.width is not None and position.x + position.width > width
                      or position.height is not None and position.y + position.height > height):
                    findings.append(QualityFinding(code="subtitle_layout_requires_review", severity="warning", dimension="visual",
                        message="字幕声明区域超出画幅，需核对真实字形、换行和安全区。", **location,
                        evidence=QualityEvidence(source="canonical_timeline", expected="declared_region_inside_canvas",
                                                 observed="declared_region_overflow", unit="pixels")))
    return [item.model_dump(mode="json", exclude_none=True) for item in findings]


def assert_renderer_quality(timeline: Timeline | dict, *, renderer_model_id: str | None) -> None:
    findings = renderer_findings(timeline, renderer_model_id=renderer_model_id)
    if any(item["severity"] == "error" for item in findings):
        raise ProviderExecutionError("Renderer cannot satisfy the declared quality contract",
            code="renderer_quality_unverified", capability="video_editing", details={"findings": findings})


def quality_timeline_hash(timeline: Timeline) -> str:
    return hashlib.sha256(json.dumps(timeline.model_dump(mode="json"), sort_keys=True,
                                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ClipVisibilityObservation(BaseModel):
    """An external inspector's complete, unobscured clip observation.

    This is an evidence ingestion contract, not a detector. In particular a
    sparse contact sheet cannot satisfy the complete-frame coverage field.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    schema_version: Literal["clip-visibility/1"]
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeline_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    clip_id: str = Field(min_length=1)
    fps: float = Field(gt=0, le=240)
    unobscured: Literal[True]
    checked_frames: list[StrictInt] = Field(min_length=1, max_length=600000)
    visible_frames: list[StrictInt] = Field(max_length=600000)
    evidence_ref: str = Field(min_length=1, max_length=200, pattern=r"^[\w./:-]+$")

    @model_validator(mode="before")
    @classmethod
    def explicit_unobscured_measurement(cls, value):
        if not isinstance(value, dict) or value.get("unobscured") is not True:
            raise ValueError("visibility inspection requires an explicitly unobscured target")
        return value


def visibility_timing_findings(value: dict, *, timeline: Timeline, output_sha256: str) -> list[dict]:
    """Locate every visibility mismatch from bound, whole-output measurements.

    A caller must have established an unobscured target and supply measured
    frame indices. This cannot establish identity, image quality or legibility.
    """
    observation = ClipVisibilityObservation.model_validate(value)
    if (observation.output_sha256 != output_sha256 or observation.timeline_hash != quality_timeline_hash(timeline)
            or abs(observation.fps - timeline.fps) > 1e-8):
        raise ValueError("visibility measurement is not bound to the output and timeline")
    if observation.checked_frames != list(range(timeline.frame_count)):
        raise ValueError("visibility measurement must inspect every output frame in order")
    observed = observation.visible_frames
    if observed != sorted(set(observed)) or any(index < 0 or index >= timeline.frame_count for index in observed):
        raise ValueError("visibility frame indices are invalid")
    pair = next(((track, clip) for track in timeline.tracks for clip in track.clips
                 if clip.id == observation.clip_id), None)
    if pair is None:
        raise ValueError("visibility clip does not exist")
    track, clip = pair
    if track.type == TrackType.AUDIO or track.muted and track.type != TrackType.VIDEO or clip.opacity == 0:
        raise ValueError("visibility measurement requires a visible clip")
    clock = FrameClock(timeline.fps)
    first, end = clock.ceil(clip.range.start), min(clock.ceil(clip.range.end), timeline.frame_count)
    expected, actual = set(range(first, end)), set(observed)
    findings = []
    for code, values, expected_visible in (("clip_visible_outside_interval", sorted(actual - expected), False),
                                            ("clip_missing_inside_interval", sorted(expected - actual), True)):
        # Group only contiguous bad frames, preserving the precise interval.
        groups = []
        for index in values:
            if groups and index == groups[-1][-1] + 1:
                groups[-1].append(index)
            else:
                groups.append([index])
        for group in groups:
            finding = QualityFinding(code=code, severity="error", dimension="technical",
                message="逐帧测量发现片段显示区间与时间线不一致。", track_id=track.id, clip_id=clip.id,
                start_seconds=clock.seconds(group[0]), end_seconds=min(clock.seconds(group[-1] + 1), timeline.duration),
                evidence=QualityEvidence(source="frame_measurement", reference=observation.evidence_ref,
                                         expected=expected_visible, observed=not expected_visible, unit="boolean"))
            findings.append(finding.model_dump(mode="json", exclude_none=True))
    return findings


def audio_tail_finding(*, expected_end_seconds: float, observed_end_seconds: float, tolerance_seconds: float,
                       evidence_ref: str, track_id: str | None = None, clip_id: str | None = None) -> dict | None:
    """Compare measured isolated audio bounds under a caller's frozen tolerance.

    Stream duration alone does not prove a missing syllable. Report the shorter
    stream and require tail/padding review; never call waveform correlation or
    track presence a complete listening assessment.
    """
    from math import isfinite

    values = (expected_end_seconds, observed_end_seconds, tolerance_seconds)
    if (any(isinstance(item, bool) or not isinstance(item, (float, int)) or not isfinite(item) for item in values)
            or expected_end_seconds <= 0 or observed_end_seconds < 0 or not 0 <= tolerance_seconds <= 0.25):
        raise ValueError("audio bounds and tolerance must be valid measured seconds")
    if expected_end_seconds - observed_end_seconds <= tolerance_seconds + 1e-9:
        return None
    return QualityFinding(code="audio_stream_tail_short", severity="error", dimension="audio",
        message="测得音轨结束早于要求，需核对尾音和编码填充；时长差不直接证明丢字。",
        track_id=track_id, clip_id=clip_id, start_seconds=observed_end_seconds, end_seconds=expected_end_seconds,
        evidence=QualityEvidence(source="audio_measurement", reference=evidence_ref,
                                 expected=expected_end_seconds, observed=observed_end_seconds,
                                 unit="seconds")).model_dump(mode="json", exclude_none=True)


class MediaQualityReport(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, strict=True)
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


def assess_render_quality(value: dict, *, timeline: Timeline, output_uri: str) -> dict:
    """Assess probe measurements only; content/legibility/listening stay unassessed."""
    findings = []
    passed_checks = []

    def check(ok, code, message, expected, observed, unit=None):
        if ok:
            passed_checks.append(code)
        else:
            findings.append(QualityFinding(code=code, severity="error", dimension="technical", message=message,
                start_seconds=0, end_seconds=timeline.duration,
                evidence=QualityEvidence(source="hosted_probe", expected=expected, observed=observed, unit=unit)))

    try:
        report = MediaQualityReport.model_validate(value)
        # Do not echo a mismatched/signed URL into persisted public findings.
        check(report.object_uri == output_uri, "output_object_binding", "媒体探测对应了不同输出对象。",
              "same_object", "same_object" if report.object_uri == output_uri else "different_object")
        check(report.width == timeline.resolution.width, "output_width", "成片宽度与时间线不一致。",
              timeline.resolution.width, report.width, "pixels")
        check(report.height == timeline.resolution.height, "output_height", "成片高度与时间线不一致。",
              timeline.resolution.height, report.height, "pixels")
        check(abs(report.duration_seconds - timeline.duration) <= max(0.1, 2.0 / timeline.fps),
              "output_duration", "成片总时长超出结构探测容差。", timeline.duration, report.duration_seconds, "seconds")
        check(abs(report.fps - timeline.fps) <= 0.02, "output_frame_rate", "成片帧率与时间线不一致。",
              timeline.fps, report.fps, "fps")
        explicit_audio = any(track.type == TrackType.AUDIO and not track.muted and
                             any(clip.volume > 0 for clip in track.clips) for track in timeline.tracks)
        if explicit_audio:
            check(report.has_audio, "requested_audio_present", "成片缺少要求的独立音轨。", True, report.has_audio, "boolean")
    except ValueError:
        findings.append(QualityFinding(code="probe_report_invalid", severity="error", dimension="technical",
            message="媒体探测报告格式无效或缺少必要测量。", start_seconds=0, end_seconds=timeline.duration,
            evidence=QualityEvidence(source="hosted_probe", expected="valid_media_quality_1", observed="invalid_report")))
    return {"schema_version": "render-quality-assessment/1", "scope": "structural_probe",
        "status": "failed" if findings else "passed", "assessed_dimensions": ["technical"],
        "not_assessed_dimensions": ["facts", "narrative", "visual", "audio"],
        "passed_checks": passed_checks, "findings": [item.model_dump(mode="json", exclude_none=True) for item in findings]}


def validate_render_quality(value: dict, *, timeline: Timeline, output_uri: str) -> dict:
    assessment = assess_render_quality(value, timeline=timeline, output_uri=output_uri)
    if assessment["status"] == "failed":
        raise ProviderExecutionError("Rendered media does not satisfy its quality contract",
            code="provider_render_quality_invalid", capability="video_editing", details={"assessment": assessment})
    report = MediaQualityReport.model_validate(value)
    result = report.model_dump(mode="json")
    result["output_sha256"] = report.output_sha256.lower()
    return result
