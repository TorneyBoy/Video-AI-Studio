"""One explainable eligibility contract for template slots and storyboards.

No embeddings, invented similarity percentages or text-as-code execution.
Unobserved semantic dimensions remain unknown and need human confirmation.
"""
from dataclasses import asdict, dataclass, field
import unicodedata


def term(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


@dataclass(frozen=True)
class ShotRequirement:
    id: str
    media_types: tuple[str, ...] = ("video", "image")
    duration_seconds: float | None = None
    min_source_seconds: float | None = None
    max_source_seconds: float | None = None
    orientation: str | None = None
    required_metadata: tuple[str, ...] = ()
    required_tags: tuple[str, ...] = ()
    excluded_tags: tuple[str, ...] = ()
    visual: dict = field(default_factory=dict)
    constraint_notes: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()

    def snapshot(self):
        return {"schema_version": "shot-requirement/1", **asdict(self)}


def template_requirement(slot, policy=None):
    policy = policy if isinstance(policy, dict) else {}
    constraints = slot.constraints
    # Keep existing template type behavior; allowed_asset_types is authoritative.
    types = tuple(row.value for row in constraints.allowed_asset_types) or ("video", "image", "audio")
    return ShotRequirement(id=slot.name, media_types=types, min_source_seconds=constraints.min_duration,
        max_source_seconds=constraints.max_duration, orientation=policy.get("orientation"),
        required_metadata=tuple(constraints.required_metadata),
        required_tags=tuple(policy.get("required_tags") or ()), excluded_tags=tuple(policy.get("excluded_tags") or ()))


def storyboard_requirement(shot, facts, override):
    return ShotRequirement(id=shot["id"], duration_seconds=shot["duration_ms"] / 1000,
        media_types=tuple(override.media_types), orientation=None if override.orientation == "any" else override.orientation,
        required_tags=tuple(override.required_tags), excluded_tags=tuple(override.excluded_tags),
        visual={key: value for key, value in shot["visual"].items() if str(value).strip()},
        constraint_notes=tuple(shot["constraints"]), facts=tuple(row["text"] for row in facts if row["confirmed"]))


def assess(requirement, *, kind, duration, orientation, metadata=None, visual=None, tags=(), measured=True, image_hold=True):
    """Return hard exclusions, useful reasons and unresolved review dimensions."""
    metadata, visual = metadata or {}, visual or {}
    rejected, reasons, review = [], [], []
    if kind not in requirement.media_types:
        rejected.append("media_type_mismatch")
    else:
        reasons.append("media_type_supported")
    if kind != "image" or not image_hold:
        minimum = max(value for value in (requirement.duration_seconds, requirement.min_source_seconds, 0) if value is not None)
        if duration is None:
            rejected.append("duration_unavailable")
        elif duration + 1e-9 < minimum:
            rejected.append("source_too_short")
        elif requirement.max_source_seconds is not None and duration > requirement.max_source_seconds + 1e-9:
            rejected.append("source_too_long")
        else:
            reasons.append("duration_sufficient")
        if not measured:
            review.append("duration_declared_only")
    else:
        reasons.append("still_image_hold")
    if requirement.orientation:
        if orientation is None:
            review.append("orientation_unknown")
        elif orientation != requirement.orientation:
            rejected.append("orientation_mismatch")
        else:
            reasons.append("orientation_matches")
    if any(metadata.get(key) is None for key in requirement.required_metadata):
        rejected.append("required_metadata_missing")
    known_tags = {term(row) for row in tags}
    if known_tags & {term(row) for row in requirement.excluded_tags}:
        rejected.append("excluded_tag_present")
    for tag in requirement.required_tags:
        if term(tag) not in known_tags:
            review.append("required_tag_unverified:" + tag)
        else:
            reasons.append("user_tag_matches:" + tag)
    for key, wanted in requirement.visual.items():
        observed = visual.get(key)
        if not observed or not term(observed):
            review.append("visual_unverified:" + key)
        elif term(observed) != term(wanted):
            # Different free-text descriptions are not proof of contradiction.
            # Without a verified semantic model, ask the human to compare them.
            review.append("visual_description_differs:" + key)
        else:
            reasons.append("user_semantic_matches:" + key)
    if requirement.constraint_notes:
        review.append("constraints_require_visual_review")
    if requirement.facts:
        review.append("product_facts_require_visual_review")
    return {"rejected": rejected, "reasons": reasons, "review": review,
            "verdict": "excluded" if rejected else "needs_review" if review else "supported"}
