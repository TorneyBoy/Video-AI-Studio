"""HMAC proofs issued by the external object-storage gateway.

The API does not fetch an object to verify a direct upload.  When an operator
configures ``STORAGE_ATTESTATION_SECRET``, the storage gateway must therefore
sign the upload completion envelope.  The canonical envelope is deliberately
small and contains the asset and object identity, integrity claims, a random
nonce, and a short expiration timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any


ATTESTATION_VERSION = "storage-attestation-v1"


class StorageAttestationError(ValueError):
    """Raised when a storage completion proof cannot be accepted."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def attestation_expiry_seconds(value: datetime) -> int:
    """Normalize an expiry to UTC epoch seconds for deterministic signing."""

    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return int(normalized.astimezone(timezone.utc).timestamp())


def canonical_attestation(
    *,
    asset_id: str,
    object_uri: str,
    size_bytes: int,
    sha256: str | None,
    expires_at: datetime,
    nonce: str,
) -> bytes:
    """Return the exact UTF-8 bytes signed by the storage gateway.

    JSON is serialized with sorted keys, no insignificant whitespace, and the
    fixed version marker.  ``sha256`` is represented as an empty string when
    the gateway does not provide a hash, while a supplied digest is lowercased
    before signing.
    """

    envelope: dict[str, Any] = {
        "asset_id": str(asset_id),
        "expires_at": attestation_expiry_seconds(expires_at),
        "nonce": str(nonce),
        "object_uri": str(object_uri),
        "sha256": str(sha256 or "").lower(),
        "size_bytes": int(size_bytes),
        "version": ATTESTATION_VERSION,
    }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_attestation(
    secret: str,
    *,
    asset_id: str,
    object_uri: str,
    size_bytes: int,
    sha256: str | None,
    expires_at: datetime,
    nonce: str,
) -> str:
    """Create the transport-safe ``sha256=<hex>`` signature value."""

    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_attestation(
            asset_id=asset_id,
            object_uri=object_uri,
            size_bytes=size_bytes,
            sha256=sha256,
            expires_at=expires_at,
            nonce=nonce,
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_attestation(
    secret: str,
    signature: str,
    *,
    asset_id: str,
    object_uri: str,
    size_bytes: int,
    sha256: str | None,
    expires_at: datetime,
    nonce: str,
    now: datetime | None = None,
) -> None:
    """Validate expiry and HMAC binding, raising a safe typed error."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    expiry_seconds = attestation_expiry_seconds(expires_at)
    if expiry_seconds <= int(current.astimezone(timezone.utc).timestamp()):
        raise StorageAttestationError("storage attestation has expired", code="attestation_expired")
    value = str(signature or "").strip()
    if value.lower().startswith("sha256="):
        value = value.split("=", 1)[1]
    expected = sign_attestation(
        secret,
        asset_id=asset_id,
        object_uri=object_uri,
        size_bytes=size_bytes,
        sha256=sha256,
        expires_at=expires_at,
        nonce=nonce,
    ).split("=", 1)[1]
    if not hmac.compare_digest(value.lower(), expected):
        raise StorageAttestationError("storage attestation signature is invalid", code="attestation_invalid")

