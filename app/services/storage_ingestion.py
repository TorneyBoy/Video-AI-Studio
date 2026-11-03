"""Durable direct uploads shared by browser and native mini-program clients."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import Settings
from .storage_attestation import StorageAttestationError, attestation_expiry_seconds, verify_attestation
from .storage_gateway import StorageGateway, StorageGatewayError


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _proof_record(proof: schemas.RemoteUploadAttestation) -> dict:
    signature = proof.signature.strip().lower()
    return {"version": "storage-attestation-v1", "nonce": proof.nonce,
            "expires_at_epoch": attestation_expiry_seconds(proof.expires_at),
            "signature": signature if signature.startswith("sha256=") else "sha256=" + signature}


def apply_upload_completion(db: Session, asset: models.Asset, payload: schemas.RemoteUploadComplete,
                            settings: Settings) -> models.Asset:
    """Validate and update an upload; caller owns the transaction/commit."""
    db.refresh(asset, with_for_update=True)
    metadata = dict(asset.metadata_json or {})
    storage = dict(metadata.get("storage") or {})
    if storage.get("mode") != "external_presigned":
        raise StorageGatewayError("external_upload_session_required", "Asset does not have an external presigned upload session", status_code=409)
    if storage.get("status") == "ready":
        if (storage.get("size_bytes") and payload.size_bytes != storage["size_bytes"]
                or storage.get("sha256") and str(payload.sha256 or "").lower() != storage["sha256"]):
            raise StorageGatewayError("upload_already_completed", "Asset was completed with different integrity evidence", status_code=409)
        if settings.storage_attestation_secret:
            if payload.attestation is None or storage.get("attestation") != _proof_record(payload.attestation):
                raise StorageGatewayError("upload_already_completed", "Asset was completed with a different attestation", status_code=409)
        return asset
    expires = None
    if storage.get("expires_at"):
        try:
            expires = _utc(datetime.fromisoformat(str(storage["expires_at"])))
        except ValueError as exc:
            raise StorageGatewayError("upload_session_expiry_invalid", "External upload session has invalid expiration metadata", status_code=409) from exc
        if datetime.now(timezone.utc) > expires:
            raise StorageGatewayError("upload_session_expired", "External upload session has expired", status_code=410)
    expected_size = int(storage.get("size_bytes") or 0)
    if expected_size and payload.size_bytes != expected_size:
        raise StorageGatewayError("upload_size_mismatch", "Verified object size does not match the upload intent", status_code=422)
    expected_hash = str(storage.get("sha256") or "").lower()
    if expected_hash and (not payload.sha256 or payload.sha256.lower() != expected_hash):
        raise StorageGatewayError("upload_sha256_mismatch", "Verified object hash does not match the upload intent", status_code=422)
    if settings.storage_attestation_secret:
        proof = payload.attestation
        if proof is None:
            raise StorageGatewayError("storage_attestation_required", "Storage gateway attestation is required", status_code=401)
        try:
            expiry = attestation_expiry_seconds(proof.expires_at)
            if expires and expiry > attestation_expiry_seconds(expires):
                raise StorageAttestationError("storage attestation outlives the upload session", code="attestation_expiry_mismatch")
            verify_attestation(settings.storage_attestation_secret, proof.signature,
                               asset_id=asset.id, object_uri=asset.uri, size_bytes=payload.size_bytes,
                               sha256=payload.sha256, expires_at=proof.expires_at, nonce=proof.nonce)
            if expiry <= int(datetime.now(timezone.utc).timestamp()):
                raise StorageAttestationError("storage attestation has expired", code="attestation_expired")
        except StorageAttestationError as exc:
            raise StorageGatewayError(exc.code, str(exc), status_code=401) from exc
        storage["attestation"] = _proof_record(proof)
    storage.update({"status": "ready", "size_bytes": payload.size_bytes, "completed_at": datetime.now(timezone.utc).isoformat()})
    if payload.sha256:
        storage["sha256"] = payload.sha256.lower()
    if payload.etag:
        storage["etag"] = payload.etag
    if payload.metadata:
        storage["completion_metadata"] = dict(payload.metadata)
    asset.metadata_json = {**metadata, "storage": storage}
    db.flush()
    return asset


def _intent_for_key(db: Session, tenant_id: str, key: str) -> models.StorageUploadIntent | None:
    return db.scalar(select(models.StorageUploadIntent).where(
        models.StorageUploadIntent.tenant_id == tenant_id, models.StorageUploadIntent.idempotency_key == key))


def _locked_intent(db: Session, tenant_id: str, asset_id: str):
    asset = db.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant_id)
                      .with_for_update().execution_options(populate_existing=True))
    intent = db.scalar(select(models.StorageUploadIntent).where(
        models.StorageUploadIntent.asset_id == asset_id, models.StorageUploadIntent.tenant_id == tenant_id)
        .with_for_update().execution_options(populate_existing=True))
    if asset is None or intent is None:
        raise StorageGatewayError("upload_intent_not_found", "Upload intent not found", status_code=404)
    return asset, intent


def _session_read(intent: models.StorageUploadIntent, settings: Settings, grant=None) -> schemas.RemoteUploadSessionRead:
    return schemas.RemoteUploadSessionRead(
        upload_id=intent.id, asset_id=intent.asset_id, object_uri=intent.object_uri or "",
        upload_url=grant.upload_url if grant else None, upload_method=grant.method if grant else None,
        upload_headers=grant.headers if grant else {}, upload_fields=grant.fields if grant else {},
        upload_file_field=grant.file_field if grant else "file",
        complete_uri=f"{settings.api_prefix}/assets/{intent.asset_id}/verify-storage-upload",
        expires_at=grant.expires_at if grant else intent.expires_at or datetime.now(timezone.utc),
        status="ready" if intent.status == "completed" else "created")


def create_gateway_upload(db: Session, *, tenant_id: str, key: str, specification: schemas.UploadSessionCreate,
                           settings: Settings, gateway: StorageGateway) -> schemas.RemoteUploadSessionRead:
    if settings.asset_storage_mode == "local":
        raise StorageGatewayError("external_upload_disabled", "External upload is disabled", status_code=409)
    if specification.size_bytes > settings.max_upload_bytes:
        raise StorageGatewayError("upload_too_large", "Upload exceeds configured size limit", status_code=413)
    request = specification.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    intent = _intent_for_key(db, tenant_id, key)
    if intent is None:
        asset = models.Asset(tenant_id=tenant_id, project_id=specification.project_id,
                             name=specification.name, kind=specification.content_type,
                             uri="upload://pending/" + models.new_id(),
                             metadata_json={"storage": {"mode": "external_presigned", "status": "created"}})
        db.add(asset)
        db.flush()
        intent = models.StorageUploadIntent(tenant_id=tenant_id, project_id=specification.project_id,
                                            asset_id=asset.id, idempotency_key=key, request_hash=digest,
                                            request_json=request, status="requesting")
        db.add(intent)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            intent = _intent_for_key(db, tenant_id, key)
            if intent is None:
                raise StorageGatewayError("upload_intent_conflict", "Upload intent could not be allocated", status_code=409)
    if intent.request_hash != digest:
        raise StorageGatewayError("idempotency_input_conflict", "Idempotency key is already bound to a different upload", status_code=409)
    if intent.status == "completed":
        return _session_read(intent, settings)
    asset_id = intent.asset_id
    db.commit()  # Never hold an allocation or asset lock while contacting storage.
    try:
        grant = gateway.upload_grant(asset_id=asset_id, tenant_id=tenant_id,
                                     project_id=specification.project_id, specification=request)
    except StorageGatewayError as exc:
        _, intent = _locked_intent(db, tenant_id, asset_id)
        if intent.status != "completed":
            intent.status = "unknown"
            intent.last_error_code = exc.code
        db.commit()
        raise
    asset, intent = _locked_intent(db, tenant_id, asset_id)
    if intent.status == "completed" or (asset.metadata_json.get("storage") or {}).get("status") == "ready":
        intent.status = "completed"
        result = _session_read(intent, settings)
        db.commit()
        return result
    if intent.object_uri and intent.object_uri != grant.object_uri:
        raise StorageGatewayError("upload_destination_changed", "Storage service changed an immutable upload destination", status_code=409)
    intent.object_uri = grant.object_uri
    intent.expires_at = max(_utc(intent.expires_at), grant.expires_at) if intent.expires_at else grant.expires_at
    intent.status = "ready"
    intent.last_error_code = None
    asset.uri = grant.object_uri
    asset.metadata_json = {**asset.metadata_json, "storage": {
        "mode": "external_presigned", "status": "created", "object_uri": grant.object_uri,
        "size_bytes": specification.size_bytes, "sha256": specification.sha256,
        "expires_at": intent.expires_at.isoformat(), "gateway_intent_id": intent.id,
    }}
    result = _session_read(intent, settings, grant)
    db.commit()
    return result


def verify_gateway_upload(db: Session, *, tenant_id: str, asset_id: str,
                           settings: Settings, gateway: StorageGateway) -> models.Asset:
    asset, intent = _locked_intent(db, tenant_id, asset_id)
    if intent.status == "completed" or (asset.metadata_json.get("storage") or {}).get("status") == "ready":
        intent.status = "completed"
        db.commit()
        return asset
    if not intent.object_uri:
        raise StorageGatewayError("upload_grant_not_ready", "Request an upload grant before verifying the object", status_code=409)
    object_uri = intent.object_uri
    db.commit()
    value = gateway.verify_upload(asset_id=asset_id, tenant_id=tenant_id, object_uri=object_uri)
    try:
        completion = schemas.RemoteUploadComplete.model_validate(value)
        # Actual hash and asset-bound attestation are mandatory for this path,
        # even in development. The gateway observes the object, not the client.
        if not completion.sha256 or not completion.attestation or not settings.storage_attestation_secret:
            raise ValueError("missing verification evidence")
    except (ValidationError, ValueError) as exc:
        raise StorageGatewayError("storage_verification_incomplete", "Storage service did not provide complete object evidence") from exc
    asset, intent = _locked_intent(db, tenant_id, asset_id)
    if intent.status == "completed":
        db.commit()
        return asset
    apply_upload_completion(db, asset, completion, settings)
    intent.status = "completed"
    intent.last_error_code = None
    db.commit()
    return asset
