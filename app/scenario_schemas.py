"""Versioned scene rules and review evidence, independent of provider SDKs."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Feature = Literal["quality", "motion", "face_sharpness", "emotion", "semantic_match"]
Dimension = Literal["facts", "technical", "narrative", "visual", "audio"]
Operation = Literal["video_generation", "person_replace", "face_replace"]


class ScenarioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ScenarioBinding(ScenarioModel):
    id: Identifier
    version: Identifier
    content_hash: Digest | None = None
    prompt_version: Identifier | None = None


class ScenarioProfile(ScenarioModel):
    schema_version: Literal["scenario-profile/1"] = "scenario-profile/1"
    id: Identifier
    version: Identifier
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    validation_status: Literal["draft", "testing", "verified", "suspended"] = "draft"
    acceptance_report: Identifier | None = None
    prompt_version: Identifier
    prompt_context: str = Field(min_length=1, max_length=4000)
    allowed_operations: tuple[Operation, ...] = ("video_generation",)
    narrative_roles: tuple[Identifier, ...]
    selection_weights: dict[Feature, float]
    required_features: tuple[Feature, ...] = ("quality", "semantic_match")
    minimum_scores: dict[Feature, float] = Field(default_factory=dict)
    preserve_facts: Literal[True] = True
    preserve_speech_meaning: bool = False
    source_policy: Literal["real_first", "speech_first"]
    required_quality_dimensions: tuple[Dimension, ...] = ("facts", "technical", "narrative", "visual", "audio")

    @field_validator("selection_weights", "minimum_scores", mode="before")
    @classmethod
    def numeric_values(cls, value):
        if not isinstance(value, dict) or any(type(v) not in (int, float) for v in value.values()):
            raise ValueError("scenario scores must be finite numeric values, not booleans or strings")
        return value

    @model_validator(mode="after")
    def consistent_rules(self):
        if not self.selection_weights or abs(sum(self.selection_weights.values()) - 1) > 1e-8:
            raise ValueError("scenario selection weights must sum to one")
        if any(not 0 <= value <= 1 for value in (*self.selection_weights.values(), *self.minimum_scores.values())):
            raise ValueError("scenario weights and score thresholds must be between zero and one")
        for sequence in (self.allowed_operations, self.narrative_roles, self.required_features, self.required_quality_dimensions):
            if not sequence or len(set(sequence)) != len(sequence):
                raise ValueError("scenario rule lists must be nonempty and unique")
        if set(self.required_features) - set(self.selection_weights):
            raise ValueError("required features must have an explicit selection weight")
        if self.validation_status == "verified" and not self.acceptance_report:
            raise ValueError("verified scenario requires a real acceptance report reference")
        return self

    @property
    def content_hash(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def snapshot(self) -> dict:
        return {**self.model_dump(mode="json"), "content_hash": self.content_hash}


class DimensionReview(ScenarioModel):
    dimension: Dimension
    status: Literal["passed", "failed", "unreviewed", "not_applicable"]
    score: float | None = Field(default=None, ge=0, le=5, strict=True)
    evidence_ids: tuple[Identifier, ...] = Field(default=(), max_length=50)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def evidence_required(self):
        if self.status != "unreviewed" and not self.evidence_ids:
            raise ValueError("an assessed dimension requires stable evidence references")
        if self.status == "unreviewed" and self.score is not None:
            raise ValueError("unreviewed quality must not receive a numeric score")
        if self.status == "not_applicable" and (not self.note.strip() or self.score is not None):
            raise ValueError("not-applicable review needs a reason and no invented score")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("quality evidence references must be unique")
        return self


class ScenarioQualityReview(ScenarioModel):
    schema_version: Literal["scenario-quality-review/1"] = "scenario-quality-review/1"
    scenario: ScenarioBinding
    output_sha256: Digest
    reviewer_id: Identifier
    rubric_version: Identifier
    dimensions: tuple[DimensionReview, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_dimensions(self):
        if not self.scenario.content_hash or not self.scenario.prompt_version:
            raise ValueError("quality review requires the frozen scenario hash and prompt version")
        if len({row.dimension for row in self.dimensions}) != len(self.dimensions):
            raise ValueError("each quality dimension may be reviewed only once")
        return self


class BenchmarkCase(ScenarioModel):
    id: Identifier
    scenario: ScenarioBinding
    split: Literal["tuning", "validation", "holdout"]
    status: Literal["planned", "ready"] = "planned"
    source_group_id: Identifier
    source_sha256: tuple[Digest, ...] = Field(default=(), max_length=100)
    reference_artifact_id: Identifier | None = None
    requirements: tuple[str, ...] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def ready_requires_real_inputs(self):
        if self.status == "ready" and (not self.source_sha256 or not self.reference_artifact_id):
            raise ValueError("ready benchmark requires real source hashes and a human reference artifact")
        if self.status == "ready" and (not self.scenario.content_hash or not self.scenario.prompt_version):
            raise ValueError("ready benchmark requires frozen scenario and prompt versions")
        if len(set(self.source_sha256)) != len(self.source_sha256):
            raise ValueError("benchmark source hashes must be unique within a case")
        if any(not line.strip() or len(line) > 1000 for line in self.requirements):
            raise ValueError("benchmark requirements must be nonempty and bounded")
        return self


class ScenarioBenchmark(ScenarioModel):
    schema_version: Literal["scenario-benchmark/1"] = "scenario-benchmark/1"
    id: Identifier
    version: Identifier
    cases: tuple[BenchmarkCase, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def split_isolation(self):
        if len({row.id for row in self.cases}) != len(self.cases):
            raise ValueError("benchmark case IDs must be unique")
        seen: dict[str, str] = {}
        for case in self.cases:
            # A group also catches recrops/encodes which have different hashes.
            for key in ("group:" + case.source_group_id, *("sha:" + value for value in case.source_sha256)):
                previous = seen.setdefault(key, case.split)
                if previous != case.split:
                    raise ValueError("benchmark source/group leakage across tuning, validation or holdout")
        return self
