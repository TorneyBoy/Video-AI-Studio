"""Compile explicit, independently enabled ICE features; never drop edits.

This compiler does not call ICE or assert that a tenant has a subscription.
Cloud acceptance controls which of these features can be opened to users.
"""

from __future__ import annotations

import math
from urllib.parse import quote, unquote, urlparse

from timeline_engine import AssetType, Timeline, TrackType, validate_timeline


COMPILER_VERSION = "ice-timeline/4"
# These are compiler capabilities, not evidence of cloud acceptance. Deployments
# must explicitly enable new effects after their own media/price acceptance.
OPTIONAL_FEATURES = frozenset({
    "audio_tracks", "multi_audio_tracks", "audio_fades", "overlays", "image_clips",
    "frame_fit", "fit_contain", "fit_cover", "fit_stretch", "track_mute",
    "video_placement", "transition_overlap", "transition_fade",
    "transition_wipeleft", "transition_wiperight",
})
TRANSITIONS = {"fade": "fade", "wipeleft": "wipeleft", "wiperight": "wiperight"}


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
    if not math.isfinite(value) or value < 0:
        raise ICECompileError("Negative placement is outside the initial ICE feature subset")
    return value if value >= 2 and float(value).is_integer() else value / dimension


def _number(value, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ICECompileError(f"{name} must be a finite number")
    return float(value)


def compile_ice_timeline(timeline: Timeline, *, bucket: str, region: str,
                         enabled_features: list[str] | tuple[str, ...] = ()) -> tuple[dict, dict]:
    """Lower canonical edits to documented ICE fields without changing timing.

    Reference: IMS Timeline/Effect configuration. Canonical transitions are
    incoming; ICE Transition belongs to the preceding clip and shortens the
    sequence. Never substitute DLTransition (which adds frozen frames).
    """
    enabled = set(enabled_features)
    if enabled - OPTIONAL_FEATURES:
        raise ICECompileError("Unknown ICE feature configuration")

    def require(*features):
        missing = set(features) - enabled
        if missing:
            raise ICECompileError("Features require cloud acceptance before being enabled: " + ", ".join(sorted(missing)))

    report = validate_timeline(timeline)
    if not report.valid:
        raise ICECompileError("Invalid canonical timeline: " + "; ".join(issue.message for issue in report.errors))
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
    primary = base_tracks[0]
    ordered = sorted(primary.clips, key=lambda item: item.range.start)
    composition = timeline.metadata.get("composition") or {}
    overlap = isinstance(composition, dict) and composition.get("transition_policy") == "overlap"
    cursor = 0.0
    for clip in ordered:
        incoming = clip.transition.duration if clip.transition else 0
        if incoming:
            require("transition_overlap", "transition_" + clip.transition.type.value)
            if not overlap or clip is ordered[0] or clip.transition.type.value not in TRANSITIONS:
                raise ICECompileError("Only explicit overlap transitions with a documented ICE equivalent are supported")
            if round(incoming, 4) == 0:
                raise ICECompileError("Transition is below ICE timestamp precision")
        if abs(clip.range.start - (cursor - incoming)) > 0.0001:
            raise ICECompileError("Primary video clips must cover the timeline without gaps or overlap")
        cursor = clip.range.end
    if abs(cursor - timeline.duration) > 0.0001:
        raise ICECompileError("Primary video track must cover the full output duration")
    audio_tracks = [track for track in timeline.tracks if track.type == TrackType.AUDIO and track.clips]
    if audio_tracks:
        require("audio_tracks")
    if len(audio_tracks) > 1:
        require("multi_audio_tracks")

    # ICE subtitle tracks sit above video tracks. Reject a canonical text layer
    # below a media overlay rather than silently changing the composition.
    visual_tracks = [track for track in timeline.tracks if track.clips and track.type in {TrackType.OVERLAY, TrackType.SUBTITLE}]
    def layer_key(track):
        value = track.metadata.get("z_index", timeline.tracks.index(track))
        if isinstance(value, bool) or not isinstance(value, int):
            raise ICECompileError("Layer order must be an integer")
        return value, timeline.tracks.index(track)
    visual_tracks.sort(key=layer_key)
    saw_text = False
    for track in visual_tracks:
        if track.muted:
            continue
        has_text = any(clip.text is not None for clip in track.clips)
        has_media = any(clip.asset_id for clip in track.clips)
        if has_media and (saw_text or has_text):
            raise ICECompileError("ICE cannot place a media overlay above a text layer in this profile")
        saw_text = saw_text or has_text

    tracks = [primary, *audio_tracks, *visual_tracks]
    for track in tracks:
        rows = []
        if track.type == TrackType.OVERLAY:
            require("overlays")
        if track.muted and track.type != TrackType.VIDEO:
            require("track_mute")
            if track.type != TrackType.AUDIO:
                continue
        for clip in sorted(track.clips, key=lambda item: (item.range.start, item.id)):
            row = {"TimelineIn": round(clip.range.start, 4), "TimelineOut": round(clip.range.end, 4)}
            if row["TimelineIn"] >= row["TimelineOut"]:
                raise ICECompileError("Clip duration is below ICE timestamp precision")
            fit, fades = "contain", []
            seen = set()
            for effect in clip.effects:
                kind = effect.get("type")
                if not isinstance(kind, str):
                    raise ICECompileError("Clip effect type must be a string")
                if kind in seen:
                    raise ICECompileError("Duplicate clip effects cannot be represented")
                seen.add(kind)
                if kind == "frame_fit" and track.type in {TrackType.VIDEO, TrackType.OVERLAY} and clip.text is None:
                    if set(effect) - {"type", "schema_version", "mode"} or effect.get("schema_version") != "frame-fit/1":
                        raise ICECompileError("Unsupported frame-fit contract")
                    fit = effect.get("mode")
                    if fit not in {"contain", "cover", "stretch"}:
                        raise ICECompileError("Unsupported frame-fit mode")
                    require("frame_fit", "fit_" + fit)
                elif kind == "audio_fade" and track.type == TrackType.AUDIO:
                    require("audio_fades")
                    if set(effect) - {"type", "schema_version", "in_seconds", "out_seconds"} or effect.get("schema_version") != "audio-envelope/1":
                        raise ICECompileError("Unsupported audio-envelope contract")
                    fade_in = _number(effect.get("in_seconds", 0), name="Audio fade in")
                    fade_out = _number(effect.get("out_seconds", 0), name="Audio fade out")
                    if min(fade_in, fade_out) < 0 or fade_in + fade_out > clip.range.duration + 1e-8:
                        raise ICECompileError("Audio fades exceed their clip duration")
                    for subtype, duration in (("In", fade_in), ("Out", fade_out)):
                        if duration:
                            if round(duration, 4) == 0:
                                raise ICECompileError("Audio fade is below ICE timestamp precision")
                            fades.append({"Type": "AFade", "SubType": subtype, "Duration": round(duration, 4), "Curve": "tri"})
                else:
                    raise ICECompileError("Unsupported clip effect; no effect may be silently dropped")
            if track.type == TrackType.SUBTITLE or clip.text is not None:
                if clip.text is None or clip.effects or clip.transition or clip.opacity != 1:
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
                if clip.placement and (clip.placement.width is not None or clip.placement.height is not None):
                    raise ICECompileError("Text bounding boxes require a separately defined wrapping contract")
            else:
                asset = assets.get(clip.asset_id)
                if asset is None or clip.slot or clip.text is not None:
                    raise ICECompileError("Every media clip must bind a concrete declared asset")
                if clip.style is not None:
                    raise ICECompileError("Text style cannot be applied to a media clip")
                if asset.type == AssetType.IMAGE:
                    require("image_clips")
                row["MediaURL"] = oss_media_url(asset.uri, bucket=bucket, region=region)
                if track.type in {TrackType.VIDEO, TrackType.OVERLAY}:
                    if asset.type not in {AssetType.VIDEO, AssetType.IMAGE}:
                        raise ICECompileError("Primary video requires video/image assets with canvas placement")
                    if clip.placement:
                        if track.type == TrackType.VIDEO:
                            require("video_placement")
                        placement = clip.placement
                        if (placement.width is None) != (placement.height is None):
                            raise ICECompileError("Media placement requires both width and height")
                        target_width, target_height = placement.width or width, placement.height or height
                        if placement.x + target_width > width or placement.y + target_height > height:
                            raise ICECompileError("Media placement exceeds the output canvas")
                        row.update(X=_pixel(placement.x, width), Y=_pixel(placement.y, height))
                    else:
                        target_width, target_height = width, height
                    row.update({"Width": _pixel(target_width, width), "Height": _pixel(target_height, height),
                                "AdaptMode": {"contain": "Contain", "cover": "Cover", "stretch": "Fill"}[fit], "Opacity": clip.opacity})
                    if track.type == TrackType.OVERLAY and asset.type == AssetType.VIDEO and clip.volume != 0:
                        raise ICECompileError("Overlay audio must be explicitly added as an audio track")
                elif track.type == TrackType.AUDIO:
                    if asset.type not in {AssetType.AUDIO, AssetType.VIDEO} or clip.opacity != 1 or clip.placement:
                        raise ICECompileError("Audio clip contains unsupported parameters")
                if asset.type == AssetType.IMAGE:
                    if clip.source_start or clip.source_end is not None or fades or clip.volume not in {0, 1}:
                        raise ICECompileError("Image clip source ranges/audio volume are not meaningful")
                    row.update(Type="Image", Duration=round(clip.range.duration, 4))
                else:
                    end = clip.source_end if clip.source_end is not None else clip.source_start + clip.range.duration
                    if end - clip.source_start + 0.0001 < clip.range.duration:
                        raise ICECompileError("Source is shorter than its timeline interval; explicit freeze or speed editing is required")
                    if clip.volume > 10:
                        raise ICECompileError("ICE volume gain must not exceed 10")
                    row.update({"In": round(clip.source_start, 4), "Out": round(clip.source_start + clip.range.duration, 4),
                                "Effects": [{"Type": "Volume", "Gain": 0 if track.muted else clip.volume}, *fades]})
            rows.append(row)
        # ICE computes overlap by attaching Transition to the outgoing clip.
        # Omit primary TimelineIn/Out only for transition sequences: canonical
        # validation above proves that the provider's sequential layout has the
        # same total duration, and avoids applying the overlap twice.
        if track is primary and any(clip.transition for clip in ordered):
            for index, clip in enumerate(ordered):
                rows[index].pop("TimelineIn")
                rows[index].pop("TimelineOut")
                if clip.transition:
                    rows[index - 1].setdefault("Effects", []).append({"Type": "Transition",
                        "SubType": TRANSITIONS[clip.transition.type.value], "Duration": round(clip.transition.duration, 4)})
        if rows:
            if track.type == TrackType.VIDEO:
                output["VideoTracks"].append({"VideoTrackClips": rows})
            elif track.type == TrackType.AUDIO:
                output["AudioTracks"].append({"AudioTrackClips": rows})
            elif track.type == TrackType.SUBTITLE:
                output["SubtitleTracks"].append({"SubtitleTrackClips": rows})
            elif track.type == TrackType.OVERLAY:
                if rows[0].get("Type") == "Text":
                    output["SubtitleTracks"].append({"SubtitleTrackClips": rows})
                else:
                    output["VideoTracks"].append({"VideoTrackClips": rows})
    return {key: value for key, value in output.items() if value}, {
        # Pin the documented quality setting explicitly for reproducible
        # output. Real QA established that the earlier apparent missing text
        # came from concurrent PNG extraction, not from the cloud video.
        "Width": width, "Height": height,
        "Video": {"Fps": timeline.fps, "Codec": "H.264", "Crf": 23}, "MaxDuration": timeline.duration,
    }
