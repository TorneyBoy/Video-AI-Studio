"""Deterministic renderer hand-off manifest."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from .schema import TemplateBinding, TemplateDefinition, Timeline, StrictModel
from .validation import validate_all


class ManifestAsset(StrictModel):
    id: str
    type: str
    uri: str
    checksum: Optional[str] = None


class RenderManifest(StrictModel):
    manifest_version: Literal["1.0"] = "1.0"
    manifest_id: str = Field(min_length=1)
    timeline_id: str
    timeline_schema_version: str
    template_id: Optional[str] = None
    template_version: Optional[str] = None
    fps: float
    duration: float
    frame_count: int
    resolution: Dict[str, int]
    assets: List[ManifestAsset] = Field(default_factory=list)
    output: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)
    source: Dict[str, Any] = Field(default_factory=dict)
    content_hash: str = Field(min_length=64, max_length=64)

    def canonical_dict(self, *, include_manifest_id: bool = False) -> dict:
        data = self.model_dump(mode="json")
        if not include_manifest_id:
            data.pop("manifest_id", None)
        return data

    def canonical_json(self, *, include_manifest_id: bool = False) -> str:
        return json.dumps(self.canonical_dict(include_manifest_id=include_manifest_id), sort_keys=True, separators=(",", ":"))

    def verify_hash(self) -> bool:
        payload = self.canonical_dict(include_manifest_id=False)
        supplied = payload.pop("content_hash", None)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return supplied == digest == self.content_hash


def create_manifest(
    timeline: Timeline,
    *,
    template: Optional[TemplateDefinition] = None,
    binding: Optional[TemplateBinding] = None,
    output: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
    validate: bool = True,
) -> RenderManifest:
    """Build a deterministic manifest from canonical objects.

    Validation is enabled by default so malformed timelines cannot silently
    reach a renderer. Set ``validate=False`` only when importing legacy data.
    """
    if validate:
        report = validate_all(timeline, template, binding)
        report.raise_for_errors()
    assets = [ManifestAsset(id=a.id, type=a.type.value, uri=a.uri, checksum=a.checksum) for a in timeline.assets]
    raw = dict(
        manifest_version="1.0",
        manifest_id="pending",
        timeline_id=timeline.id,
        timeline_schema_version=timeline.schema_version,
        template_id=template.id if template else None,
        template_version=template.version if template else None,
        fps=timeline.fps,
        duration=timeline.duration,
        frame_count=timeline.frame_count,
        resolution=timeline.resolution.model_dump(mode="json"),
        assets=assets,
        output=output or {"format": "mp4"},
        options=options or {},
        source={"timeline": timeline.model_dump(mode="json"), "binding": binding.model_dump(mode="json") if binding else None},
        content_hash="0" * 64,
    )
    # Hash excludes both the generated id and hash itself. Normalize through
    # Pydantic first so nested models and enums have exactly one JSON form.
    normalized = RenderManifest(**raw).canonical_dict(include_manifest_id=False)
    normalized.pop("content_hash")
    content_hash = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    raw["content_hash"] = content_hash
    raw["manifest_id"] = f"m_{content_hash[:16]}"
    return RenderManifest(**raw)

