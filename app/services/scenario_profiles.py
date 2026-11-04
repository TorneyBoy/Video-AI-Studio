"""Approved rule definitions; draft profiles do not imply media acceptance."""
from __future__ import annotations

from copy import deepcopy

from ..scenario_schemas import ScenarioBinding, ScenarioProfile


_PROFILES = (
    {"id": "product_showcase", "version": "1", "name": "商品展示", "description": "商品外观、卖点与真实使用证据优先。",
     "prompt_version": "product-shot-1", "source_policy": "real_first",
     "prompt_context": "以已确认的商品资料和参考图为事实依据，保留颜色、结构、包装和文字。仅补选定缺口，明确主体、动作、机位和时长；不能虚构卖点或以氛围画面代替商品证据。",
     "narrative_roles": ["establish_product", "feature_evidence", "usage", "close"],
     "selection_weights": {"quality": 0.45, "semantic_match": 0.55}},
    {"id": "spoken_explainer", "version": "1", "name": "口播讲解", "description": "先保留讲解语义，再组织字幕和示意画面。",
     "prompt_version": "spoken-shot-1", "source_policy": "speech_first", "preserve_speech_meaning": True,
     "allowed_operations": ["video_generation", "person_replace"],
     "prompt_context": "以确认的讲话和术语为依据，保留否定、数字、指代及上下文。生成画面应与讲解对应，不把示意画面当作真实事件；保留已批准人物与镜头，局部重做须明确。",
     "narrative_roles": ["question", "explanation", "example", "conclusion"],
     "selection_weights": {"quality": 0.25, "semantic_match": 0.75}},
)


def get_scenario_profile(id: str, version: str | None = None) -> ScenarioProfile:
    candidates = [row for row in _PROFILES if row["id"] == id and (version is None or row["version"] == version)]
    if not candidates:
        raise ValueError("unknown scenario profile or version")
    profile = ScenarioProfile.model_validate(deepcopy(candidates[-1]))
    if profile.validation_status == "suspended":
        raise ValueError("scenario profile is suspended")
    return profile


resolve_scenario_profile = get_scenario_profile


def scenario_binding(profile: ScenarioProfile) -> dict:
    return {"id": profile.id, "version": profile.version,
            "content_hash": profile.content_hash, "prompt_version": profile.prompt_version}


def list_scenario_profiles() -> list[dict]:
    return [get_scenario_profile(row["id"], row["version"]).snapshot() for row in _PROFILES]


def resolve_scenario_binding(value: dict | ScenarioBinding) -> ScenarioProfile:
    binding = value if isinstance(value, ScenarioBinding) else ScenarioBinding.model_validate(value)
    profile = get_scenario_profile(binding.id, binding.version)
    if binding.content_hash is not None and binding.content_hash != profile.content_hash:
        raise ValueError("scenario definition differs from its frozen content hash")
    if binding.prompt_version is not None and binding.prompt_version != profile.prompt_version:
        raise ValueError("scenario prompt differs from its frozen version")
    return profile


def template_scenario(template) -> ScenarioProfile | None:
    metadata = template.metadata if isinstance(template.metadata, dict) else {}
    if "scenario" not in metadata:
        return None
    return resolve_scenario_binding(metadata["scenario"])


def freeze_template_scenario(template):
    profile = template_scenario(template)
    if profile is None:
        return template
    return template.model_copy(update={"metadata": {**template.metadata, "scenario": scenario_binding(profile)}}, deep=True)
