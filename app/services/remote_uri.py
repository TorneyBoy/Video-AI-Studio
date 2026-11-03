"""Validation for URIs handed to hosted storage and AI providers.

The control plane never dereferences media URLs, but it does hand them to
external services.  Rejecting insecure and loopback targets here prevents a
tenant from accidentally turning a hosted provider into an SSRF primitive.
Object-storage URIs remain supported because they are resolved by the
provider's storage integration rather than by this process.
"""

from __future__ import annotations

from ipaddress import ip_address
import socket
from urllib.parse import urlparse


REMOTE_URI_SCHEMES = frozenset({"https", "s3", "gs", "oss", "az", "azure"})
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


class RemoteURIError(ValueError):
    """Raised when an external URI is not safe to pass downstream."""


def _literal_ip(hostname: str):
    """Parse canonical and legacy IPv4 literals.

    ``ipaddress.ip_address`` deliberately accepts only canonical dotted
    notation for IPv4.  URL consumers and HTTP clients also accept legacy
    forms such as ``2130706433`` (127.0.0.1), hexadecimal, octal, and
    shortened dotted addresses.  Treating those values as hostnames leaves a
    common SSRF bypass.  ``inet_aton`` is used only as a parser; no DNS lookup
    or network request is performed.
    """

    try:
        return ip_address(hostname)
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(hostname)
    except OSError:
        return None
    # inet_aton returns an IPv4 packed address and accepts the legacy forms
    # described above.  Re-enter the standard parser so all later checks use
    # the same ``ipaddress`` type and properties.
    return ip_address(socket.inet_ntoa(packed))


def validate_remote_uri(value: str, *, field: str = "uri") -> str:
    """Validate a stable object/artifact URI for hosted provider use.

    HTTPS is required for web URLs.  S3/GCS/OSS/Azure style object URIs are
    allowed as stable references.  Literal private, loopback, link-local and
    otherwise reserved IP targets are rejected to avoid handing internal
    network addresses to a provider that fetches inputs or outputs.
    """

    uri = str(value or "").strip()
    if not uri or any(ord(char) < 0x20 or ord(char) == 0x7F for char in uri):
        raise RemoteURIError(f"{field} must not contain control characters")
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme not in REMOTE_URI_SCHEMES or not parsed.netloc:
        raise RemoteURIError(f"{field} must use HTTPS or a supported object-storage URI")
    if parsed.username or parsed.password:
        raise RemoteURIError(f"{field} must not contain embedded credentials")
    try:
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise RemoteURIError(f"{field} has an invalid host") from exc
    try:
        # Accessing ``port`` makes urllib.parse validate malformed and
        # out-of-range ports.  A bad port would otherwise pass this gate and
        # fail later in a provider-specific HTTP client.
        parsed.port
    except ValueError as exc:
        raise RemoteURIError(f"{field} has an invalid port") from exc
    if not hostname or hostname in _BLOCKED_HOSTNAMES:
        raise RemoteURIError(f"{field} must not target a local host")
    address = _literal_ip(hostname)
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise RemoteURIError(f"{field} must not target a private or reserved IP address")
    return uri


def validate_presigned_upload_url(value: str) -> str:
    """Validate a browser direct-upload URL without dereferencing it."""

    return validate_remote_uri(value, field="upload_url")
