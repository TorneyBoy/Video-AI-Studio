"""Deterministic, explainable auto-edit planning.

The planner selects shots from analysis metadata. It deliberately stops at a
reviewable plan; a later compiler turns the plan into Timeline operations.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fractions import Fraction
import math
from .shot_requirements import assess, template_requirement
from .scenario_profiles import scenario_binding, template_scenario
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
    scenario: dict[str, Any] | None = None


def scenario_feature_eligibility(profile, shot: ShotFeature) -> dict:
    """One rule for planning, manual candidates and saved scene validation."""
    required = set(profile.required_features) | set(profile.minimum_scores)
    missing = {key for key in required if shot.available_features is None or key not in shot.available_features}
    invalid = {key for key in required - missing if type(getattr(shot, key)) not in (int, float)
               or not math.isfinite(getattr(shot, key)) or not 0 <= getattr(shot, key) <= 1}
    below = {key for key, minimum in profile.minimum_scores.items()
             if key not in missing | invalid and getattr(shot, key) < minimum}
    return {"eligible": not (missing or invalid or below), "missing": sorted(missing),
            "invalid": sorted(invalid), "below_minimum": sorted(below)}


class AutoEditPlanner:
    """Match analyzed shots to template slots with stable scoring."""

    def __init__(self, weights: dict[str, float] | None = None):
        self._custom_weights = weights is not None
        self.weights = weights or {
            "quality": 0.20,
            "motion": 0.25,
            "face_sharpness": 0.20,
            "emotion": 0.15,
            "semantic_match": 0.20,
        }

    def score(self, shot: ShotFeature, *, weights: dict[str, float] | None = None) -> tuple[float, dict[str, float]]:
        # An unavailable measurement is different from a measured zero. Do not
        # manufacture a quality/semantic score from a detector's missing fields.
        rationale = {key: round(getattr(shot, key) * weight, 6) for key, weight in (weights or self.weights).items()
                     if shot.available_features is None or key in shot.available_features}
        return round(sum(rationale.values()), 6), rationale

    def plan(self, template: TemplateDefinition, shots: list[ShotFeature]) -> AutoEditPlan:
        selected: list[SelectedShot] = []
        warnings: list[str] = []
        missing_required: list[str] = []
        profile = template_scenario(template)
        if profile is not None and self._custom_weights:
            raise ValueError("scenario weights are versioned; explicit planner overrides require a new profile")
        weights = profile.selection_weights if profile else self.weights
        unavailable: set[str] = set()
        rejected_quality: set[str] = set()
        clock = FrameClock(float(template.fps or 30))
        policies = template.metadata.get("slot_policies", {}) if isinstance(template.metadata, dict) else {}
        eligible: list[list[str]] = []
        features: dict[str, tuple[float, ShotFeature, dict[str, float], int]] = {}

        def order(item):
            delimiter = "#speech-" if "#speech-" in item[1].asset_id else "#shot-"
            base, _, index = item[1].asset_id.partition(delimiter)
            return -item[0], base, int(index) if index.isdigit() else -1, item[1].asset_id

        for slot in template.slots:
            policy = policies.get(slot.name, {}) if isinstance(policies, dict) else {}
            requirement = template_requirement(slot, policy)
            candidates = []
            for shot in shots:
                if profile is not None:
                    eligibility = scenario_feature_eligibility(profile, shot)
                    if eligibility["missing"] or eligibility["invalid"]:
                        unavailable.update([*eligibility["missing"], *eligibility["invalid"]])
                        continue
                    if eligibility["below_minimum"]:
                        rejected_quality.add(shot.asset_id)
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
                score, rationale = self.score(shot, weights=weights)
                candidates.append((score, shot, rationale, last - first))
            candidates.sort(key=order)
            unique = {}
            for candidate in candidates:
                unique.setdefault(candidate[1].asset_id, candidate)
            eligible.append(list(unique))
            features.update(unique)

        # Match required quotas before filling spare positions. Augmenting paths
        # can move an earlier flexible slot to its next candidate instead of
        # consuming the only source that satisfies a later required slot.
        assignments: dict[tuple[int, int], str] = {}
        owners: dict[str, tuple[int, int]] = {}

        def assign(seat: tuple[int, int]) -> bool:
            queue = deque([seat])
            parents = {seat: None}
            visited: set[str] = set()
            while queue:
                current = queue.popleft()
                for shot_id in eligible[current[0]]:
                    if shot_id in visited:
                        continue
                    visited.add(shot_id)
                    owner = owners.get(shot_id)
                    if owner is not None:
                        if owner not in parents:
                            parents[owner] = (current, shot_id)
                            queue.append(owner)
                        continue
                    # Apply the path only after a free source is found. Failed
                    # attempts leave every previous assignment intact.
                    while True:
                        assignments[current] = shot_id
                        owners[shot_id] = current
                        parent = parents[current]
                        if parent is None:
                            return True
                        current, shot_id = parent
            return False

        minima = [max(1, slot.constraints.min_items) if slot.required else slot.constraints.min_items
                  for slot in template.slots]
        for index, slot in enumerate(template.slots):
            if slot.required:
                for ordinal in range(minima[index]):
                    assign((index, ordinal))

        omitted: set[int] = set()
        for index, slot in enumerate(template.slots):
            if slot.required:
                continue
            before, prior_owners = dict(assignments), dict(owners)
            if any(not assign((index, ordinal)) for ordinal in range(minima[index])):
                assignments, owners = before, prior_owners
                omitted.add(index)

        for index, slot in enumerate(template.slots):
            if index in omitted:
                continue
            maximum = slot.constraints.max_items
            maximum = len(eligible[index]) if maximum is None else maximum
            for ordinal in range(minima[index], maximum):
                if not assign((index, ordinal)):
                    break

        used = set(owners)
        for index, slot in enumerate(template.slots):
            chosen_ids = {shot_id for seat, shot_id in assignments.items() if seat[0] == index}
            chosen = [features[shot_id] for shot_id in eligible[index] if shot_id in chosen_ids]
            if profile is not None and profile.source_policy == "speech_first":
                # Quality chooses eligible material, not a new order of words.
                # Preserve chronological units from each source within a slot.
                chosen.sort(key=lambda row: (row[1].asset_id.partition("#speech-")[0],
                                            float((row[1].metadata or {}).get("source_start", 0))))
            for score, shot, rationale, _ in chosen:
                selected.append(SelectedShot(slot=slot.name, asset_id=shot.asset_id, score=score, rationale=rationale,
                                             selection_basis="measured_features" if rationale else "source_order"))
            if slot.required and len(chosen) < minima[index]:
                missing_required.append(slot.name)
                warnings.append(f"no eligible shot for required slot '{slot.name}'")
        if selected and any(not row.rationale for row in selected):
            warnings.append("部分镜头没有画质、动作或语义评分，按素材与镜头顺序选择；这不代表质量为零。")
        if unavailable:
            warnings.append("场景需要真实测量，缺失或无效的指标不能冒充零分：" + ", ".join(sorted(unavailable)))
        if rejected_quality:
            warnings.append(f"{len(rejected_quality)}个镜头未达到场景最低质量要求，未进入选镜。")
        if profile is not None and profile.preserve_speech_meaning:
            warnings.append("口播语义、完整词句与上下文需由真实转写证据和独立审核确认，选镜分数不代替语义验收。")
        available_frames = sum(features[shot_id][3] for shot_id in used)
        available = clock.seconds(available_frames)
        if template.duration and available_frames < clock.floor(template.duration) and selected:
            warnings.append(f"目标时长 {template.duration:g} 秒，可用入选镜头共 {available:g} 秒；当前将按可用时长制作，不补帧或变速。")
        return AutoEditPlan(template.id, template.version, tuple(selected), tuple(warnings), tuple(missing_required),
                            scenario_binding(profile) if profile else None)


def _allocate_with_minima(capacities: list[int], minima: list[int], total: int) -> list[int]:
    """Proportionally divide whole frames while preserving slot hard minima."""
    if any(minimum > capacity for minimum, capacity in zip(minima, capacities)):
        raise ValueError("入选镜头不满足槽位的最小时长，不能隐式延长素材")
    if total < sum(minima):
        raise ValueError("目标时长不足以容纳每个入选镜头的最小时长，请增加时长或减少镜头")
    if all(minimum == 1 for minimum in minima):
        return allocate_frames(capacities, total)
    allocation = list(minima)
    active = list(range(len(capacities)))
    remaining = total
    while active:
        weight = sum(capacities[index] for index in active)
        ideal = {index: Fraction(remaining * capacities[index], weight) for index in active}
        fixed = [index for index in active if ideal[index] < minima[index]]
        if fixed:
            remaining -= sum(minima[index] for index in fixed)
            active = [index for index in active if index not in fixed]
            continue
        for index in active:
            allocation[index] = ideal[index].numerator // ideal[index].denominator
        remainder = total - sum(allocation)
        ranked = sorted(active, key=lambda index: (-(ideal[index] - allocation[index]), index))
        for index in ranked[:remainder]:
            allocation[index] += 1
        break
    return allocation


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


def shot_features_from_assets(assets: list[Any], *, scenario=None) -> list[ShotFeature]:
    """Build deterministic planner features from tenant-scoped asset rows.

    The API may enqueue a plan before media analysis has finished. Keeping the
    feature extraction here lets the worker re-plan from the committed asset
    metadata immediately before compilation, so a stale fallback plan cannot
    silently become the final Timeline.
    """
    features: list[ShotFeature] = []
    if isinstance(scenario, dict):
        from .scenario_profiles import resolve_scenario_binding
        scenario = resolve_scenario_binding(scenario)
    for asset in assets:
        metadata = getattr(asset, "metadata_json", None) or {}
        analysis = metadata.get("analysis", {}) if isinstance(metadata, dict) else {}
        shot_rows = analysis.get("shots", []) if isinstance(analysis, dict) else []
        speech_units = scenario is not None and scenario.preserve_speech_meaning
        if speech_units:
            from .speech_evidence import speech_windows_from_asset
            windows = speech_windows_from_asset(asset)
            speech_rows = []
            for window in windows:
                covering = [row for row in shot_rows if isinstance(row, dict)
                            and float(row.get("source_start", row.get("start", 0))) <= window["start"]
                            and float(row.get("source_end", row.get("end", 0))) >= window["end"]]
                # Only measured features covering the full sentence are reused.
                # Word alignment is not an invented picture/semantic score.
                raw = covering[0] if covering else {}
                speech_rows.append({**raw, "start": window["start"], "end": window["end"],
                    "source_start": window["start"], "source_end": window["end"],
                    "duration": window["end"] - window["start"], "speech_unit": window})
            shot_rows = speech_rows
        if not shot_rows:
            if speech_units:
                continue
            shot_rows = [{"duration": float(metadata.get("duration", 1.0) or 1.0)}]
        for index, raw_shot in enumerate(shot_rows):
            shot = raw_shot if isinstance(raw_shot, dict) else {}
            if metadata.get("user_tags"):
                shot = {**shot, "_user_tags": list(metadata["user_tags"])}
            features.append(
                ShotFeature(
                    asset_id=f"{asset.id}#{'speech' if speech_units else 'shot'}-{index}",
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
    profile = template_scenario(template)
    by_id = {str(asset.id): asset for asset in assets}
    rows: list[tuple[dict[str, Any], Any, int, int]] = []
    missing: list[str] = []
    for selected_row in selected:
        shot_id = str(selected_row.get("asset_id") or "")
        speech_unit = "#speech-" in shot_id
        if profile is not None and profile.preserve_speech_meaning and not speech_unit:
            raise ValueError("口播场景需要有证据的完整讲话单元，不能将普通镜头当作安全句段")
        asset_id, marker, suffix = shot_id.partition("#speech-" if speech_unit else "#shot-")
        asset = by_id.get(asset_id or shot_id)
        if asset is None:
            missing.append(shot_id)
            continue
        analysis = (asset.metadata_json or {}).get("analysis", {})
        shots = analysis.get("shots", []) if isinstance(analysis, dict) else []
        shot: dict[str, Any] = {}
        if speech_unit:
            if profile is None or not profile.preserve_speech_meaning:
                raise ValueError("speech selections require the bound spoken scenario")
            from .speech_evidence import speech_windows_from_asset
            windows = speech_windows_from_asset(asset)
            if not suffix.isdigit() or int(suffix) >= len(windows):
                raise ValueError("selected speech unit is unavailable in its source-bound analysis")
            window = windows[int(suffix)]
            shot = {"start": window["start"], "end": window["end"], "speech_unit": window}
        elif marker:
            if not suffix.isdigit() or int(suffix) >= max(1, len(shots)):
                raise ValueError("auto-edit selected shot is unavailable in its analyzed source")
            if shots:
                candidate = shots[int(suffix)]
                if not isinstance(candidate, dict):
                    raise ValueError("auto-edit selected shot has invalid analysis metadata")
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
            if speech_unit and (abs(chosen_start - source_start) > 1e-8 or abs(chosen_end - source_end) > 1e-8):
                raise ValueError("spoken edits must keep the complete source-bound speech unit")
            source_start, source_end, duration = chosen_start, chosen_end, chosen_end - chosen_start
        first_frame, last_frame = clock.window(source_start, source_end)
        if speech_unit:
            unit = shot["speech_unit"]
            if ("word_start" not in unit or "word_end" not in unit
                    or clock.seconds(first_frame) > unit["word_start"] + 1e-8
                    or clock.seconds(last_frame) < unit["word_end"] - 1e-8):
                raise ValueError("frame snapping would cut a measured spoken word; more source padding is required")
            selected_row = {**selected_row, "speech_unit": unit}
        rows.append((selected_row, asset, first_frame, last_frame))

    if missing:
        raise ValueError(f"auto-edit selected assets are unavailable: {', '.join(sorted(missing))}")
    if not rows:
        raise ValueError("auto-edit produced no eligible shots")

    requested_duration = float(template.duration or 0.0)
    slots = {slot.name: slot for slot in template.slots}
    capacities, minima = [], []
    for selected_row, _, first, last in rows:
        slot = slots.get(selected_row.get("slot"))
        minimum, capacity = 1, last - first
        if slot is not None:
            minimum = max(1, clock.ceil(slot.constraints.min_duration or 0))
            if slot.constraints.max_duration is not None:
                capacity = min(capacity, clock.floor(slot.constraints.max_duration))
        capacities.append(capacity)
        minima.append(minimum)
    total_frames = min(clock.floor(requested_duration), sum(capacities)) if requested_duration > 0 else sum(capacities)
    allocation = _allocate_with_minima(capacities, minima, total_frames)
    if profile is not None and profile.preserve_speech_meaning and any(
            clip_frames < last - first for (_, _, first, last), clip_frames in zip(rows, allocation)):
        raise ValueError("口播场景不能按比例截断完整讲话，请先选择经审核的完整句段或增加目标时长")
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
                    **({"speech_unit": selected_row["speech_unit"]} if selected_row.get("speech_unit") else {}),
                },
            )
        )
        cursor = end
    if not clips:
        raise ValueError("auto-edit produced no renderable clips")

    timeline = Timeline(
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
            **({"scenario": scenario_binding(profile)} if profile else {}),
        },
    )
    assert_scenario_timeline_sources(timeline, assets, template)
    return timeline


def assert_scenario_timeline_sources(timeline: Timeline, assets: list[Any], template: TemplateDefinition) -> None:
    """Recheck scene claims after manual edits, restore and before rendering.

    The source snapshot is authoritative. A copied clip's old speech metadata
    is not proof that its new source identity/window is safe.
    """
    profile = template_scenario(template)
    if profile is None:
        return
    from .scenario_profiles import resolve_scenario_binding
    value = timeline.metadata.get("scenario")
    if not isinstance(value, dict):
        raise ValueError("scenario timeline is missing its frozen profile binding")
    if scenario_binding(resolve_scenario_binding(value)) != scenario_binding(profile):
        raise ValueError("timeline scenario differs from the frozen template")
    candidates = {row.asset_id: row for row in shot_features_from_assets(assets, scenario=profile)}
    by_id = {str(row.id): row for row in assets}
    clock = FrameClock(timeline.fps)
    for track in timeline.tracks:
        if track.type != TrackType.VIDEO:
            continue
        for clip in track.clips:
            id = str(clip.metadata.get("source_shot_id") or f"{clip.asset_id}#shot-0")
            shot = candidates.get(id)
            if (shot is None or str(clip.asset_id) != id.partition("#speech-" if "#speech-" in id else "#shot-")[0]
                    or str(clip.asset_id) not in by_id or not scenario_feature_eligibility(profile, shot)["eligible"]):
                raise ValueError("scenario clip lacks eligible, source-bound measured evidence")
            raw = shot.metadata or {}
            first, last = clock.window(float(raw.get("source_start", raw.get("start", 0))),
                                      float(raw.get("source_end", raw.get("end", shot.duration))))
            source_end = clip.source_end if clip.source_end is not None else clip.source_start + clip.range.duration
            if (clip.source_start < clock.seconds(first) - 1e-8 or source_end > clock.seconds(last) + 1e-8
                    or abs(source_end - clip.source_start - clip.range.duration) > 1e-8):
                raise ValueError("scenario clip exceeds its measured source or changes speech speed")
            if profile.preserve_speech_meaning:
                unit = raw.get("speech_unit")
                if ("#speech-" not in id or not unit or clip.metadata.get("speech_unit") != unit
                        or abs(clip.source_start - clock.seconds(first)) > 1e-8
                        or abs(source_end - clock.seconds(last)) > 1e-8
                        or clock.seconds(first) > unit["word_start"] + 1e-8
                        or clock.seconds(last) < unit["word_end"] - 1e-8):
                    raise ValueError("spoken clip must preserve the exact complete evidenced speech unit")
