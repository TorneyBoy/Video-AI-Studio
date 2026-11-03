"""Pure Python quality gates for canonical timelines and template bindings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from .schema import AssetType, SlotType, TemplateBinding, TemplateDefinition, Timeline, TrackType


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str = ""
    severity: Severity = Severity.ERROR

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path, "severity": self.severity.value}


class ValidationReport:
    def __init__(self, issues: Iterable[ValidationIssue] = ()):
        self.issues = list(issues)

    @property
    def valid(self) -> bool:
        return not any(i.severity == Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    def as_dict(self) -> dict:
        return {"valid": self.valid, "errors": len(self.errors), "warnings": len(self.warnings), "issues": [i.as_dict() for i in self.issues]}

    def raise_for_errors(self) -> None:
        if not self.valid:
            raise ValueError("; ".join(f"{i.path}: {i.message}" for i in self.errors))


def validate_timeline(timeline: Timeline, *, allow_overlaps: bool = False) -> ValidationReport:
    """Run renderer-independent timeline quality gates."""
    issues: list[ValidationIssue] = []
    assets = {a.id: a for a in timeline.assets}
    clips_seen: set[str] = set()
    for ti, track in enumerate(timeline.tracks):
        clips = sorted(track.clips, key=lambda c: c.range.start)
        previous = None
        composition = timeline.metadata.get("composition") or {}
        overlap_policy = track.type == TrackType.VIDEO and isinstance(composition, dict) and composition.get("transition_policy") == "overlap"
        for ci, clip in enumerate(clips):
            path = f"tracks[{ti}].clips[{ci}]"
            if clip.id in clips_seen:
                issues.append(ValidationIssue("duplicate_clip_id", f"duplicate clip id '{clip.id}'", path))
            clips_seen.add(clip.id)
            if overlap_policy:
                incoming = clip.transition.duration if clip.transition else 0
                expected = previous.range.end - incoming if previous else 0
                if (not previous and incoming) or abs(clip.range.start - expected) > 1e-8:
                    issues.append(ValidationIssue("transition_layout", "explicit overlap layout must match the incoming transition without gaps or padding", path))
                if clip.asset_id in assets and assets[clip.asset_id].type == AssetType.VIDEO and (clip.source_end is None or abs(clip.range.duration - (clip.source_end - clip.source_start)) > 1e-7):
                    issues.append(ValidationIssue("transition_source_timing", "explicit video composition must preserve one-to-one source timing", path))
            if previous and clip.range.start < previous.range.end - 1e-9 and not allow_overlaps:
                explicit = (overlap_policy
                            and clip.transition and abs(previous.range.end - clip.range.start - clip.transition.duration) < 1e-8)
                if not explicit:
                    issues.append(ValidationIssue("overlap", f"clip overlaps '{previous.id}'", path))
                elif ci > 1 and clip.range.start < clips[ci - 2].range.end - 1e-9:
                    issues.append(ValidationIssue("transition_three_way_overlap", "transition creates three simultaneous base clips", path))
            if previous and clip.transition and clip.transition.duration >= previous.range.duration:
                issues.append(ValidationIssue("transition_duration", "transition must be shorter than both adjacent clips", path))
            previous = clip
            if clip.asset_id and clip.asset_id not in assets:
                issues.append(ValidationIssue("missing_asset", f"asset '{clip.asset_id}' is not declared", path))
            if track.type in (TrackType.VIDEO, TrackType.AUDIO) and not clip.asset_id:
                issues.append(ValidationIssue("track_asset_required", f"{track.type.value} track requires an asset clip", path))
            if track.type == TrackType.SUBTITLE and not clip.text:
                issues.append(ValidationIssue("subtitle_text_required", "subtitle clip requires text", path))
            if track.type == TrackType.OVERLAY and not clip.asset_id and not clip.text:
                issues.append(ValidationIssue("overlay_content_required", "overlay clip requires an asset or text", path))
            if track.type in (TrackType.VIDEO, TrackType.AUDIO) and clip.text:
                issues.append(ValidationIssue("track_text_not_allowed", f"{track.type.value} track cannot contain text clips", path))
            if clip.transition and track.type != TrackType.VIDEO:
                issues.append(ValidationIssue("transition_track_type", "transitions are only supported on video tracks", path))
            if clip.transition and clip.transition.duration >= clip.range.duration:
                issues.append(ValidationIssue("transition_duration", "transition must be shorter than the clip", path))
            if clip.asset_id in assets:
                asset_type = assets[clip.asset_id].type
                allowed = {
                    # Still images are valid base clips; the renderer loops
                    # them for the declared clip duration.
                    TrackType.VIDEO: (AssetType.VIDEO, AssetType.IMAGE),
                    TrackType.AUDIO: (AssetType.AUDIO, AssetType.VIDEO),
                    TrackType.OVERLAY: (AssetType.VIDEO, AssetType.IMAGE),
                }.get(track.type)
                if allowed and asset_type not in allowed:
                    issues.append(ValidationIssue("track_asset_type", f"{track.type.value} track does not accept {asset_type.value} asset", path))
    if timeline.duration * timeline.fps > 10_000_000:
        issues.append(ValidationIssue("frame_count_limit", "timeline has more than 10 million frames", "duration", Severity.WARNING))
    if not timeline.tracks:
        issues.append(ValidationIssue("empty_timeline", "timeline has no tracks", "tracks", Severity.WARNING))
    return ValidationReport(issues)


def validate_template_binding(
    template: TemplateDefinition,
    binding: TemplateBinding,
    *,
    timeline: Optional[Timeline] = None,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    if template.id != binding.template_id:
        issues.append(ValidationIssue("template_id_mismatch", f"binding targets '{binding.template_id}', expected '{template.id}'", "template_id"))
        return ValidationReport(issues)
    slots = {s.name: s for s in template.slots}
    for unknown in set(binding.values) - set(slots):
        issues.append(ValidationIssue("unknown_slot", f"slot '{unknown}' is not declared by template", f"values.{unknown}"))
    assets = {a.id: a for a in timeline.assets} if timeline else {}
    for name, slot in slots.items():
        value = binding.values.get(name)
        path = f"values.{name}"
        if value is None:
            if slot.required:
                issues.append(ValidationIssue("required_slot", f"required slot '{name}' is not bound", path))
            continue
        c = slot.constraints
        count = len(value.asset_ids)
        if value.text is not None and slot.type not in (SlotType.TEXT, SlotType.ANY):
            issues.append(ValidationIssue("slot_type", f"slot '{name}' does not accept text", path))
        if count < c.min_items:
            issues.append(ValidationIssue("min_items", f"slot '{name}' requires at least {c.min_items} item(s)", path))
        if c.max_items is not None and count > c.max_items:
            issues.append(ValidationIssue("max_items", f"slot '{name}' accepts at most {c.max_items} item(s)", path))
        if value.duration is not None:
            if c.min_duration is not None and value.duration < c.min_duration:
                issues.append(ValidationIssue("min_duration", f"duration {value.duration} is below {c.min_duration}", path))
            if c.max_duration is not None and value.duration > c.max_duration:
                issues.append(ValidationIssue("max_duration", f"duration {value.duration} exceeds {c.max_duration}", path))
        for aid in value.asset_ids:
            if timeline and aid not in assets:
                issues.append(ValidationIssue("missing_asset", f"asset '{aid}' is not declared", path))
            elif timeline and c.allowed_asset_types and assets[aid].type not in c.allowed_asset_types:
                issues.append(ValidationIssue("asset_type", f"asset '{aid}' type '{assets[aid].type.value}' is not allowed", path))
            if timeline and assets.get(aid):
                missing = [k for k in c.required_metadata if k not in assets[aid].metadata]
                if missing:
                    issues.append(ValidationIssue("asset_metadata", f"asset '{aid}' missing metadata: {', '.join(missing)}", path))
    if template.duration and timeline and abs(template.duration - timeline.duration) > 1e-6:
        issues.append(ValidationIssue("duration_mismatch", "timeline duration differs from template duration", "duration", Severity.WARNING))
    return ValidationReport(issues)


def validate_all(timeline: Timeline, template: Optional[TemplateDefinition] = None, binding: Optional[TemplateBinding] = None) -> ValidationReport:
    report = validate_timeline(timeline)
    if template and binding:
        report.issues.extend(validate_template_binding(template, binding, timeline=timeline).issues)
    return report

