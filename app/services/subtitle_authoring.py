"""Frozen ASR, editable cues, source anchors and deterministic text exports.

All work is metadata-only. This module never calls recognition or rendering.
"""
from collections import defaultdict
from bisect import bisect_right
from copy import deepcopy
import hashlib
from html import escape
import json
import math
from uuid import NAMESPACE_URL, uuid5

from timeline_engine import Clip, Placement, TextStyle, Timeline
from timeline_engine.timing import FrameClock

from .editing_errors import EditingError

SUBTITLE_ACTIONS = {"import_transcript", "save_subtitle", "delete_subtitle"}
NAMESPACE = "_subtitle"


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value: str) -> str:
    if not value.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise EditingError("subtitle_text_invalid", "字幕不能留空或包含控制字符", 422)
    return value.replace("\r\n", "\n").replace("\r", "\n")


def capture_transcript(analysis: dict) -> dict | None:
    transcript = analysis.get("transcription")
    if not isinstance(transcript, dict) or transcript.get("status") not in {"ok", "partial"}:
        return None
    aligned = transcript.get("words") is not None
    if aligned:
        from .analysis_contract import validate_hosted_analysis
        from .providers import ProviderExecutionError
        try:
            transcript = validate_hosted_analysis(analysis, source_uri=analysis["source"]["uri"])["transcription"]
        except (KeyError, TypeError, ProviderExecutionError) as exc:
            raise EditingError("subtitle_transcript_invalid", "词级原文未通过来源与时间校验，请检查分析结果", 422) from exc
    segments = transcript.get("segments") or []
    if not isinstance(segments, list) or len(segments) > 20000:
        raise EditingError("subtitle_transcript_invalid", "识别原文格式无效，请保留原分析并检查来源", 422)
    result = {key: deepcopy(transcript.get(key)) for key in ("status", "provider", "version", "language")}
    result["schema_version"] = "transcript/2" if aligned else "transcript/1"
    if aligned:
        result["words"] = deepcopy(transcript["words"])
        result["word_alignment"] = deepcopy(transcript["word_alignment"])
    result["segments"] = []
    for index, row in enumerate(segments):
        try:
            start, end, text = float(row["start"]), float(row["end"]), row["text"]
            if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end or not isinstance(text, str) or len(text) > 20000:
                raise ValueError("invalid segment")
            _text(text)
        except (KeyError, TypeError, ValueError) as exc:
            raise EditingError("subtitle_transcript_invalid", "识别原文含无效时间或文字，请检查分析结果", 422) from exc
        result["segments"].append({"id": row.get("id", f"segment-{index}") if aligned else f"segment-{index}",
            "start": start, "end": end, "text": text,
            **({key: row[key] for key in ("speaker_id", "boundary_safe") if key in row} if aligned else {})})
    result["hash"] = _hash(result)
    return result


def subtitle_data(clip: dict) -> dict:
    value = (clip.get("metadata") or {}).get(NAMESPACE)
    return value if isinstance(value, dict) else {}


def _track(value: dict, track_id: str) -> dict:
    track = next((row for row in value["tracks"] if row["id"] == track_id), None)
    if track and track["type"] != "subtitle":
        raise EditingError("subtitle_track_type", "所选轨道不是字幕轨", 422)
    if track is None:
        if len(value["tracks"]) >= 64:
            raise EditingError("subtitle_track_limit", "单版本最多64条轨道，请合并或拆分项目", 422)
        track = {"id": track_id, "type": "subtitle", "clips": [], "muted": False, "metadata": {}}
        value["tracks"].append(track)
    return track


def _range(clock: FrameClock, start: float, end: float, duration: float) -> tuple[float, float]:
    if start < 0 or end > duration + 1e-8:
        raise EditingError("subtitle_time_bounds", "字幕起止不能超出当前成片时长", 422)
    try:
        first, last = clock.window(start, end)
    except ValueError as exc:
        raise EditingError("subtitle_time_bounds", "字幕起止需有序，且至少保留当前帧率的一帧", 422) from exc
    return clock.seconds(first), clock.seconds(last)


def _placement(value: dict, style: dict, position: dict | None = None) -> dict:
    width, height = value["resolution"]["width"], value["resolution"]["height"]
    position = position or {"x": round(width * 0.05), "y": min(round(height * 0.85), height - style["font_size"])}
    if not (0 <= position["x"] < width and 0 <= position["y"] and position["y"] + style["font_size"] <= height):
        raise EditingError("subtitle_position_bounds", "字幕位置或字号超出当前画布；请调整字号和位置", 422)
    return Placement(**position).model_dump(mode="json")


def _primary_clips(value: dict) -> list[dict]:
    return [clip for track in value["tracks"] if track["type"] == "video" for clip in track["clips"]]


