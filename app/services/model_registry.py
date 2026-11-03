"""Auditable model registry and runtime policy checks.

The registry is deliberately a small control-plane primitive.  It does not
download weights or import vendor SDKs; it records the exact model build that
an adapter is allowed to run and rejects versions whose commercial, regional,
or lifecycle policy is not satisfied.  A database-backed registry can use the
same dataclass and JSON representation later without changing ProviderRouter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Literal

from .providers import (
    ModelSnapshot,
    ProviderExecutionError,
    ProviderRequest,
)


ModelStatus = Literal["draft", "approved", "suspended", "retired"]
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_ACTIVE_STATUSES = frozenset({"approved"})


@dataclass(frozen=True)
class ModelVersion:
    """Immutable model release record used for routing and provenance."""

    provider: str
    model_id: str
    version: str
    capabilities: tuple[str, ...] = ()
    repo_url: str | None = None
    commit_sha: str | None = None
    code_license: str | None = None
    weights_license: str | None = None
    dataset_terms: str | None = None
    third_party_licenses: tuple[str, ...] = ()
    commercial_allowed: bool = False
    allowed_regions: tuple[str, ...] = ()
    content_restrictions: tuple[str, ...] = ()
    runtime_digest: str | None = None
    weights_sha256: str | None = None
    quality_benchmark: dict[str, float] = field(default_factory=dict)
    status: ModelStatus = "draft"
    reviewed_at: datetime | None = None
    integrity_mode: str = "weights"
    integrity_reference: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.model_id, self.version)

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model_id.strip() or not self.version.strip():
            raise ValueError("provider, model_id and version are required")
        if self.status not in {"draft", "approved", "suspended", "retired"}:
            raise ValueError(f"unsupported model status: {self.status}")
        if self.commit_sha and not _COMMIT_RE.fullmatch(self.commit_sha):
            raise ValueError("commit_sha must be a 7-64 character hexadecimal git SHA")
        if self.weights_sha256 and not _SHA256_RE.fullmatch(self.weights_sha256):
            raise ValueError("weights_sha256 must be a 64 character hexadecimal SHA-256")
        if self.integrity_mode not in {"weights", "provider_version"}:
            raise ValueError("integrity_mode must be weights or provider_version")
        if self.integrity_mode == "provider_version" and not self.integrity_reference:
            raise ValueError("hosted model version evidence reference is required")
        if self.integrity_mode == "provider_version" and self.version.strip().lower() in {"latest", "unknown", "configured", "unspecified"}:
            raise ValueError("hosted model evidence requires a concrete provider version")
        if any(not item.strip() for item in self.capabilities):
            raise ValueError("model capabilities cannot contain empty values")
        if any(not item.strip() for item in self.allowed_regions):
            raise ValueError("allowed_regions cannot contain empty values")
        for name, score in self.quality_benchmark.items():
            if not isinstance(name, str) or not isinstance(score, (int, float)):
                raise ValueError("quality_benchmark values must be numeric")

    def snapshot(self) -> ModelSnapshot:
        """Return a provenance snapshot suitable for an Artifact manifest."""

        return ModelSnapshot(
            provider=self.provider,
            model_id=self.model_id,
            version=self.version,
            weights_sha256=self.weights_sha256,
            runtime_digest=self.runtime_digest,
            license_snapshot={
                "repo_url": self.repo_url,
                "commit_sha": self.commit_sha,
                "code_license": self.code_license,
                "weights_license": self.weights_license,
                "dataset_terms": self.dataset_terms,
                "third_party_licenses": list(self.third_party_licenses),
                "commercial_allowed": self.commercial_allowed,
                "allowed_regions": list(self.allowed_regions),
                "content_restrictions": list(self.content_restrictions),
                "status": self.status,
                "integrity_mode": self.integrity_mode,
                "integrity_reference": self.integrity_reference,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["capabilities"] = list(self.capabilities)
        value["third_party_licenses"] = list(self.third_party_licenses)
        value["allowed_regions"] = list(self.allowed_regions)
        value["content_restrictions"] = list(self.content_restrictions)
        value["reviewed_at"] = self.reviewed_at.isoformat() if self.reviewed_at else None
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelVersion":
        data = dict(value)
        for field_name in ("capabilities", "third_party_licenses", "allowed_regions", "content_restrictions"):
            data[field_name] = tuple(data.get(field_name) or ())
        reviewed = data.get("reviewed_at")
        if isinstance(reviewed, str):
            data["reviewed_at"] = datetime.fromisoformat(reviewed.replace("Z", "+00:00"))
        return cls(**data)


class ModelRegistry:
    """In-memory registry with deterministic lookup and policy validation.

    ``strict`` controls whether an adapter must have a registered model.  The
    production default router uses strict mode; tests and custom callers can
    omit a registry to retain the original provider-only routing behavior.
    """

    def __init__(self, models: Iterable[ModelVersion] = (), *, strict: bool = True) -> None:
        self.strict = strict
        self._models: dict[tuple[str, str, str], ModelVersion] = {}
        for model in models:
            self.register(model)

    def register(self, model: ModelVersion) -> None:
        if model.key in self._models:
            raise ValueError(f"model version already registered: {model.key}")
        self._models[model.key] = model

    def get(self, provider: str, model_id: str, version: str | None = None) -> ModelVersion | None:
        if version is not None:
            return self._models.get((provider, model_id, version))
        candidates = [item for item in self._models.values() if item.provider == provider and item.model_id == model_id]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.status == "approved", item.version))

    def resolve(
        self,
        *,
        provider: str,
        capability: str,
        model_id: str | None = None,
        version: str | None = None,
    ) -> ModelVersion | None:
        candidates = [
            item
            for item in self._models.values()
            if item.provider == provider
            and capability in item.capabilities
            and (model_id is None or item.model_id == model_id)
            and (version is None or item.version == version)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                item.status in _ACTIVE_STATUSES,
                item.quality_benchmark.get(capability, 0.0),
                item.version,
            ),
        )

    def validate_request(
        self,
        model: ModelVersion,
        request: ProviderRequest,
        *,
        commercial_use: bool = True,
    ) -> None:
        """Raise a stable ProviderExecutionError when policy rejects a model."""

        if model.status not in _ACTIVE_STATUSES:
            raise ProviderExecutionError(
                f"model version is not active: {model.provider}/{model.model_id}/{model.version}",
                code="model_not_active",
                provider=model.provider,
                capability=request.capability,
                retryable=False,
                details={"status": model.status, "model_id": model.model_id, "version": model.version},
            )
        if request.capability not in model.capabilities:
            raise ProviderExecutionError(
                "registered model does not advertise the requested capability",
                code="model_capability_mismatch",
                provider=model.provider,
                capability=request.capability,
                retryable=False,
                details={"model_id": model.model_id, "capabilities": list(model.capabilities)},
            )
        if commercial_use and not model.commercial_allowed:
            raise ProviderExecutionError(
                "model license does not allow commercial use",
                code="model_commercial_restricted",
                provider=model.provider,
                capability=request.capability,
                retryable=False,
                details={"model_id": model.model_id, "version": model.version},
            )
        if request.region and model.allowed_regions and request.region.upper() not in {
            item.upper() for item in model.allowed_regions
        }:
            raise ProviderExecutionError(
                f"model is not approved for region '{request.region}'",
                code="model_region_restricted",
                provider=model.provider,
                capability=request.capability,
                retryable=False,
                details={"region": request.region, "allowed_regions": list(model.allowed_regions)},
            )
        if commercial_use and not model.weights_license:
            raise ProviderExecutionError(
                "model weights license is not recorded",
                code="model_license_unknown",
                provider=model.provider,
                capability=request.capability,
                retryable=False,
            )
        if commercial_use and not model.weights_sha256 and model.integrity_mode != "provider_version":
            raise ProviderExecutionError(
                "model weights checksum is not recorded",
                code="model_integrity_unverified",
                provider=model.provider,
                capability=request.capability,
                retryable=False,
            )

    def validate_snapshot(self, expected: ModelVersion, actual: ModelSnapshot, request: ProviderRequest) -> None:
        """Prevent an adapter from returning a model different from the one routed."""

        if (actual.provider, actual.model_id, actual.version) != expected.key:
            raise ProviderExecutionError(
                "provider returned a model different from the registered version",
                code="model_snapshot_mismatch",
                provider=actual.provider,
                capability=request.capability,
                retryable=False,
                details={"expected": expected.key, "actual": (actual.provider, actual.model_id, actual.version)},
            )
        if expected.weights_sha256 and actual.weights_sha256 != expected.weights_sha256:
            raise ProviderExecutionError(
                "provider returned a model with a different weights checksum",
                code="model_weights_mismatch",
                provider=actual.provider,
                capability=request.capability,
                retryable=False,
            )
        if expected.runtime_digest and actual.runtime_digest != expected.runtime_digest:
            raise ProviderExecutionError(
                "provider returned a model with a different runtime digest",
                code="model_runtime_mismatch",
                provider=actual.provider,
                capability=request.capability,
                retryable=False,
            )

    @classmethod
    def from_json(cls, path: str | Path, *, strict: bool = True) -> "ModelRegistry":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload, strict=strict)

    @classmethod
    def from_dict(cls, payload: Any, *, strict: bool = True) -> "ModelRegistry":
        if isinstance(payload, dict):
            payload = payload.get("models", [])
        if not isinstance(payload, list):
            raise ValueError("model registry JSON must be an array or an object with a models array")
        return cls((ModelVersion.from_dict(item) for item in payload), strict=strict)

    def to_dict(self) -> dict[str, Any]:
        return {"models": [item.to_dict() for item in self._models.values()]}

    def to_json(self, path: str | Path) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def all(self) -> tuple[ModelVersion, ...]:
        return tuple(self._models.values())


def development_registry() -> ModelRegistry:
    """Registry used by the local deterministic manifest adapter."""

    checksum = hashlib.sha256(b"video-ai-studio/local-deterministic-manifest/v1").hexdigest()
    return ModelRegistry(
        [
            ModelVersion(
                provider="local-deterministic",
                model_id="deterministic-manifest",
                version="1",
                capabilities=(
                    "voice_tts",
                    "voice_clone",
                    "voice_conversion",
                    "video_generation",
                    "motion_transfer",
                    "face_transform",
                    "lip_sync",
                    "enhancement",
                ),
                code_license="internal-development",
                weights_license="no-weights",
                commercial_allowed=True,
                runtime_digest="python-standard-library",
                weights_sha256=checksum,
                status="approved",
                reviewed_at=datetime.now(timezone.utc),
            )
        ]
    )

