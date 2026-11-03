"""Deterministic FFmpeg command construction and render quality gates.

The timeline is the source of truth. This module rejects timeline features it
cannot represent instead of silently dropping a track or an effect. A legacy
single-source path remains byte-for-byte compatible with existing commands;
contiguous multi-clip video, subtitles, overlays, transitions, and one-track
audio mixing use an explicit FFmpeg filter graph.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from timeline_engine import AssetType, TextStyle, Timeline, TrackType, validate_timeline


@dataclass(frozen=True)
class RenderProfile:
    name: str = "draft"
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int = 23
    preset: str = "medium"
    pixel_format: str = "yuv420p"
    fps_mode: str = "cfr"
    audio_bitrate: str = "192k"


class RenderQualityError(ValueError):
    """Raised when a canonical timeline cannot be rendered safely."""


def probe_render_output(
    output_path: str | Path,
    timeline: Timeline,
    *,
    ffprobe_bin: str = "ffprobe",
) -> dict:
    """Run a small post-render QC check and return normalized media facts.

    Checking the produced file before creating an Artifact catches truncated
    files, wrong dimensions, and commands that accidentally emitted audio-only
    or image output.  The tolerance is one video frame plus 100 ms because
    container timestamps are not always exact.
    """

    path = Path(output_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RenderQualityError("render output is missing or empty")
    completed = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RenderQualityError("ffprobe returned invalid JSON") from exc
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        raise RenderQualityError("render output has no video stream")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if (width, height) != (timeline.resolution.width, timeline.resolution.height):
        raise RenderQualityError(
            f"render output resolution is {width}x{height}, expected "
            f"{timeline.resolution.width}x{timeline.resolution.height}"
        )
    raw_duration = (payload.get("format") or {}).get("duration") or video.get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise RenderQualityError("render output has no measurable duration") from exc
    tolerance = max(0.1, 1.0 / timeline.fps + 0.1)
    if duration + tolerance < timeline.duration:
        raise RenderQualityError(
            f"render output duration is {duration:.3f}s, expected at least {timeline.duration:.3f}s"
        )
    return {
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "video_codec": video.get("codec_name"),
        "video_fps": video.get("r_frame_rate"),
        "size_bytes": path.stat().st_size,
    }


def _local_uri(uri: str) -> str:
    return uri[7:] if uri.startswith("file://") else uri


def _quality_gate(
    timeline: Timeline, profile: RenderProfile
) -> tuple[object, object, object | None, object | None]:
    """Return supported video/audio clips and their declared assets.

    The local draft renderer intentionally has a narrow contract.  The
    checks here prevent a successful FFmpeg invocation from producing a
    misleading output when the canonical timeline contains unsupported
    placement, tracks, or effects.
    """

    report = validate_timeline(timeline, allow_overlaps=False)
    if not report.valid:
        raise RenderQualityError(
            "; ".join(f"{issue.path}: {issue.message}" for issue in report.errors)
        )
    if timeline.resolution.width % 2 or timeline.resolution.height % 2:
        raise RenderQualityError("yuv420p output requires an even width and height")
    if timeline.duration > 2 * 60 * 60:
        raise RenderQualityError("draft renderer limits timelines to two hours")
    if timeline.frame_count > 10_000_000:
        raise RenderQualityError("timeline exceeds the 10 million frame render limit")

    non_empty = [track for track in timeline.tracks if track.clips]
    if any(clip.effects for track in non_empty if track.type == TrackType.AUDIO for clip in track.clips):
        raise RenderQualityError("audio effects, including fades, require an explicitly supported external renderer")
    video_tracks = [track for track in non_empty if track.type == TrackType.VIDEO]
    if len(video_tracks) != 1 or len(video_tracks[0].clips) != 1:
        raise RenderQualityError(
            "draft renderer supports exactly one video track with one clip"
        )
    if video_tracks[0].muted:
        raise RenderQualityError("draft renderer does not support a muted video track")
    audio_tracks = [track for track in non_empty if track.type == TrackType.AUDIO]
    unsupported = [track.type.value for track in non_empty if track.type in (TrackType.OVERLAY, TrackType.SUBTITLE)]
    if unsupported:
        raise RenderQualityError(
            "draft renderer does not yet support non-empty tracks: "
            + ", ".join(sorted(set(unsupported)))
        )
    if len(audio_tracks) > 1 or (audio_tracks and len(audio_tracks[0].clips) != 1):
        raise RenderQualityError("draft renderer supports at most one audio track with one clip")

    clip = video_tracks[0].clips[0]
    epsilon = 1e-6
    if abs(clip.range.start) > epsilon or abs(clip.range.end - timeline.duration) > epsilon:
        raise RenderQualityError(
            "the single video clip must cover the full timeline from 0 to duration"
        )
    if abs(clip.opacity - 1.0) > epsilon:
        raise RenderQualityError("draft renderer does not support clip opacity yet")
    if abs(clip.volume - 1.0) > epsilon:
        raise RenderQualityError("draft renderer does not support video clip volume yet")
    if clip.effects:
        raise RenderQualityError("draft renderer does not support clip effects yet")
    if not clip.asset_id:
        raise RenderQualityError("draft renderer requires a concrete asset_id")
    assets = {asset.id: asset for asset in timeline.assets}
    asset = assets.get(clip.asset_id)
    if asset is None:
        raise RenderQualityError(f"asset '{clip.asset_id}' is not declared")
    if asset.type != AssetType.VIDEO:
        raise RenderQualityError("draft renderer requires a video asset")
    source = _local_uri(asset.uri)
    if "://" in source:
        raise RenderQualityError(
            "draft renderer accepts local file paths only; prefetch remote assets first"
        )
    if clip.source_end is not None:
        source_span = clip.source_end - clip.source_start
        if source_span + epsilon < clip.range.duration:
            raise RenderQualityError("source range is shorter than the timeline clip")

    audio_clip = audio_asset = None
    if audio_tracks:
        audio_track = audio_tracks[0]
        if audio_track.muted:
            raise RenderQualityError("draft renderer does not support a muted audio track")
        audio_clip = audio_track.clips[0]
        if abs(audio_clip.range.start) > epsilon or abs(audio_clip.range.end - timeline.duration) > epsilon:
            raise RenderQualityError("the single audio clip must cover the full timeline from 0 to duration")
        if audio_clip.effects:
            raise RenderQualityError("draft renderer does not support audio clip effects yet")
        if not audio_clip.asset_id:
            raise RenderQualityError("draft renderer requires a concrete audio asset_id")
        audio_asset = assets.get(audio_clip.asset_id)
        if audio_asset is None:
            raise RenderQualityError(f"asset '{audio_clip.asset_id}' is not declared")
        if audio_asset.type != AssetType.AUDIO:
            raise RenderQualityError("draft renderer requires an audio asset for an audio track")
        audio_source = _local_uri(audio_asset.uri)
        if "://" in audio_source:
            raise RenderQualityError("draft renderer accepts local file paths only; prefetch remote audio assets first")
        if audio_clip.source_end is not None:
            audio_span = audio_clip.source_end - audio_clip.source_start
            if audio_span + epsilon < audio_clip.range.duration:
                raise RenderQualityError("audio source range is shorter than the timeline clip")
    return clip, asset, audio_clip, audio_asset


def build_ffmpeg_command(timeline: Timeline, output_path: str | Path, profile: RenderProfile | None = None) -> list[str]:
    """Build a reproducible command for a canonical timeline.

    Simple one-clip timelines use the stable legacy path. More complex
    timelines are lowered into the bounded multi-track filter graph and fail
    with :class:`RenderQualityError` when a feature is not explicitly supported.
    """
    profile = profile or RenderProfile()
    if not _requires_multitrack_renderer(timeline):
        clip, asset, audio_clip, audio_asset = _quality_gate(timeline, profile)
        return _build_single_source_command(timeline, output_path, profile, clip, asset, audio_clip, audio_asset)
    return _build_multitrack_command(timeline, output_path, profile)


def _build_single_source_command(
    timeline: Timeline,
    output_path: str | Path,
    profile: RenderProfile,
    clip: object,
    asset: object,
    audio_clip: object | None,
    audio_asset: object | None,
) -> list[str]:
    """Preserve the original deterministic single-source command contract."""
    # The concrete types are established by _quality_gate; keeping the helper
    # separate makes the multi-track branch unable to change legacy commands.
    source = _local_uri(asset.uri)
    scale = f"scale={timeline.resolution.width}:{timeline.resolution.height}:force_original_aspect_ratio=decrease,pad={timeline.resolution.width}:{timeline.resolution.height}:(ow-iw)/2:(oh-ih)/2"
    source_duration = clip.range.duration
    if clip.source_end is not None:
        source_duration = min(source_duration, clip.source_end - clip.source_start)
    video_filter = f"{scale},setsar=1"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{clip.source_start:.6f}",
        "-t",
        f"{source_duration:.6f}",
        "-i",
        source,
    ]
    if audio_clip is not None and audio_asset is not None:
        audio_source = _local_uri(audio_asset.uri)
        audio_duration = audio_clip.range.duration
        if audio_clip.source_end is not None:
            audio_duration = min(audio_duration, audio_clip.source_end - audio_clip.source_start)
        command.extend(
            [
                "-ss",
                f"{audio_clip.source_start:.6f}",
                "-t",
                f"{audio_duration:.6f}",
                "-i",
                audio_source,
                "-filter_complex",
                f"[0:v]{video_filter}[vout];[1:a]volume={audio_clip.volume:.6f},aresample=async=1:first_pts=0[aout]",
                "-map",
                "[vout]",
                "-map",
                "[aout]",
            ]
        )
    else:
        command.extend(["-vf", video_filter, "-map", "0:v:0", "-map", "0:a?"])
    command.extend(
        [
            "-r",
            f"{timeline.fps:g}",
            "-c:v",
            profile.video_codec,
            "-preset",
            profile.preset,
            "-crf",
            str(profile.crf),
            "-pix_fmt",
            profile.pixel_format,
            "-fps_mode",
            profile.fps_mode,
            "-c:a",
            profile.audio_codec,
            "-b:a",
            profile.audio_bitrate,
            "-movflags",
            "+faststart",
            "-t",
            f"{timeline.duration:.6f}",
            str(Path(output_path)),
        ]
    )
    return command


def _requires_multitrack_renderer(timeline: Timeline) -> bool:
    """Return true for timelines needing the filter-graph renderer."""
    non_empty = [track for track in timeline.tracks if track.clips]
    video_tracks = [track for track in non_empty if track.type == TrackType.VIDEO]
    audio_tracks = [track for track in non_empty if track.type == TrackType.AUDIO]
    if len(video_tracks) != 1 or len(video_tracks[0].clips) != 1:
        return True
    if len(audio_tracks) > 1 or (audio_tracks and len(audio_tracks[0].clips) != 1):
        return True
    if any(track.type in (TrackType.OVERLAY, TrackType.SUBTITLE) for track in non_empty):
        return True
    clip = video_tracks[0].clips[0]
    assets = {asset.id: asset for asset in timeline.assets}
    if clip.asset_id and assets.get(clip.asset_id) is not None and assets[clip.asset_id].type != AssetType.VIDEO:
        return True
    audio_clip = audio_tracks[0].clips[0] if audio_tracks else None
    return any(
        item is not None
        for item in (
            clip.transition,
            clip.text,
            clip.placement,
            clip.style,
            audio_clip.transition if audio_clip else None,
        )
    ) or bool(clip.effects or (audio_clip and audio_clip.effects))


def _escape_filter_value(value: str) -> str:
    """Escape user-controlled values embedded in an FFmpeg filter expression."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _build_multitrack_command(timeline: Timeline, output_path: str | Path, profile: RenderProfile) -> list[str]:
    """Build a bounded FFmpeg filter graph for multi-clip/overlay timelines.

    The renderer intentionally supports a small, explicit subset: one base
    video track with contiguous clips, optional xfade transitions, any number
    of image/video/text overlays, and externally mixed audio clips. Unsupported
    effects fail before FFmpeg starts so a successful command cannot silently
    drop user content.
    """
    report = validate_timeline(timeline, allow_overlaps=True)
    if not report.valid:
        raise RenderQualityError("; ".join(f"{i.path}: {i.message}" for i in report.errors))
    if timeline.resolution.width % 2 or timeline.resolution.height % 2:
        raise RenderQualityError("yuv420p output requires an even width and height")
    if timeline.duration > 2 * 60 * 60 or timeline.frame_count > 10_000_000:
        raise RenderQualityError("timeline exceeds the draft renderer limit")

    if any(clip.effects for track in timeline.tracks if track.type == TrackType.AUDIO for clip in track.clips):
        raise RenderQualityError("audio effects, including fades, require an explicitly supported external renderer")
    if (timeline.metadata.get("composition") or {}).get("transition_policy") == "overlap" and any(clip.transition for track in timeline.tracks for clip in track.clips):
        raise RenderQualityError("explicit overlap transitions require a supported external renderer; no frozen-frame padding is allowed")

    non_empty = [track for track in timeline.tracks if track.clips]
    video_tracks = [track for track in non_empty if track.type == TrackType.VIDEO]
    if len(video_tracks) != 1:
        raise RenderQualityError("multi-track renderer requires exactly one base video track")
    base_track = video_tracks[0]
    if base_track.muted:
        raise RenderQualityError("draft renderer does not support a muted base video track")
    clips = sorted(base_track.clips, key=lambda c: c.range.start)
    if not clips:
        raise RenderQualityError("base video track cannot be empty")
    epsilon = 1e-6
    if abs(clips[0].range.start) > epsilon or abs(clips[-1].range.end - timeline.duration) > epsilon:
        raise RenderQualityError("base video track must cover the full timeline from 0 to duration")
    for previous, current in zip(clips, clips[1:]):
        if abs(current.range.start - previous.range.end) > epsilon:
            raise RenderQualityError("base video clips must be contiguous without gaps")
    transitions = [clip.transition for clip in clips[1:]]
    if any(clip.transition for clip in clips[:1]):
        raise RenderQualityError("the first base video clip cannot declare a transition")
    if any(t is not None for t in transitions) and not all(t is not None for t in transitions):
        raise RenderQualityError("either every base clip boundary has a transition or none do")

    assets = {asset.id: asset for asset in timeline.assets}
    for clip in clips:
        if not clip.asset_id or clip.asset_id not in assets:
            raise RenderQualityError("base video clips require declared local video assets")
        asset = assets[clip.asset_id]
        if asset.type not in (AssetType.VIDEO, AssetType.IMAGE):
            raise RenderQualityError("base video track requires video or image assets")
        if clip.effects or abs(clip.opacity - 1.0) > epsilon or abs(clip.volume - 1.0) > epsilon:
            raise RenderQualityError("base video effects, opacity, and volume are not supported yet")
        if clip.source_end is not None and clip.source_end - clip.source_start + epsilon < clip.range.duration:
            raise RenderQualityError(f"source range for clip '{clip.id}' is shorter than its timeline range")
        _require_local_asset(asset.uri)

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    filters: list[str] = []
    input_index = 0
    base_labels: list[str] = []
    base_scale = (
        f"scale={timeline.resolution.width}:{timeline.resolution.height}:force_original_aspect_ratio=decrease,"
        f"pad={timeline.resolution.width}:{timeline.resolution.height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    for pos, clip in enumerate(clips):
        asset = assets[clip.asset_id]
        source_duration = clip.range.duration
        if clip.source_end is not None:
            source_duration = min(source_duration, clip.source_end - clip.source_start)
        if asset.type == AssetType.IMAGE:
            command.extend(["-loop", "1", "-t", f"{source_duration:.6f}", "-i", _local_uri(asset.uri)])
        else:
            command.extend(["-ss", f"{clip.source_start:.6f}", "-t", f"{source_duration:.6f}", "-i", _local_uri(asset.uri)])
        label = f"vbase{pos}"
        filters.append(f"[{input_index}:v]trim=duration={source_duration:.6f},setpts=PTS-STARTPTS,{base_scale}[{label}]")
        base_labels.append(label)
        input_index += 1

    current = base_labels[0]
    current_duration = clips[0].range.duration
    if transitions and transitions[0] is not None:
        for pos, transition in enumerate(transitions, start=1):
            assert transition is not None
            duration = transition.duration
            if duration >= min(current_duration, clips[pos].range.duration):
                raise RenderQualityError("transition duration must be shorter than both adjacent clips")
            next_label = f"vxf{pos}"
            offset = max(0.0, current_duration - duration)
            filters.append(
                f"[{current}][{base_labels[pos]}]xfade=transition={transition.type.value}:"
                f"duration={duration:.6f}:offset={offset:.6f}[{next_label}]"
            )
            current = next_label
            current_duration += clips[pos].range.duration - duration
        if current_duration < timeline.duration - epsilon:
            padded = "vbaseout"
            filters.append(f"[{current}]tpad=stop_mode=clone:stop_duration={timeline.duration - current_duration:.6f}[{padded}]")
            current = padded
        elif current_duration > timeline.duration + epsilon:
            raise RenderQualityError("transition output exceeds timeline duration")
    else:
        if len(base_labels) > 1:
            concat = "".join(f"[{label}]" for label in base_labels)
            filters.append(f"{concat}concat=n={len(base_labels)}:v=1:a=0[vbaseout]")
            current = "vbaseout"

    overlay_tracks = [track for track in non_empty if track.type in (TrackType.OVERLAY, TrackType.SUBTITLE)]
    for track in sorted(overlay_tracks, key=lambda item: int(item.metadata.get("z_index", 0))):
        if track.muted:
            raise RenderQualityError("draft renderer does not support muted overlay or subtitle tracks")
        for clip in sorted(track.clips, key=lambda item: (item.range.start, item.id)):
            if clip.effects:
                raise RenderQualityError("overlay and subtitle effects are not supported yet")
            next_label = f"vov{input_index}_{len(filters)}"
            placement = clip.placement
            x = placement.x if placement else 0
            y = placement.y if placement else 0
            enable = f"between(t,{clip.range.start:.6f},{clip.range.end:.6f})"
            if clip.text is not None:
                style = clip.style or TextStyle()
                args = []
                if style.font_file:
                    _require_local_asset(style.font_file)
                    args.append(f"fontfile='{_escape_filter_value(style.font_file)}'")
                args.extend([
                    f"text='{_escape_filter_value(clip.text)}'",
                    f"x={x:g}", f"y={y:g}", f"fontsize={style.font_size}",
                    f"fontcolor={style.font_color}", f"borderw={style.border_width}",
                    f"bordercolor={style.border_color}", f"box={1 if style.box else 0}",
                    f"boxcolor={style.box_color}", f"boxborderw={style.box_border}",
                    f"enable='{enable}'",
                ])
                filters.append(f"[{current}]drawtext={':'.join(args)}[{next_label}]")
            else:
                if not clip.asset_id or clip.asset_id not in assets:
                    raise RenderQualityError("overlay clips require declared assets")
                asset = assets[clip.asset_id]
                if asset.type not in (AssetType.IMAGE, AssetType.VIDEO):
                    raise RenderQualityError("overlay clips require image or video assets")
                _require_local_asset(asset.uri)
                source_duration = clip.range.duration
                if clip.source_end is not None:
                    source_duration = min(source_duration, clip.source_end - clip.source_start)
                if asset.type == AssetType.IMAGE:
                    command.extend(["-loop", "1", "-t", f"{timeline.duration:.6f}", "-i", _local_uri(asset.uri)])
                else:
                    command.extend(["-ss", f"{clip.source_start:.6f}", "-t", f"{source_duration:.6f}", "-i", _local_uri(asset.uri)])
                src = f"ovsrc{input_index}"
                overlay_filter = f"[{input_index}:v]setpts=PTS-STARTPTS"
                if placement and placement.width and placement.height:
                    overlay_filter += f",scale={placement.width}:{placement.height}"
                if clip.opacity < 1.0:
                    overlay_filter += f",format=rgba,colorchannelmixer=aa={clip.opacity:.6f}"
                filters.append(f"{overlay_filter}[{src}]")
                filters.append(f"[{current}][{src}]overlay=x={x:g}:y={y:g}:enable='{enable}'[{next_label}]")
                input_index += 1
            current = next_label

    audio_tracks = [track for track in non_empty if track.type == TrackType.AUDIO]
    # Preserve the existing one-track audio contract while allowing several
    # clips on that track to be mixed and delayed independently.
    if len(audio_tracks) > 1:
        raise RenderQualityError("draft renderer supports at most one audio track with one or more clips")
    audio_labels: list[str] = []
    for track in audio_tracks:
        if track.muted:
            raise RenderQualityError("draft renderer does not support a muted audio track")
        for clip in track.clips:
            if not clip.asset_id or clip.asset_id not in assets:
                raise RenderQualityError("audio clips require declared assets")
            asset = assets[clip.asset_id]
            if asset.type not in (AssetType.AUDIO, AssetType.VIDEO):
                raise RenderQualityError("audio track requires audio or video assets")
            _require_local_asset(asset.uri)
            source_duration = clip.range.duration
            if clip.source_end is not None:
                source_duration = min(source_duration, clip.source_end - clip.source_start)
            command.extend(["-ss", f"{clip.source_start:.6f}", "-t", f"{source_duration:.6f}", "-i", _local_uri(asset.uri)])
            label = f"a{input_index}"
            delay_ms = max(0, round(clip.range.start * 1000))
            filters.append(
                f"[{input_index}:a]atrim=duration={source_duration:.6f},asetpts=PTS-STARTPTS,"
                f"volume={clip.volume:.6f},adelay={delay_ms}:all=1[{label}]"
            )
            audio_labels.append(label)
            input_index += 1
    if audio_labels:
        if len(audio_labels) == 1:
            audio_out = audio_labels[0]
        else:
            audio_out = "aout"
            filters.append(f"{''.join(f'[{label}]' for label in audio_labels)}amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0,aresample=async=1:first_pts=0[{audio_out}]")
    else:
        audio_out = None
    filters.append(f"[{current}]fps={timeline.fps:g},format={profile.pixel_format}[vout]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[vout]"])
    if audio_out:
        command.extend(["-map", f"[{audio_out}]"])
    command.extend([
        "-r", f"{timeline.fps:g}", "-c:v", profile.video_codec, "-preset", profile.preset,
        "-crf", str(profile.crf), "-pix_fmt", profile.pixel_format, "-fps_mode", profile.fps_mode,
        "-c:a", profile.audio_codec, "-b:a", profile.audio_bitrate, "-movflags", "+faststart",
        "-t", f"{timeline.duration:.6f}", str(Path(output_path)),
    ])
    return command


def _require_local_asset(uri: str) -> None:
    source = _local_uri(uri)
    if "://" in source:
        raise RenderQualityError("draft renderer accepts local file paths only; prefetch remote assets first")