def _part(clip: dict, source_start: float, source_end: float) -> dict:
    return {"clip_id": clip["id"], "asset_id": clip["asset_id"], "source_start": source_start, "source_end": source_end}


def _output(clip: dict, start: float, end: float) -> tuple[float, float]:
    source_end = clip.get("source_end")
    if source_end is None or abs(source_end - clip["source_start"] - clip["range"]["end"] + clip["range"]["start"]) > 1e-7:
        raise EditingError("subtitle_anchor_speed", "关联字幕需要源区间与输出时长一致；不能猜测变速后的词级时间", 422)
    return clip["range"]["start"] + start - clip["source_start"], clip["range"]["start"] + end - clip["source_start"]


def _import_key(origin: dict, clip_id: str) -> str:
    return _hash([origin["asset_id"], origin["transcript_hash"], origin["segment_id"], clip_id])


def _bounded(value: dict) -> Timeline:
    if sum(len(track["clips"]) for track in value["tracks"]) > 10000 or len(json.dumps(value, ensure_ascii=False).encode()) > 16 * 1024 * 1024:
        raise EditingError("subtitle_size_limit", "单版本最多10000个片段且快照不超过16MiB，请拆分项目", 422)
    for track in value["tracks"]:
        if track["type"] != "subtitle":
            continue
        track["clips"].sort(key=lambda clip: (clip["range"]["start"], clip["id"]))
        end = 0
        for clip in track["clips"]:
            if clip["range"]["start"] < end - 1e-8:
                raise EditingError("subtitle_overlap", "同一字幕轨的句子不能重叠，请调整时间或使用另一条字幕轨", 422)
            end = clip["range"]["end"]
            style = clip.get("style") or TextStyle().model_dump(mode="json")
            _placement(value, style, clip.get("placement"))
    try:
        return Timeline.model_validate(value)
    except ValueError as exc:
        raise EditingError("subtitle_invalid", "字幕时间线未通过校验，原版本未改变", 422) from exc


def import_transcript(canonical: Timeline, sources: list[dict], action) -> tuple[Timeline, list[str]]:
    value = canonical.model_dump(mode="json"); track = _track(value, action.track_id); clock = FrameClock(canonical.fps)
    chosen = set(action.asset_ids) or {clip.get("asset_id") for clip in _primary_clips(value)}
    known = {row["id"]: row for row in sources}
    if not chosen <= set(known):
        raise EditingError("subtitle_source_not_found", "字幕来源不属于当前编辑版本", 404)
    imported = set(track.setdefault("metadata", {}).get("subtitle_imported_keys") or [])
    groups = defaultdict(list); warnings = []; total_text = 0
    clips = _primary_clips(value); pieces_count = 0
    for asset_id in sorted(chosen):
        snapshot = known[asset_id].get("transcript")
        if not snapshot or not snapshot.get("segments"):
            warnings.append(f"素材 {known[asset_id]['name']} 没有可用的已识别原文，未调用新识别。")
            continue
        segments = sorted(snapshot["segments"], key=lambda row: row["start"])
        if any(left["end"] > right["start"] + 1e-8 for left, right in zip(segments, segments[1:])):
            raise EditingError("subtitle_transcript_overlap", "识别原文存在重叠句子，请先按原文手工整理；未覆盖现有字幕", 422)
        starts = [row["start"] for row in segments]
        for clip in clips:
            if clip.get("asset_id") != asset_id:
                continue
            clip_end = clip.get("source_end") or clip["source_start"] + clip["range"]["end"] - clip["range"]["start"]
            index = max(0, bisect_right(starts, clip["source_start"]) - 1)
            while index < len(segments) and segments[index]["start"] < clip_end:
                segment = segments[index]; index += 1
                origin = {"asset_id": asset_id, "segment_id": segment["id"], "transcript_hash": snapshot["hash"],
                    "text": segment["text"], "start": segment["start"], "end": segment["end"],
                    "language": snapshot.get("language"), "provider": snapshot.get("provider")}
                if _import_key(origin, clip["id"]) in imported:
                    continue
                start = max(segment["start"], clip["source_start"])
                end = min(segment["end"], clip.get("source_end") or clip["source_start"] + clip["range"]["end"] - clip["range"]["start"])
                if end <= start:
                    continue
                try:
                    first, last = clock.window(start, end)
                except ValueError:
                    continue
                start, end = clock.seconds(first), clock.seconds(last)
                out_start, out_end = _output(clip, start, end)
                groups[(asset_id, segment["id"])].append({"origin": origin, "parts": [_part(clip, start, end)],
                    "start": out_start, "end": out_end})
                pieces_count += 1
                if pieces_count > 10000:
                    raise EditingError("subtitle_size_limit", "导入关联片段超过10000个，请拆分项目", 422)
    for pieces in groups.values():
        pieces.sort(key=lambda row: row["start"]); runs = []
        for piece in pieces:
            if runs and abs(runs[-1]["end"] - piece["start"]) < 1e-8 and abs(runs[-1]["parts"][-1]["source_end"] - piece["parts"][0]["source_start"]) < 1e-8:
                runs[-1]["parts"].extend(piece["parts"]); runs[-1]["end"] = piece["end"]
            else:
                runs.append(piece)
        for run in runs:
            origin = run["origin"]; total_text += len(origin["text"].encode())
            if total_text > 4 * 1024 * 1024:
                raise EditingError("subtitle_size_limit", "本次导入文字过多，请分项目处理", 422)
            keys = [_import_key(origin, part["clip_id"]) for part in run["parts"]]
            cue_id = "cue_" + uuid5(NAMESPACE_URL, "subtitle:" + track["id"] + ":" + ":".join(keys)).hex
            style = TextStyle().model_dump(mode="json")
            start, end = _range(clock, run["start"], run["end"], value["duration"])
            partial = abs(sum(part["source_end"] - part["source_start"] for part in run["parts"]) - origin["end"] + origin["start"]) > 1 / canonical.fps + 1e-8
            cue = Clip(id=cue_id, range={"start": start, "end": end}, text=_text(origin["text"]), style=style,
                placement=_placement(value, style), metadata={NAMESPACE: {"schema_version": "subtitle/1", "origin": origin,
                    "anchor": {"parts": run["parts"]}, "partial": partial}}).model_dump(mode="json")
            track["clips"].append(cue); imported.update(keys)
            if partial:
                warnings.append("有原句只采用了部分音频，字幕保留整句原文，请人工修订，不猜测词级切分。")
    track["metadata"]["subtitle_imported_keys"] = sorted(imported)
    value["metadata"]["precise_clip_edits"] = True
    return _bounded(value), list(dict.fromkeys(warnings))


