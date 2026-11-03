import json

import pytest

from app.services.provenance import ProvenanceManifest, read_sidecar, write_sidecar


def test_provenance_manifest_seals_and_verifies():
    manifest = ProvenanceManifest(
        artifact_id="artifact-1",
        artifact_type="video",
        source_asset_ids=["asset-1"],
        operations=[{"type": "auto_edit", "version": 1}],
        consent_ids=["consent-1"],
        rights_confirmed=True,
        ai_generated=True,
    ).seal()
    assert manifest.manifest_hash
    assert manifest.verify()
    altered = manifest.model_copy(update={"rights_confirmed": False})
    assert not altered.verify()


def test_write_sidecar_creates_parent_and_round_trips_verified_manifest(tmp_path):
    manifest = ProvenanceManifest(artifact_id="artifact-2", artifact_type="audio")
    destination = tmp_path / "nested" / "artifact.provenance.json"

    assert write_sidecar(manifest, destination) == str(destination)
    loaded = read_sidecar(destination)

    assert loaded.artifact_id == "artifact-2"
    assert loaded.verify()
    assert not list(destination.parent.glob(".*.tmp"))


def test_read_sidecar_rejects_tampering(tmp_path):
    destination = tmp_path / "artifact.provenance.json"
    write_sidecar(ProvenanceManifest(artifact_id="artifact-3", artifact_type="video"), destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload["rights_confirmed"] = True
    destination.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid provenance manifest hash"):
        read_sidecar(destination)
