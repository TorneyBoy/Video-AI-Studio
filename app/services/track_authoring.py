"""Bounded audio/overlay/transition commands; media execution stays external."""
from copy import deepcopy
import math
from uuid import NAMESPACE_URL, uuid5

from timeline_engine import AssetRef, Clip, Timeline, validate_timeline
from timeline_engine.timing import FrameClock

from .composition_layout import layout_sequence
from .editing_errors import EditingError
from .subtitle_authoring import subtitle_data, sync_subtitle_anchors

TRACK_ACTIONS = {"save_audio_clip", "save_overlay_clip", "delete_layer_clip", "set_track_mute", "move_track", "delete_layer_track", "set_transition", "set_video_fit"}


def _track(value: dict, track_id: str, kind: str | None = None) -> dict:
    track = next((row for row in value["tracks"] if row["id"] == track_id), None)
    if track is None:
        if kind is None:
            raise EditingError("edit_track_not_found", "未找到该版本的轨道", 404)
        if len(value["tracks"]) >= 64:
            raise EditingError("edit_track_limit", "单版本最多64条轨道，请拆分项目", 422)
        track = {"id": track_id, "type": kind, "muted": False, "clips": [], "metadata": {"z_index": len(value["tracks"])}}
        value["tracks"].append(track)
    if kind is not None and track["type"] != kind:
        raise EditingError("edit_track_type", "轨道类型与本次编辑不符，请选择同类或新建轨道", 422)
    return track


def _clip(value: dict, clip_id: str):
    for track in value["tracks"]:
        for clip in track["clips"]:
            if clip["id"] == clip_id:
                return track, clip
    raise EditingError("edit_clip_not_found", "未找到当前版本的片段", 404)


def _duration(source: dict) -> float:
    metadata = source["metadata"]; analysis = metadata.get("analysis") or {}
    raw = (analysis.get("media") or {}).get("duration_seconds") or metadata.get("duration")
    try:
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("invalid duration")
    except (ValueError, TypeError) as exc:
        raise EditingError("edit_media_facts_required", "请先完成素材媒体分析，不能猜测音频或视频长度", 422) from exc
    return value


def _protect_locks(before: dict, after: dict, rows: list[dict]) -> None:
    original = {clip["id"]: (track["id"], track["muted"], clip) for track in before["tracks"] for clip in track["clips"]}
    updated = {clip["id"]: (track["id"], track["muted"], clip) for track in after["tracks"] for clip in track["clips"]}
    for row in rows:
        if row["locked"] and original.get(row["clip_id"]) != updated.get(row["clip_id"]):
            raise EditingError("edit_selection_locked", "这次修改会改变锁定镜头的内容或位置，请先解锁")


