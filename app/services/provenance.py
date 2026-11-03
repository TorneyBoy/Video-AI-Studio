"""Deterministic provenance manifest used before a real C2PA signer is added."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProvenanceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    artifact_id: str
    artifact_type: str
    source_asset_ids: list[str] = Field(default_factory=list)
    timeline_id: str | None = None
    operations: list[dict[str, Any]] = Field(default_factory=list)
    model_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    consent_ids: list[str] = Field(default_factory=list)
    rights_confirmed: bool = False
    ai_generated: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    manifest_hash: str = ""

    def _canonical_payload(self) -> str:
        """Return the exact payload covered by ``manifest_hash``.

        Keeping canonicalisation in one method is important because JSON key
        order and datetime serialisation must be identical when a manifest is
        sealed, persisted, and verified by a different worker.
        """
        payload = self.model_dump(mode="json", exclude={"manifest_hash"})
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def seal(self) -> "ProvenanceManifest":
        digest = hashlib.sha256(self._canonical_payload().encode("utf-8")).hexdigest()
        return self.model_copy(update={"manifest_hash": digest})

    def verify(self) -> bool:
        if not self.manifest_hash:
            return False
        digest = hashlib.sha256(self._canonical_payload().encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, self.manifest_hash)


def write_sidecar(manifest: ProvenanceManifest, path: str | Path) -> str:
    """Atomically write a sealed JSON sidecar.

    Workers can be interrupted while encoding a large artifact.  Writing to a
    sibling temporary file and replacing the destination prevents consumers
    from observing a truncated manifest.  Parent directories are created to
    make this safe for a newly allocated artifact path.
    """
    sealed = manifest.seal()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = output.name
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return str(destination)


def read_sidecar(path: str | Path, *, verify: bool = True) -> ProvenanceManifest:
    """Load a sidecar and optionally enforce its content hash."""
    manifest = ProvenanceManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if verify and not manifest.verify():
        raise ValueError(f"invalid provenance manifest hash: {path}")
    return manifest