def save_subtitle(canonical: Timeline, action, *, intent: str) -> Timeline:
    value = canonical.model_dump(mode="json"); clock = FrameClock(canonical.fps)
    track = _track(value, action.track_id)
    existing = next((clip for clip in track["clips"] if clip["id"] == action.cue_id), None)
    if action.cue_id and existing is None:
        raise EditingError("subtitle_cue_not_found", "未找到当前版本的这句字幕", 404)
    start, end = _range(clock, action.start, action.end, value["duration"])
    cue = deepcopy(existing) if existing else Clip(id="cue_" + uuid5(NAMESPACE_URL, intent).hex,
        text=action.text, range={"start": start, "end": end}).model_dump(mode="json")
    cue.update(text=_text(action.text), range={"start": start, "end": end})
    data = deepcopy(subtitle_data(cue))
    data.setdefault("schema_version", "subtitle/1")
    if action.anchor_mode == "absolute":
        data.pop("anchor", None)
    elif action.anchor_mode == "keep" and existing and data.get("anchor"):
        if abs(start - existing["range"]["start"]) > 1e-8 or abs(end - existing["range"]["end"]) > 1e-8:
            raise EditingError("subtitle_anchor_time_change", "调整关联字幕时间前请选择固定成片时间，或选择单个片段跟随", 422)
    elif action.anchor_mode == "clip":
        clip = next((clip for clip in _primary_clips(value) if clip["id"] == action.anchor_clip_id), None)
        if clip is None or start < clip["range"]["start"] - 1e-8 or end > clip["range"]["end"] + 1e-8:
            raise EditingError("subtitle_anchor_bounds", "关联字幕需位于所选视频片段内；跨片段请保持固定时间或按句拆分", 422)
        _output(clip, clip["source_start"], clip["source_end"])
        data["anchor"] = {"parts": [_part(clip, clip["source_start"] + start - clip["range"]["start"], clip["source_start"] + end - clip["range"]["start"])]}
    cue["metadata"][NAMESPACE] = data
    data["edited"] = bool(data.get("origin") and cue["text"] != _text(data["origin"]["text"]))
    if action.style:
        prior = cue.get("style") or {}
        cue["style"] = TextStyle(**{**prior, **action.style.model_dump(mode="json")}).model_dump(mode="json")
    style = cue.get("style") or TextStyle().model_dump(mode="json")
    cue["style"] = style
    cue["placement"] = _placement(value, style, action.position.model_dump(mode="json") if action.position else cue.get("placement"))
    if existing:
        track["clips"][track["clips"].index(existing)] = cue
    else:
        track["clips"].append(cue)
    value["metadata"]["precise_clip_edits"] = True
    return _bounded(value)