def apply_track_action(canonical: Timeline, sources: list[dict], rows: list[dict], action, *, intent: str) -> Timeline:
    before = canonical.model_dump(mode="json"); value = deepcopy(before); clock = FrameClock(canonical.fps)
    if action.op in {"save_audio_clip", "save_overlay_clip"}:
        kind = "audio" if action.op == "save_audio_clip" else "overlay"
        track = _track(value, action.track_id, kind)
        existing = None
        if action.clip_id:
            current_track, existing = _clip(value, action.clip_id)
            if current_track["type"] != kind:
                raise EditingError("edit_clip_track_mismatch", "现有片段与目标轨道类型不同，原记录未改变", 422)
            if existing.get("effects") and any(effect.get("type") not in ({"audio_fade"} if kind == "audio" else {"frame_fit"}) for effect in existing["effects"]):
                raise EditingError("edit_unknown_effects", "该片段含其他效果，不能在此表单隐式移除", 422)
        source = next((row for row in sources if row["id"] == action.asset_id), None)
        if source is None:
            raise EditingError("edit_source_unavailable", "请先把当前项目素材明确加入本版本", 404)
        asset_type = source["kind"].split("/", 1)[0]
        if asset_type not in ({"audio", "video"} if kind == "audio" else {"image", "video"}):
            raise EditingError("edit_layer_asset_type", "音轨需要音频/有声视频，叠加需要图片/视频", 422)
        if action.end > canonical.duration + 1e-8:
            raise EditingError("edit_layer_bounds", "附加轨片段不能超出成片时长", 422)
        try:
            first, last = clock.window(action.start, action.end)
        except ValueError as exc:
            raise EditingError("edit_layer_bounds", "片段起止需有序，且至少保留一帧", 422) from exc
        count = last - first; source_first = clock.ceil(action.source_start)
        source_end = None
        if asset_type == "image":
            if action.source_start:
                raise EditingError("edit_image_source_time", "图片按成片区间停留，不接受素材起点", 422)
        else:
            if source_first + count > clock.floor(_duration(source)):
                raise EditingError("edit_source_window_exceeded", "源区间不足以覆盖成片片段，不会隐式循环、拉伸或补帧", 422)
            if kind == "audio" and asset_type == "video" and not (source["metadata"].get("analysis") or {}).get("audio"):
                raise EditingError("edit_audio_facts_required", "该视频尚无可用音轨的分析信息，请先确认有声媒体", 422)
            source_end = clock.seconds(source_first + count)
        effects = []
        metadata = deepcopy((existing or {}).get("metadata") or {})
        if kind == "audio":
            fade_in, fade_out = clock.floor(action.fade_in), clock.floor(action.fade_out)
            if (action.fade_in and fade_in < 1) or (action.fade_out and fade_out < 1) or fade_in + fade_out > count:
                raise EditingError("edit_audio_fade_bounds", "淡入淡出各需至少一帧，合计不能超过当前音频片段", 422)
            if fade_in or fade_out:
                effects.append({"type": "audio_fade", "schema_version": "audio-envelope/1", "in_seconds": clock.seconds(fade_in), "out_seconds": clock.seconds(fade_out)})
            metadata["audio_role"] = action.role
            volume, opacity, placement = action.volume, 1, None
        else:
            placement = action.placement.model_dump(mode="json"); resolution = value["resolution"]
            if placement["x"] + placement["width"] > resolution["width"] or placement["y"] + placement["height"] > resolution["height"]:
                raise EditingError("edit_overlay_bounds", "叠加位置和尺寸必须在当前画布内", 422)
            effects.append({"type": "frame_fit", "schema_version": "frame-fit/1", "mode": action.fit})
            volume, opacity = 0, action.opacity  # Overlay audio is added explicitly through an audio track.
        clip = Clip(id=action.clip_id or "layer_" + uuid5(NAMESPACE_URL, intent).hex, asset_id=source["id"],
            range={"start": clock.seconds(first), "end": clock.seconds(last)}, source_start=clock.seconds(source_first), source_end=source_end,
            volume=volume, opacity=opacity, placement=placement, effects=effects, metadata=metadata).model_dump(mode="json")
        if existing:
            current_track["clips"].remove(existing); track["clips"].append(clip)
        else:
            track["clips"].append(clip)
        if not any(asset["id"] == source["id"] for asset in value["assets"]):
            value["assets"].append(AssetRef(id=source["id"], type=asset_type, uri=source["uri"], metadata={"name": source["name"]}).model_dump(mode="json"))
        track["clips"].sort(key=lambda clip: (clip["range"]["start"], clip["id"]))
    elif action.op == "delete_layer_clip":
        track, clip = _clip(value, action.clip_id)
        if track["type"] not in {"audio", "overlay"}:
            raise EditingError("edit_layer_track_required", "此删除操作仅用于附加音频或叠加片段", 422)
        track["clips"].remove(clip)
    elif action.op in {"set_track_mute", "move_track", "delete_layer_track"}:
        track = _track(value, action.track_id)
        if action.op == "set_track_mute":
            track["muted"] = action.muted
        elif action.op == "delete_layer_track":
            if track["type"] not in {"audio", "overlay"}:
                raise EditingError("edit_layer_track_required", "仅附加音轨和叠加轨可在此整体删除", 422)
            value["tracks"].remove(track)
        else:
            if track["type"] == "video" or not 1 <= action.to_index < len(value["tracks"]):
                raise EditingError("edit_track_order_bounds", "主视频轨保持在首位，其他轨道可调整顺序", 422)
            value["tracks"].remove(track); value["tracks"].insert(action.to_index, track)
            for index, item in enumerate(value["tracks"]):
                if item["type"] in {"overlay", "subtitle"}:
                    item["metadata"]["z_index"] = index
    elif action.op in {"set_transition", "set_video_fit"}:
        track, clip = _clip(value, action.clip_id)
        if track["type"] != "video":
            raise EditingError("edit_primary_video_required", "此操作需要主视频片段", 422)
        if action.op == "set_video_fit":
            clip["effects"] = [effect for effect in clip["effects"] if effect.get("type") != "frame_fit"]
            if action.mode != "contain":
                clip["effects"].append({"type": "frame_fit", "schema_version": "frame-fit/1", "mode": action.mode})
        else:
            ordered = sorted(track["clips"], key=lambda clip: clip["range"]["start"]); track["clips"] = ordered
            frames = clock.floor(action.duration)
            if action.type != "none" and frames < 1:
                raise EditingError("transition_duration_bounds", "转场至少保留当前帧率的一帧", 422)
            clip["transition"] = None if action.type == "none" else {"type": action.type, "duration": clock.seconds(frames)}
            if any(item.get("source_end") is None for item in ordered):
                raise EditingError("transition_source_timing", "转场需要具有明确源区间的主视频片段", 422)
            layout_sequence(value, track, clock)
            for other in value["tracks"]:
                if other is not track and any(item["range"]["end"] > value["duration"] + 1e-8 and not subtitle_data(item).get("anchor") for item in other["clips"]):
                    raise EditingError("edit_other_track_bounds", "转场缩短了成片，其他固定时间轨道超出新时长；请先调整这些片段", 422)
    value["metadata"]["precise_clip_edits"] = True
    _protect_locks(before, value, rows)
    canonical, _ = sync_subtitle_anchors(value)
    report = validate_timeline(canonical)
    if not report.valid:
        raise EditingError("edit_track_invalid", "轨道区间重叠或类型/转场不符，请调整；原版本未改变", 422, {"issues": report.as_dict()["issues"]})
    return canonical
