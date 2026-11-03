"""Pure metadata edits. No media IO, provider execution or source mutation."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from timeline_engine import Timeline
from timeline_engine.timing import FrameClock, allocate_frames

from .editing_errors import EditingError
from .subtitle_authoring import subtitle_data, sync_subtitle_anchors
from .composition_layout import layout_sequence, transition_frames


CLIP_ACTIONS = {"trim_clip", "split_clip", "move_clip", "remove_clip", "set_clip_audio"}


def editor_view(timeline: dict) -> dict:
    # Exclude source URIs, metadata, grants, license evidence and arbitrary
    # renderer effects from the client-facing authoring view.
    fields = ("id", "asset_id", "range", "source_start", "source_end", "volume", "opacity", "text", "placement", "style", "transition")
    result = {"fps": timeline["fps"], "duration": timeline["duration"], "resolution": deepcopy(timeline["resolution"]),
        "tracks": [{"id": track["id"], "type": track["type"], "muted": track.get("muted", False),
                    "clips": [{key: deepcopy(clip.get(key)) for key in fields} for clip in track["clips"]]}
                   for track in timeline["tracks"]]}
    for track in result["tracks"]:
        for clip, original in zip(track["clips"], next(row for row in timeline["tracks"] if row["id"] == track["id"])["clips"]):
            if isinstance(clip.get("style"), dict):
                clip["style"].pop("font_file", None)
            if track["type"] == "subtitle":
                clip["subtitle"] = deepcopy(subtitle_data(original))
            fade = next((effect for effect in original.get("effects", []) if effect.get("type") == "audio_fade"), None)
            fit = next((effect for effect in original.get("effects", []) if effect.get("type") == "frame_fit"), None)
            clip["composition"] = {"audio_fade": {key: fade.get(key, 0) for key in ("in_seconds", "out_seconds")} if fade else None,
                                   "fit": fit.get("mode", "contain") if fit else "contain",
                                   "audio_role": original.get("metadata", {}).get("audio_role", "other")}
    return result


def _frame(clock: FrameClock, value: float) -> int:
    result = clock.floor(value)
    if abs(clock.seconds(result) - value) > 1e-7:
        raise EditingError("edit_frame_alignment", "原版本含未对齐帧的区间，请先从当前素材重新建立计划", 422)
    return result


def _sequence(value: dict, track: dict, clock: FrameClock) -> list[dict]:
    videos = [row for row in value["tracks"] if row["type"] == "video" and row["clips"]]
    if track["type"] != "video" or len(videos) != 1:
        raise EditingError("edit_sequence_required", "当前顺接操作需要一条主视频轨；音频和叠加轨请使用各自的区间编辑", 422)
    clips = sorted(track["clips"], key=lambda row: row["range"]["start"])
    assets = {row["id"]: row for row in value["assets"]}
    cursor = 0
    composition = value["metadata"].get("composition") or {}
    for clip in clips:
        if assets.get(clip.get("asset_id"), {}).get("type") != "video":
            raise EditingError("edit_video_sequence_required", "当前顺接源区间操作仅用于视频；图片需使用明确的画面停留时长编辑", 422)
        overlap = transition_frames(clip, clock)
        if overlap and (not isinstance(composition, dict) or composition.get("transition_policy") != "overlap"):
            raise EditingError("edit_legacy_transition", "旧转场的时间语义不明确，请明确移除旧转场后再调整片段", 422)
        if _frame(clock, clip["range"]["start"]) != cursor - overlap:
            raise EditingError("edit_sequence_has_gaps", "主视频含空隙或重叠，不能隐式覆盖，请先审阅轨道区间", 422)
        cursor = _frame(clock, clip["range"]["end"])
        # Never turn a legacy speed/still/transition clip into normal video
        # by quietly discarding its meaning.
        if any(effect.get("type") != "frame_fit" for effect in clip.get("effects", [])):
            raise EditingError("edit_sequence_effects", "主视频含其他效果，需先明确处理其区间后再顺接裁切", 422)
        if clip.get("source_end") is None or abs(clip["source_end"] - clip["source_start"] - clip["range"]["end"] + clip["range"]["start"]) > 1e-7:
            raise EditingError("edit_source_timing", "主视频源区间与输出时长不同，不能隐式变速或延长", 422)
    track["clips"] = clips
    return clips


def _ripple(value: dict, track: dict, clock: FrameClock) -> None:
    layout_sequence(value, track, clock)
    for other in value["tracks"]:
        if other is track:
            continue
        if any(clip["range"]["end"] > value["duration"] + 1e-8 and not subtitle_data(clip).get("anchor") for clip in other["clips"]):
            raise EditingError("edit_other_track_bounds", "其他轨道超出新时长；请先调整这些片段，原内容没有被裁掉", 422)


def _protect_locks(before: dict, after: dict, rows: list[dict]) -> None:
    original = {clip["id"]: (track["id"], clip) for track in before["tracks"] for clip in track["clips"]}
    changed = {clip["id"]: (track["id"], clip) for track in after["tracks"] for clip in track["clips"]}
    for row in rows:
        if row["locked"] and original.get(row["clip_id"]) != changed.get(row["clip_id"]):
            raise EditingError("edit_selection_locked", "这次修改会改变锁定镜头的内容或位置，请先解锁")


def _validated(value: dict) -> Timeline:
    if any(subtitle_data(clip).get("anchor") for track in value["tracks"] if track["type"] == "subtitle" for clip in track["clips"]):
        return sync_subtitle_anchors(value)[0]
    try:
        return Timeline.model_validate(value)
    except ValueError as exc:
        raise EditingError("edit_clip_invalid", "片段修改产生无效区间，原版本已保留", 422) from exc


def apply_clip_action(canonical: Timeline, rows: list[dict], action, *, intent: str) -> Timeline:
    before = canonical.model_dump(mode="json"); value = deepcopy(before)
    clock = FrameClock(canonical.fps)
    found = [(track, clip) for track in value["tracks"] for clip in track["clips"] if clip["id"] == action.clip_id]
    if not found:
        raise EditingError("edit_clip_not_found", "未找到当前版本的片段", 404)
    track, clip = found[0]
    if action.op == "set_clip_audio":
        asset = next((row for row in value["assets"] if row["id"] == clip.get("asset_id")), None)
        if track["type"] not in {"video", "audio"} or not asset or asset["type"] not in {"video", "audio"}:
            raise EditingError("edit_clip_has_no_audio", "该片段没有可调节的媒体原声音量", 422)
        clip["volume"] = action.volume
    else:
        clips = _sequence(value, track, clock)
        index = clips.index(clip)
        if action.op == "trim_clip":
            row = next((row for row in rows if row["clip_id"] == clip["id"]), None)
            if row is None:
                raise EditingError("edit_source_window_missing", "该片段缺少可信源区间", 422)
            if action.source_start < row["window_start"] - 1e-8 or action.source_end > row["window_end"] + 1e-8:
                raise EditingError("edit_source_window_exceeded", "裁切不能超出分析镜头的可用源区间", 422)
            try:
                first, last = clock.window(action.source_start, action.source_end)
            except ValueError as exc:
                raise EditingError("edit_clip_too_short", str(exc), 422) from exc
            clip.update(source_start=clock.seconds(first), source_end=clock.seconds(last))
        elif action.op == "split_clip":
            first = _frame(clock, clip["source_start"]); last = _frame(clock, clip["source_end"])
            cut = first + clock.floor(action.offset_seconds)
            if not first < cut < last:
                raise EditingError("edit_split_bounds", "分割点需位于片段内部，两边各至少保留一帧", 422)
            right = deepcopy(clip)
            right["id"] = "cut_" + uuid5(NAMESPACE_URL, intent + ":" + clip["id"]).hex
            right["transition"] = None  # The new cut is hard; the old incoming transition stays on the left.
            right["metadata"]["split_from_clip_ids"] = list(dict.fromkeys([*(clip.get("metadata", {}).get("split_from_clip_ids") or []), clip["id"]]))
            clip["source_end"] = clock.seconds(cut); right["source_start"] = clock.seconds(cut)
            clips.insert(index + 1, right)
        elif action.op == "move_clip":
            if action.to_index >= len(clips):
                raise EditingError("edit_order_bounds", "目标位置超出当前片段数量", 422)
            clips.pop(index); clips.insert(action.to_index, clip)
        elif action.op == "remove_clip":
            if len(clips) == 1:
                raise EditingError("edit_last_clip", "至少保留一个主视频片段", 422)
            clips.remove(clip)
        _ripple(value, track, clock)
    value["metadata"]["precise_clip_edits"] = True
    if sum(len(track["clips"]) for track in value["tracks"]) > 10000:
        raise EditingError("edit_clip_limit", "单个编辑版本最多保存10000个片段，请拆分项目", 422)
    _protect_locks(before, value, rows)
    return _validated(value)


def resize_precise_sequence(canonical: Timeline, target: float | None) -> Timeline:
    value = canonical.model_dump(mode="json"); clock = FrameClock(canonical.fps)
    track = next(row for row in value["tracks"] if row["type"] == "video" and row["clips"])
    clips = _sequence(value, track, clock)
    capacities = [_frame(clock, clip["source_end"]) - _frame(clock, clip["source_start"]) for clip in clips]
    overlap = sum(transition_frames(clip, clock) for clip in clips)
    total = min(clock.floor(target) + overlap, sum(capacities)) if target is not None else sum(capacities)
    try:
        allocation = allocate_frames(capacities, total)
    except ValueError as exc:
        raise EditingError("edit_duration_too_short", str(exc), 422) from exc
    for clip, count in zip(clips, allocation):
        clip["source_end"] = clock.seconds(_frame(clock, clip["source_start"]) + count)
    _ripple(value, track, clock)
    return _validated(value)
