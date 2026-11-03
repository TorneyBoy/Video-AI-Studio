"""Explicit overlap transitions: output is shortened, never padded or frozen."""
from timeline_engine.timing import FrameClock

from .editing_errors import EditingError


def frame(clock: FrameClock, seconds: float) -> int:
    value = clock.floor(seconds)
    if abs(clock.seconds(value) - seconds) > 1e-7:
        raise EditingError("edit_frame_alignment", "原版本含未对齐帧的区间，请先建立按帧对齐的计划", 422)
    return value


def transition_frames(clip: dict, clock: FrameClock) -> int:
    transition = clip.get("transition")
    return frame(clock, transition["duration"]) if transition else 0


def layout_sequence(value: dict, track: dict, clock: FrameClock) -> None:
    clips = track["clips"]; cursor = 0
    sizes = [frame(clock, clip["source_end"]) - frame(clock, clip["source_start"]) for clip in clips]
    overlaps = [transition_frames(clip, clock) for clip in clips]
    if not clips or any(size < 1 for size in sizes):
        raise EditingError("edit_clip_too_short", "每个主视频片段至少保留一帧", 422)
    if overlaps[0]:
        raise EditingError("transition_first_clip", "第一个片段没有前一镜头，请先移除其转场", 422)
    for index, clip in enumerate(clips):
        overlap = overlaps[index]
        if overlap and (overlap >= sizes[index] or overlap >= sizes[index - 1]):
            raise EditingError("transition_duration_bounds", "转场需短于相邻两段，且至少一帧", 422)
        outgoing = overlaps[index + 1] if index + 1 < len(clips) else 0
        if overlap + outgoing > sizes[index]:
            raise EditingError("transition_three_way_overlap", "同一片段的入/出转场不能产生三段同时叠加，请缩短转场", 422)
        start = cursor - overlap
        clip["range"] = {"start": clock.seconds(start), "end": clock.seconds(start + sizes[index])}
        cursor = start + sizes[index]
    value["duration"] = clock.seconds(cursor)
    value["metadata"]["composition"] = {"schema_version": "composition/1", "transition_policy": "overlap"}
