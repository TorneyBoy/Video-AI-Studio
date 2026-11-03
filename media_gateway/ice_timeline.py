"""Compile the explicit initial ICE feature subset; never silently drop edits.

This compiler does not call ICE or assert that a tenant has a subscription.
Cloud acceptance controls which of these features can be opened to users.
"""

from __future__ import annotations

from urllib.parse import quote, unquote, urlparse

from timeline_engine import AssetType, Timeline, TrackType


class ICECompileError(ValueError):
    pass


def oss_media_url(uri: str, *, bucket: str, region: str) -> str:
    parsed = urlparse(uri)
    host = f"{bucket}.oss-{region}.aliyuncs.com"
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ICECompileError("ICE inputs must be stable OSS references")
    if not ((parsed.scheme == "oss" and parsed.netloc == bucket)
            or (parsed.scheme == "https" and parsed.netloc == host)):
        raise ICECompileError("ICE inputs must use the configured same-region OSS bucket")
    key = unquote(parsed.path.lstrip("/"))
    if not key or any(part in {".", ".."} for part in key.split("/")):
        raise ICECompileError("ICE input object key is invalid")
    return f"https://{host}/{quote(key, safe='/')}"


def _pixel(value: float, dimension: int) -> float:
    # ICE treats [0, .9999] as fractions and >=2 integers as pixels.
    # Represent 1px/subpixel positions explicitly as canvas fractions.
    if value < 0:
        raise ICECompileError("Negative placement is outside the initial ICE feature subset")
    return value if value >= 2 and float(value).is_integer() else value / dimension


def compile_ice_timeline(timeline: Timeline, *, bucket: str, region: str) -> tuple[dict, dict]:
    width, height = timeline.resolution.width, timeline.resolution.height
    if min(width, height) < 128 or max(width, height) > 4096 or min(width, height) > 2160:
        raise ICECompileError("Output resolution exceeds ICE limits")
    if width % 2 or height % 2 or timeline.fps > 60:
        raise ICECompileError("Initial ICE profile requires even dimensions and at most 60fps")
    assets = {asset.id: asset for asset in timeline.assets}
    output = {"VideoTracks": [], "AudioTracks": [], "SubtitleTracks": []}
    base_tracks = [track for track in timeline.tracks if track.type == TrackType.VIDEO and track.clips]
    if len(base_tracks) != 1:
        raise ICECompileError("Initial ICE profile requires one primary video track")
    cursor = 0.0
    for clip in sorted(base_tracks[0].clips, key=lambda item: item.range.start):
        if abs(clip.range.start - cursor) > 0.0001:
            raise ICECompileError("Primary video clips must cover the timeline without gaps or overlap")
        cursor = clip.range.end
    if abs(cursor - timeline.duration) > 0.0001:
        raise ICECompileError("Primary video track must cover the full output duration")
    for track in timeline.tracks:
        rows = []
        if track.type == TrackType.AUDIO and track.clips:
            raise ICECompileError("Separate audio tracks require cloud acceptance before being enabled")
        if track.type == TrackType.OVERLAY and track.clips:
            raise ICECompileError("Overlay order requires cloud acceptance before being enabled")
        for clip in track.clips:
            if clip.transition or clip.effects:
                raise ICECompileError("Transitions and custom effects are not enabled in the initial ICE profile")
            row = {"TimelineIn": round(clip.range.start, 4), "TimelineOut": round(clip.range.end, 4)}
            if track.type == TrackType.SUBTITLE:
                if track.muted or clip.text is None or clip.opacity != 1:
                    raise ICECompileError("Subtitle clip cannot be represented by the initial ICE profile")
                style = clip.style
                if style and (style.font_file or style.box or style.border_width):
                    raise ICECompileError("Custom fonts, subtitle backgrounds and outlines require separate acceptance")
                color = style.font_color if style else "white"
                colors = {"white": "#FFFFFF", "black": "#000000", "red": "#FF0000"}
                if color not in colors and not (len(color) == 7 and color.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in color[1:])):
                    raise ICECompileError("Unsupported subtitle colour")
                row.update({"Type": "Text", "Content": clip.text, "FontSize": style.font_size if style else 48,
                            "FontColor": colors.get(color, color), "Alignment": "TopLeft",
                            "X": _pixel(clip.placement.x, width) if clip.placement else 0,
                            "Y": _pixel(clip.placement.y, height) if clip.placement else 0})
            else:
                asset = assets.get(clip.asset_id)
                if asset is None or clip.slot or clip.text is not None:
                    raise ICECompileError("Every media clip must bind a concrete declared asset")
                if asset.type == AssetType.IMAGE:
                    raise ICECompileError("Image clips require cloud acceptance before being enabled")
                row["MediaURL"] = oss_media_url(asset.uri, bucket=bucket, region=region)
                if track.type == TrackType.VIDEO:
                    if asset.type not in {AssetType.VIDEO, AssetType.IMAGE} or clip.placement:
                        raise ICECompileError("Primary video requires video/image assets with canvas placement")
                    row.update({"Width": width, "Height": height, "AdaptMode": "Contain", "Opacity": clip.opacity})
                elif track.type == TrackType.AUDIO:
                    if asset.type not in {AssetType.AUDIO, AssetType.VIDEO} or clip.opacity != 1 or clip.placement:
                        raise ICECompileError("Audio clip contains unsupported parameters")
                if asset.type == AssetType.IMAGE:
                    if clip.source_start or clip.source_end is not None or clip.volume != 1:
                        raise ICECompileError("Image clip source ranges/audio volume are not meaningful")
                    row["Duration"] = round(clip.range.duration, 4)
                else:
                    end = clip.source_end if clip.source_end is not None else clip.source_start + clip.range.duration
                    if end - clip.source_start + 0.0001 < clip.range.duration:
                        raise ICECompileError("Source is shorter than its timeline interval; explicit freeze or speed editing is required")
                    row.update({"In": round(clip.source_start, 4), "Out": round(clip.source_start + clip.range.duration, 4),
                                "Effects": [{"Type": "Volume", "Gain": 0 if track.muted else clip.volume}]})
            rows.append(row)
        if rows:
            if track.type == TrackType.VIDEO:
                output["VideoTracks"].append({"VideoTrackClips": rows})
            elif track.type == TrackType.AUDIO:
                output["AudioTracks"].append({"AudioTrackClips": rows})
            elif track.type == TrackType.SUBTITLE:
                output["SubtitleTracks"].append({"SubtitleTrackClips": rows})
    return {key: value for key, value in output.items() if value}, {
        # Pin the documented quality setting explicitly for reproducible
        # output. Real QA established that the earlier apparent missing text
        # came from concurrent PNG extraction, not from the cloud video.
        "Width": width, "Height": height,
        "Video": {"Fps": timeline.fps, "Codec": "H.264", "Crf": 23}, "MaxDuration": timeline.duration,
    }
