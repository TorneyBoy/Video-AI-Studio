"""Archival and renewable access are distinct from provider task completion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import Settings
from .storage_attestation import StorageAttestationError, verify_attestation
from .storage_gateway import StorageGateway, StorageGatewayError


def _artifact(db: Session, tenant_id: str, artifact_id: str, *, lock: bool = False):
    query = select(models.Artifact).where(models.Artifact.id == artifact_id, models.Artifact.tenant_id == tenant_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise StorageGatewayError("artifact_not_found", "Artifact not found", status_code=404)
    return row


def archive_artifact(db: Session, *, tenant_id: str, artifact_id: str, settings: Settings,
                     gateway: StorageGateway) -> models.ArtifactStorage:
    row = _artifact(db, tenant_id, artifact_id)
    existing = db.get(models.ArtifactStorage, artifact_id)
    if existing:
        return existing
    if row.legacy_duplicate_of or row.delivery_status in {"revoked", "deleted", "archived_duplicate"}:
        raise StorageGatewayError("artifact_delivery_forbidden", "Artifact is not eligible for archival", status_code=409)
    source_uri = row.uri
    expected_hash = (row.metadata_json or {}).get("output_sha256")
    if not settings.storage_attestation_secret:
        raise StorageGatewayError("storage_attestation_unconfigured", "Archive verification is not configured", status_code=503)
    db.commit()
    value = gateway.archive_artifact(artifact_id=artifact_id, tenant_id=tenant_id,
                                     source_uri=source_uri, expected_sha256=expected_hash)
    try:
        completion = schemas.RemoteUploadComplete.model_validate(value)
        if not completion.sha256 or not completion.attestation:
            raise ValueError("Incomplete archive evidence")
        proof = completion.attestation
        verify_attestation(settings.storage_attestation_secret, proof.signature,
                           asset_id=artifact_id, object_uri=value["object_uri"], size_bytes=completion.size_bytes,
                           sha256=completion.sha256, nonce=proof.nonce, expires_at=proof.expires_at)
        if expected_hash and completion.sha256.lower() != str(expected_hash).lower():
            raise ValueError("Archive differs from render output")
    except (StorageAttestationError, ValueError) as exc:
        raise StorageGatewayError("storage_archive_verification_failed", "Archive integrity could not be verified") from exc
    row = _artifact(db, tenant_id, artifact_id, lock=True)
    existing = db.get(models.ArtifactStorage, artifact_id)
    if existing:
        return existing
    if row.uri != source_uri or row.delivery_status in {"revoked", "deleted", "archived_duplicate"}:
        raise StorageGatewayError("artifact_changed", "Artifact changed while it was being archived", status_code=409)
    archive = models.ArtifactStorage(artifact_id=artifact_id, tenant_id=tenant_id,
        object_uri=value["object_uri"], sha256=completion.sha256.lower(), size_bytes=completion.size_bytes,
        etag=completion.etag, expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        evidence_json={"attestation": completion.attestation.model_dump(mode="json"),
                       "verification": completion.metadata.get("verification"), "source": "storage_gateway"})
    db.add(archive)
    # Archival confirms bytes only; it does not approve media quality or review.
    db.commit()
    return archive


def _eligible_delivery(db: Session, *, tenant_id: str, artifact_id: str):
    row = _artifact(db, tenant_id, artifact_id)
    archive = db.get(models.ArtifactStorage, artifact_id)
    if row.delivery_status in {"revoked", "deleted", "archived_duplicate"} or row.legacy_duplicate_of:
        raise StorageGatewayError("artifact_delivery_forbidden", "Artifact delivery was revoked", status_code=403)
    if archive is None:
        raise StorageGatewayError("artifact_archive_pending", "Artifact is not yet archived", status_code=409)
    expires = archive.expires_at.replace(tzinfo=timezone.utc) if archive.expires_at.tzinfo is None else archive.expires_at
    if expires <= datetime.now(timezone.utc):
        raise StorageGatewayError("artifact_retention_expired", "Artifact retention has expired", status_code=410)
    metadata = row.metadata_json or {}
    qc = metadata.get("quality_control") or {}
    if (not isinstance(qc, dict) or qc.get("status") != "passed" or qc.get("output_sha256") != archive.sha256
            or qc.get("size_bytes") != archive.size_bytes):
        raise StorageGatewayError("artifact_quality_pending", "Archived content has not passed media quality checks", status_code=409)
    if row.job_id:
        job = db.get(models.Job, row.job_id)
        if job is None or job.status != "succeeded" or job.policy_blocked:
            raise StorageGatewayError("artifact_job_not_deliverable", "Task is not eligible for delivery", status_code=409)
    return archive, expires


def artifact_access(db: Session, *, tenant_id: str, artifact_id: str, purpose: str,
                    gateway: StorageGateway, expires_at_limit: datetime | None = None) -> dict:
    archive, expires = _eligible_delivery(db, tenant_id=tenant_id, artifact_id=artifact_id)
    if purpose not in {"preview", "download"}:
        raise StorageGatewayError("artifact_access_purpose", "Unsupported access purpose", status_code=422)
    from .artifact_reviews import require_approved
    review_id = require_approved(db, tenant_id=tenant_id, artifact_id=artifact_id) if purpose == "download" else None
    object_uri = archive.object_uri
    if expires_at_limit is not None:
        expires = min(expires, expires_at_limit)
    ttl = min(300, int((expires - datetime.now(timezone.utc)).total_seconds()))
    if ttl < 1:
        raise StorageGatewayError("artifact_retention_expired", "Artifact retention has expired", status_code=410)
    db.commit()
    result = gateway.artifact_access(artifact_id=artifact_id, tenant_id=tenant_id, object_uri=object_uri,
                                     purpose=purpose, ttl_seconds=ttl)
    # The signing request may race with revocation, deletion or expiry. Re-check.
    db.expire_all()
    current, current_expiry = _eligible_delivery(db, tenant_id=tenant_id, artifact_id=artifact_id)
    if purpose == "download" and require_approved(db, tenant_id=tenant_id, artifact_id=artifact_id) != review_id:
        raise StorageGatewayError("artifact_review_changed", "签名期间审核状态已变化，请重新核对当前成片", status_code=409)
    if current.object_uri != object_uri or result["expires_at"] > min(current_expiry, expires):
        raise StorageGatewayError("artifact_access_stale", "Artifact access changed during signing", status_code=409)
    return result
