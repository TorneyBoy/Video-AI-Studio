"""Network-free feature admission before job allocation and external dispatch."""
from .capabilities import capability_catalog, require_available_capability
from .editing_errors import EditingError
from .provider_registry import ProviderRegistryError
from .providers import ProviderExecutionError

LABELS = {"audio_tracks": "独立音轨", "multi_audio_tracks": "多音轨混合", "audio_fades": "音频淡入淡出",
          "overlays": "画面叠加", "image_clips": "图片片段", "frame_fit": "画面适配", "track_mute": "附加轨隐藏/静音",
          "opacity": "透明度", "transition_overlap": "重叠转场", "unicode_subtitles": "非ASCII字幕字体",
          "custom_fonts": "自定义字体", "styled_subtitles": "字幕边框/背景"}
LABELS.update({"fit_contain": "完整显示适配", "fit_cover": "居中填充裁切", "fit_stretch": "拉伸适配",
               "video_placement": "主画面定位", "legacy_transition": "旧转场时间规则", "transition_wipeleft": "左擦除转场",
               "transition_wiperight": "右擦除转场", "transition_slideleft": "左滑动转场", "transition_slideright": "右滑动转场"})


def advanced_features(timeline: dict) -> list[str]:
    features = set(); assets = {row["id"]: row for row in timeline.get("assets", [])}; audio_count = 0
    for track in timeline.get("tracks", []):
        if not track.get("clips"):
            continue
        kind = track["type"]
        if kind == "audio":
            features.add("audio_tracks"); audio_count += 1
        elif kind == "overlay":
            features.add("overlays")
        if kind != "video" and track.get("muted"):
            features.add("track_mute")
        for clip in track["clips"]:
            if assets.get(clip.get("asset_id"), {}).get("type") == "image":
                features.add("image_clips")
            if clip.get("transition"):
                composition = timeline.get("metadata", {}).get("composition")
                policy = composition.get("transition_policy") if isinstance(composition, dict) else None
                features.update({"transition_overlap" if policy == "overlap" else "legacy_transition", "transition_" + clip["transition"]["type"]})
            if kind == "subtitle":
                style = clip.get("style") or {}
                if any(ord(character) > 127 for character in clip.get("text") or ""):
                    features.add("unicode_subtitles")
                if style.get("font_file"):
                    features.add("custom_fonts")
                if style.get("box") or style.get("border_width"):
                    features.add("styled_subtitles")
            if clip.get("opacity", 1) != 1:
                features.add("opacity")
            if kind == "video" and clip.get("placement"):
                features.add("video_placement")
            for effect in clip.get("effects", []):
                effect_type = str(effect.get("type") or "unknown")
                features.add({"audio_fade": "audio_fades", "frame_fit": "frame_fit"}.get(effect_type, "effect_" + effect_type))
                if effect_type == "frame_fit":
                    features.add("fit_" + str(effect.get("mode", "contain")))
    if audio_count > 1:
        features.add("multi_audio_tracks")
    return sorted(features)


def render_support(timeline: dict, *, provider: str | None = None, model_id: str | None = None, model_version: str | None = None) -> dict:
    required = advanced_features(timeline)
    base = {"required_features": required, "selected": None, "missing_features": []}
    if not required:
        return {**base, "supported": True, "reason": "未使用额外媒体效果；渲染仍须通过服务、预算和质量检查。"}
    try:
        entries = capability_catalog()["capabilities"]
    except (ProviderRegistryError, ValueError, OSError):
        entries = []
    choices = [item for item in entries if item.get("available") and item["capability"] == "video_editing"
               and (not provider or item["provider"] == provider) and (not model_id or item["model_id"] == model_id)
               and (not model_version or item["model_version"] == model_version)]
    matches = [item for item in choices if set(required) <= set(item.get("limits", {}).get("features", []))]
    if matches:
        selected = sorted(matches, key=lambda item: item["id"])[0]
        return {**base, "supported": True, "selected": selected, "reason": "已找到声明覆盖本版本全部效果的已验收服务；仍须通过额度与执行检查。"}
    best = min(choices, key=lambda item: len(set(required) - set(item.get("limits", {}).get("features", [])))) if choices else None
    missing = sorted(set(required) - set((best or {}).get("limits", {}).get("features", [])))
    names = [LABELS.get(name, {"transition_fade": "淡化转场", "transition_dissolve": "溶解转场"}.get(name, name)) for name in missing]
    return {**base, "supported": False, "missing_features": missing,
            "reason": "当前服务尚未开放：" + "、".join(names) + "。可保存编辑，生成前需完成对应能力接入和验收。"}


def require_render_support(timeline: dict, *, require_price: bool = False, **preferred) -> dict | None:
    support = render_support(timeline, **preferred)
    if not support["supported"]:
        raise EditingError("render_features_unavailable", support["reason"], 422,
                           {"required_features": support["required_features"], "missing_features": support["missing_features"]})
    if support["selected"] is None and require_price:
        try:
            return require_available_capability("video_editing", preferred.get("provider"), preferred.get("model_id"), preferred.get("model_version"))
        except ProviderExecutionError as exc:
            raise EditingError("quote_price_unavailable", "当前服务未提供已验收且带版本的报价，不能按零费用提交", 422) from exc
    return support["selected"]