def delete_subtitle(canonical: Timeline, cue_id: str) -> Timeline:
    value = canonical.model_dump(mode="json")
    for track in value["tracks"]:
        if track["type"] == "subtitle":
            cue = next((clip for clip in track["clips"] if clip["id"] == cue_id), None)
            if cue:
                track["clips"].remove(cue)
                # Imported keys are deliberately retained as deletion tombstones.
                return _bounded(value)
    raise EditingError("subtitle_cue_not_found", "未找到当前版本的这句字幕", 404)


def sync_subtitle_anchors(canonical: Timeline | dict) -> tuple[Timeline, list[str]]:
    value = canonical.model_dump(mode="json") if isinstance(canonical, Timeline) else deepcopy(canonical)
    videos = _primary_clips(value); warnings = []; fps = value["fps"]; clock = FrameClock(fps)
    for track in value["tracks"]:
        if track["type"] != "subtitle":
            continue
        kept = []; imported = set(track.setdefault("metadata", {}).get("subtitle_imported_keys") or [])
        for cue in track["clips"]:
            data = subtitle_data(cue); anchor = data.get("anchor")
            if not anchor:
                kept.append(cue); continue
            pieces = {}
            for part in anchor["parts"]:
                for clip in videos:
                    ancestors = (clip.get("metadata") or {}).get("split_from_clip_ids") or []
                    if (clip["id"] != part["clip_id"] and part["clip_id"] not in ancestors) or clip.get("asset_id") != part["asset_id"]:
                        continue
                    start = max(clip["source_start"], part["source_start"])
                    end = min(clip.get("source_end") or clip["source_start"], part["source_end"])
                    if end - start < 1 / fps - 1e-8:
                        continue
                    out_start, out_end = _output(clip, start, end)
                    pieces[(clip["id"], start, end)] = {"part": _part(clip, start, end), "start": out_start, "end": out_end}
            rows = sorted(pieces.values(), key=lambda row: row["start"])
            if not rows:
                warnings.append("有随源字幕的音频区间已移除，该句从当前版移除；原文和历史修订仍保留。")
                continue
            if any(abs(left["end"] - right["start"]) > 1e-8 for left, right in zip(rows, rows[1:])):
                raise EditingError("subtitle_anchor_discontinuous", "片段调整使关联字幕不连续，请先把该句改为固定时间或分句处理", 422)
            start, end = _range(clock, rows[0]["start"], rows[-1]["end"], value["duration"])
            cue["range"] = {"start": start, "end": end}; data["anchor"] = {"parts": [row["part"] for row in rows]}
            origin = data.get("origin")
            if origin:
                imported.update(_import_key(origin, row["part"]["clip_id"]) for row in rows)
                data["partial"] = abs(sum(row["end"] - row["start"] for row in rows) - origin["end"] + origin["start"]) > 1 / fps + 1e-8
                data["partial"] = data["partial"] or any(abs(left["part"]["source_end"] - right["part"]["source_start"]) > 1e-8 for left, right in zip(rows, rows[1:]))
                if data["partial"]:
                    warnings.append("有随源字幕只覆盖了原句的部分音频，请人工审阅修订文字。")
            kept.append(cue)
        track["clips"] = kept; track["metadata"]["subtitle_imported_keys"] = sorted(imported)
    return _bounded(value), list(dict.fromkeys(warnings))


def subtitle_export(canonical: Timeline, *, revision_id: str, timeline_hash: str, format: str, track_id: str | None = None) -> dict:
    tracks = [track for track in canonical.tracks if track.type.value == "subtitle" and not track.muted and (track_id is None or track.id == track_id)]
    if track_id and not tracks:
        raise EditingError("subtitle_track_not_found", "未找到可导出的字幕轨", 404)
    cues = sorted((clip for track in tracks for clip in track.clips), key=lambda cue: (cue.range.start, cue.range.end, cue.id))
    if not cues:
        raise EditingError("subtitle_export_empty", "当前版本没有可导出的非静音字幕", 422)
    def timestamp(seconds):
        ms = int(math.floor(seconds * 1000 + 0.5)); hours, ms = divmod(ms, 3600000); minutes, ms = divmod(ms, 60000); secs, ms = divmod(ms, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{',' if format == 'srt' else '.'}{ms:03d}"
    blocks = []
    for index, cue in enumerate(cues):
        text = "\n".join(escape(line, quote=False) if line.strip() else "\u00a0" for line in _text(cue.text).split("\n"))
        blocks.append(f"{index + 1}\n{timestamp(cue.range.start)} --> {timestamp(cue.range.end)}\n{text}")
    content = ("WEBVTT\n\n" if format == "vtt" else "") + "\n\n".join(blocks) + "\n\n"
    return {"revision_id": revision_id, "timeline_hash": timeline_hash, "format": format,
        "filename": f"subtitles-{revision_id}.{format}", "encoding": "UTF-8", "cue_count": len(cues),
        "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "warnings": ["本次导出为文字及时间，不包含烧录字幕的字号、颜色和位置；多条轨道按时间合并。"]}
