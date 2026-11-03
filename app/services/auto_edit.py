"""Deterministic, explainable auto-edit planning.

The planner selects shots from analysis metadata. It deliberately stops at a
reviewable plan; a later compiler turns the plan into Timeline operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from .shot_requirements import assess, template_requirement
from typing import Any
from timeline_engine.timing import FrameClock, allocate_frames

from timeline_engine import (
    AssetRef,
    AssetType,
    Clip,
    Resolution,
    TemplateDefinition,
    TimeRange,
    Timeline,
    Track,
    TrackType,
)


@dataclass(frozen=True)
class ShotFeature:
    asset_id: str
    duration: float
    quality: float = 0.0
    motion: float = 0.0
    face_sharpness: float = 0.0
    emotion: float = 0.0
    semantic_match: float = 0.0
    orientation: str | None = None
    asset_type: AssetType = AssetType.VIDEO
    metadata: dict[str, Any] | None = None
    available_features: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SelectedShot:
    slot: str
    asset_id: str
    score: float
    rationale: dict[str, float]
    selection_basis: str = "measured_features"


@dataclass(frozen=True)
class AutoEditPlan:
    template_id: str
    template_version: str
    selected: tuple[SelectedShot, ...]
    warnings: tuple[str, ...] = ()
    missing_required_slots: tuple[str, ...] = ()


class AutoEditPlanner:
    """Match analyzed shots to template slots with stable scoring."""

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = weights or {
            "quality": 0.20,
            "motion": 0.25,
            "face_sharpness": 0.20,
            "emotion": 0.15,
            "semantic_match": 0.20,
        }

    def score(self, shot: ShotFeature) -> tuple[float, dict[str, float]]:
        # An unavailable measurement is different from a measured zero. Do not
        # manufacture a quality/semantic score from a detector's missing fields.
        rationale = {key: round(getattr(shot, key) * weight, 6) for key, weight in self.weights.items()
                     if shot.available_features is None or key in shot.available_features}
        return round(sum(rationale.values()), 6), rationale

    def plan(self, template: TemplateDefinition, shots: list[ShotFeature]) -> AutoEditPlan:
        selected: list[SelectedShot] = []
        warnings: list[str] = []
        missing_required: list[str] = []
        used: set[str] = set()
        clock = FrameClock(float(template.fps or 30))
        policies = template.metadata.get("slot_policies", {}) if isinstance(template.metadata, dict) else {}

        for slot in template.slots:
            policy = policies.get(slot.name, {}) if isinstance(policies, dict) else {}
            requirement = template_requirement(slot, policy)
            candidates = []
            for shot in shots:
                if shot.asset_id in used:
                    continue
                raw = shot.metadata or {}
                start = float(raw.get("source_start", raw.get("start", 0)) or 0)
                end = float(raw.get("source_end", raw.get("end", start + shot.duration)) or start + shot.duration)
                try:
                    first, last = clock.window(start, end)
                except ValueError:
                    continue
                usable_duration = clock.seconds(last - first)
                eligibility = assess(requirement, kind=shot.asset_type.value, duration=usable_duration,
                    orientation=shot.orientation, metadata=raw, tags=raw.get("_user_tags") or (), image_hold=False)
                if eligibility["verdict"] != "supported":
                    continue
                score, rationale = self.score(shot)
                candidates.append((score, shot, rationale))
            def order(item):
                base, _, index = item[1].asset_id.partition("#shot-")
                return -item[0], base, int(index) if index.isdigit() else -1, item[1].asset_id
            candidates.sort(key=order)
            max_items = slot.constraints.max_items or 1
            chosen = candidates[:max_items]
            if not slot.required and len(chosen) < slot.constraints.min_items:
                chosen = []
            for score, shot, rationale in chosen:
                selected.append(SelectedShot(slot=slot.name, asset_id=shot.asset_id, score=score, rationale=rationale,
                                             selection_basis="measured_features" if rationale else "source_order"))
                used.add(shot.asset_id)
            if slot.required and len(chosen) < max(1, slot.constraints.min_items):
                missing_required.append(slot.name)
                warnings.append(f"no eligible shot for required slot '{slot.name}'")
        if selected and any(not row.rationale for row in selected):
            warnings.append("部分镜头没有画质、动作或语义评分，按素材与镜头顺序选择；这不代表质量为零。")
        available = sum(shot.duration for shot in shots if shot.asset_id in used)
        if template.duration and available < template.duration - 0.02 and selected:
            warnings.append(f"目标时长 {template.duration:g} 秒，可用入选镜头共 {available:g} 秒；当前将按可用时长制作，不补帧或变速。")
        return AutoEditPlan(template.id, template.version, tuple(selected), tuple(warnings), tuple(missing_required))


def _asset_type(value: str | None) -> AssetType:
    """Map API asset kinds to the canonical timeline enum."""
    normalized = (value or "video").lower()
    if normalized.startswith("image"):
        return AssetType.IMAGE
    if normalized.startswith("audio"):
        return AssetType.AUDIO
    if normalized.startswith("font"):
        return AssetType.FONT
    return AssetType.VIDEO


def shot_features_from_assets(assets: list[Any]) -> list[ShotFeature]:
    """Build deterministic planner features from tenant-scoped asset rows.

    The API may enqueue a plan before media analysis has finished. Keeping the
    feature extraction here lets the worker re-plan from the committed asset
    metadata immediately before compilation, so a stale fallback plan cannot
    silently become the final Timeline.
    """
    features: list[ShotFeature] = []
    for asset in assets:
        metadata = getattr(asset, "metadata_json", None) or {}
        analysis = metadata.get("analysis", {}) if isinstance(metadata, dict) else {}
        shot_rows = analysis.get("shots", []) if isinstance(analysis, dict) else []
        if not shot_rows:
            shot_rows = [{"duration": float(metadata.get("duration", 1.0) or 1.0)}]
        for index, raw_shot in enumerate(shot_rows):
            shot = raw_shot if isinstance(raw_shot, dict) else {}
            if metadata.get("user_tags"):
                shot = {**shot, "_user_tags": list(metadata["user_tags"])}
            features.append(
                ShotFeature(
                    asset_id=f"{asset.id}#shot-{index}",
                    duration=float(shot.get("duration", 1.0) or 1.0),
                    quality=float(shot.get("quality", 0.0) or 0.0),
                    motion=float(shot.get("motion", 0.0) or 0.0),
                    face_sharpness=float(shot.get("face_sharpness", 0.0) or 0.0),
                    emotion=float(shot.get("emotion", 0.0) or 0.0),
                    semantic_match=float(shot.get("semantic_match", 0.0) or 0.0),
                    orientation=shot.get("orientation"),
                    asset_type=_asset_type(getattr(asset, "kind", None)),
                    metadata=shot,
                    available_features=tuple(key for key in ("quality", "motion", "face_sharpness", "emotion", "semantic_match")
                                             if isinstance(shot.get(key), (int, float)) and not isinstance(shot.get(key), bool)),
                )
            )
    return features


def compile_auto_edit_timeline(
    template: TemplateDefinition,
    selected: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    assets: list[Any],
    *,
    timeline_id: str,
    fps: float | None = None,
    resolution: dict[str, int] | None = None,
) -> Timeline:
    """Compile a planner result into a canonical, renderable Timeline.

    The planner intentionally returns explainable shot IDs (``asset#shot-N``)
    rather than renderer objects.  This compiler resolves those IDs against
    tenant-scoped ``Asset`` rows, preserves source ranges from analysis, and
    places selected clips sequentially on one video track.  The function is
    deterministic, so a retry after a worker crash produces the same timeline
    and content hash.
    """
    clock = FrameClock(float(fps if fps is not None else template.fps or 30))
    by_id = {str(asset.id): asset for asset in assets}
    rows: list[tuple[dict[str, Any], Any, int, int]] = []
    missing: list[str] = []
    for selected_row in selected:
        shot_id = str(selected_row.get("asset_id") or "")
        asset_id, marker, suffix = shot_id.partition("#shot-")
        asset = by_id.get(asset_id or shot_id)
        if asset is None:
            missing.append(shot_id)
            continue
        analysis = (asset.metadata_json or {}).get("analysis", {})
        shots = analysis.get("shots", []) if isinstance(analysis, dict) else []
        shot: dict[str, Any] = {}
        if marker and suffix.isdigit() and int(suffix) < len(shots):
            candidate = shots[int(suffix)]
            if isinstance(candidate, dict):
                shot = candidate
        source_start = float(shot.get("source_start", shot.get("start", 0.0)) or 0.0)
        duration = float(shot.get("duration", 0.0) or 0.0)
        source_end = shot.get("source_end", shot.get("end"))
        if source_end is not None:
            source_end = float(source_end)
            duration = source_end - source_start
        if duration <= 0:
            duration = float((asset.metadata_json or {}).get("duration", 1.0) or 1.0)
        source_end = float(source_end if source_end is not None else source_start + duration)
        if "source_start" in selected_row or "source_end" in selected_row:
            chosen_start = float(selected_row.get("source_start", source_start))
            chosen_end = float(selected_row.get("source_end", source_end))
            if chosen_start < source_start - 1e-8 or chosen_end > source_end + 1e-8 or chosen_end <= chosen_start:
                raise ValueError("selected source window exceeds its analyzed shot")
            source_start, source_end, duration = chosen_start, chosen_end, chosen_end - chosen_start
        first_frame, last_frame = clock.window(source_start, source_end)
        rows.append((selected_row, asset, first_frame, last_frame))

    if missing:
        raise ValueError(f"auto-edit selected assets are unavailable: {', '.join(sorted(missing))}")
    if not rows:
        raise ValueError("auto-edit produced no eligible shots")

    requested_duration = float(template.duration or 0.0)
    capacities = [last - first for _, _, first, last in rows]
    total_frames = min(clock.floor(requested_duration), sum(capacities)) if requested_duration > 0 else sum(capacities)
    allocation = allocate_frames(capacities, total_frames)
    cursor = 0
    clips: list[Clip] = []
    refs: dict[str, AssetRef] = {}
    for index, ((selected_row, asset, first, last), clip_frames) in enumerate(zip(rows, allocation)):
        end = cursor + clip_frames
        actual_id = str(asset.id)
        refs.setdefault(
            actual_id,
            AssetRef(
                id=actual_id,
                type=_asset_type(getattr(asset, "kind", None)),
                uri=str(asset.uri),
                metadata={"name": getattr(asset, "name", actual_id)},
            ),
        )
        clips.append(
            Clip(
                id=f"auto-{index}",
                asset_id=actual_id,
                range=TimeRange(start=clock.seconds(cursor), end=clock.seconds(end)),
                source_start=clock.seconds(first),
                source_end=clock.seconds(first + clip_frames),
                metadata={
                    "slot": str(selected_row.get("slot") or "slot"),
                    "score": float(selected_row.get("score", 0.0) or 0.0),
                    "rationale": dict(selected_row.get("rationale") or {}),
                    "source_shot_id": str(selected_row.get("asset_id")),
                },
            )
        )
        cursor = end
    if not clips:
        raise ValueError("auto-edit produced no renderable clips")

    return Timeline(
        id=timeline_id,
        fps=clock.fps,
        duration=clock.seconds(total_frames),
        resolution=Resolution(**(resolution or {"width": 1080, "height": 1920})),
        assets=list(refs.values()),
        tracks=[Track(id="video-main", type=TrackType.VIDEO, clips=clips)],
        metadata={
            "source": "auto_edit",
            "template_id": template.id,
            "template_version": template.version,
            "selected_count": len(clips),
            "requested_duration": requested_duration or None,
            "duration_policy": "fit_available_sources",
            "timing_policy": "whole_frames_within_source",
        },
    )
