from dataclasses import dataclass
from hashlib import sha256
import hmac
import time

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..config import get_settings
from ..models import Project, Tenant
from ..database import get_db
from ..services.identity import IdentityError, cookie_names, resolve_access


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Identity verified by the configured authentication boundary."""

    principal_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    client_type: str | None = None


def _clean_claim(value: str | None, *, field: str) -> str:
    claim = (value or "").strip()
    # Proxy-injected identity headers must be single-valued.  Reject commas so
    # a duplicate HTTP header cannot be normalized differently by the edge
    # proxy and ASGI server before signature verification.
    if (
        not claim
        or len(claim) > 255
        or "," in claim
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in claim)
    ):
        raise HTTPException(status_code=401, detail={"code": "invalid_identity_claim", "field": field})
    return claim


def authenticate_request(
    request: Request,
    db: Session = Depends(get_db),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_auth_principal: str | None = Header(default=None, alias="X-Auth-Principal"),
    x_auth_tenant_id: str | None = Header(default=None, alias="X-Auth-Tenant-ID"),
    x_auth_issued_at: str | None = Header(default=None, alias="X-Auth-Issued-At"),
    x_auth_signature: str | None = Header(default=None, alias="X-Auth-Signature"),
) -> RequestIdentity:
    """Resolve the request identity without introducing an auth SDK.

    The development contract remains the explicit X-Tenant-ID header. The
    trusted-proxy contract uses an HMAC signed claim envelope so an arbitrary
    internet client cannot forge a tenant by simply copying a header. The
    reverse proxy must remove inbound copies of all identity headers before
    injecting its own signed set.
    """
    settings = get_settings()
    if settings.auth_mode == "session":
        if any(name in request.headers for name in (
                "X-Tenant-ID", "X-Auth-Principal", "X-Auth-Tenant-ID", "X-Auth-Issued-At", "X-Auth-Signature")):
            raise HTTPException(400, detail={"code": "client_identity_headers_forbidden"})
        cookie = request.cookies.get(cookie_names(settings)[0])
        authorization = request.headers.get("Authorization")
        if cookie and authorization:
            raise HTTPException(400, detail={"code": "ambiguous_session_credentials"})
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
                raise HTTPException(401, detail={"code": "invalid_session"})
        else:
            token = cookie or ""
        if cookie and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("Origin") != settings.auth_public_origin:
                raise HTTPException(403, detail={"code": "request_origin_forbidden"})
        try:
            context = resolve_access(db, token)
        except IdentityError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from None
        if bool(cookie) != (context.client_type == "web"):
            raise HTTPException(400, detail={"code": "session_transport_mismatch"})
        return RequestIdentity(principal_id=context.user_id, tenant_id=context.tenant_id,
                               user_id=context.user_id, session_id=context.session_id,
                               client_type=context.client_type)
    if settings.auth_mode == "development_header":
        return RequestIdentity(tenant_id=(x_tenant_id or "").strip() or None)

    # A client-supplied development header is ambiguous in production and is
    # rejected even when the signed claim is otherwise valid.
    if x_tenant_id is not None:
        raise HTTPException(status_code=400, detail={"code": "client_tenant_header_forbidden"})
    principal = _clean_claim(x_auth_principal, field="X-Auth-Principal")
    tenant = _clean_claim(x_auth_tenant_id, field="X-Auth-Tenant-ID")
    issued_at = (x_auth_issued_at or "").strip()
    if not issued_at.isdigit():
        raise HTTPException(status_code=401, detail={"code": "invalid_identity_claim", "field": "X-Auth-Issued-At"})
    if not x_auth_signature:
        raise HTTPException(status_code=401, detail={"code": "missing_identity_signature"})
    try:
        issued_seconds = int(issued_at)
    except ValueError as exc:  # pragma: no cover - guarded by isdigit
        raise HTTPException(status_code=401, detail={"code": "invalid_identity_claim", "field": "X-Auth-Issued-At"}) from exc
    if abs(int(time.time()) - issued_seconds) > settings.trusted_proxy_max_age_seconds:
        raise HTTPException(status_code=401, detail={"code": "stale_identity_claim"})
    canonical = f"{issued_at}\n{principal}\n{tenant}".encode("utf-8")
    expected = hmac.new(
        (settings.trusted_proxy_shared_secret or "").encode("utf-8"), canonical, sha256
    ).hexdigest()
    if not hmac.compare_digest(x_auth_signature.strip().lower(), expected):
        raise HTTPException(status_code=401, detail={"code": "invalid_identity_signature"})
    return RequestIdentity(principal_id=principal, tenant_id=tenant)


def require_tenant_id(identity: RequestIdentity = Depends(authenticate_request)) -> str:
    tenant_id = identity.tenant_id
    if not tenant_id:
        if get_settings().auth_mode in {"trusted_proxy", "session"}:
            raise HTTPException(status_code=401, detail={"code": "authenticated_tenant_required"})
        raise HTTPException(status_code=400, detail="X-Tenant-ID header is required")
    return tenant_id


def require_tenant_access(
    tenant_id: str,
    identity: RequestIdentity = Depends(authenticate_request),
) -> RequestIdentity:
    """Protect endpoints that take a tenant ID in the URL path."""
    if identity.tenant_id != tenant_id:
        if not identity.tenant_id and get_settings().auth_mode == "development_header":
            raise HTTPException(status_code=400, detail="X-Tenant-ID header is required")
        if not identity.tenant_id:
            raise HTTPException(status_code=401, detail={"code": "authenticated_tenant_required"})
        raise HTTPException(status_code=403, detail={"code": "tenant_claim_mismatch"})
    return identity


def reject_public_tenant_admin(
    identity: RequestIdentity = Depends(authenticate_request),
) -> RequestIdentity:
    """Tenant provisioning/listing is kept off the public trusted path."""
    if get_settings().auth_mode != "development_header":
        raise HTTPException(status_code=403, detail={"code": "tenant_provisioning_requires_control_plane"})
    return identity


def require_internal_execution(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
) -> None:
    """Internal state mutations are never authorized by an ordinary user claim.

    Development retains the fixture API. Production callers also supply the
    signed tenant identity required by each route; this key alone grants no
    tenant access. An absent configured key disables the HTTP control surface.
    """
    settings = get_settings()
    if settings.auth_mode == "development_header" and settings.app_environment != "production":
        return
    expected = settings.internal_api_secret or ""
    if not expected or not x_internal_api_key or not hmac.compare_digest(expected, x_internal_api_key):
        raise HTTPException(status_code=403, detail={"code": "internal_execution_required"})


def require_metrics_access(x_metrics_key: str | None = Header(default=None, alias='X-Metrics-Key')) -> None:
    """A scrape key grants no execution or per-job diagnostic privileges."""
    settings=get_settings();secret=settings.metrics_api_secret
    if secret is None and settings.auth_mode=='development_header' and settings.app_environment!='production':return
    expected=secret.get_secret_value() if secret else ''
    if not expected or not x_metrics_key or not hmac.compare_digest(expected,x_metrics_key):
        raise HTTPException(403,detail={'code':'metrics_access_required'})


def require_available_database(request: Request,db: Session=Depends(get_db)) -> None:
    if request.url.path.startswith(get_settings().api_prefix+'/') or request.method not in {'GET','HEAD','OPTIONS'}:
        from ..services.recovery_guard import require_active_database
        require_active_database(db)


def ensure_tenant(db: Session, tenant_id: str) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def ensure_project(db: Session, project_id: str, tenant_id: str) -> Project:
    project = db.scalar(select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project
