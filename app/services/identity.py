"""Shared personal accounts and revocable opaque sessions for both clients.

Passwords use the stdlib scrypt implementation; tokens have 256 random bits
and only their hashes enter the database. No media/vendor execution occurs.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
from threading import BoundedSemaphore

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import models
from ..config import Settings


ACCESS_COOKIE = "video_ai_access"
REFRESH_COOKIE = "video_ai_refresh"
_PASSWORD_WORK = BoundedSemaphore(1)
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 17, 8, 1
_TOKEN = re.compile(r"^vas_[ar]_[A-Za-z0-9_-]{43}$")


def cookie_names(settings: Settings) -> tuple[str, str]:
    prefix = "__Host-" if settings.app_environment == "production" else ""
    return prefix + ACCESS_COOKIE, prefix + REFRESH_COOKIE


class IdentityError(ValueError):
    def __init__(self, code: str, message: str, status: int = 401, *, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


@dataclass(frozen=True)
class SessionContext:
    user_id: str
    tenant_id: str
    session_id: str
    client_type: str


@dataclass(frozen=True)
class SessionGrant:
    context: SessionContext
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_at: datetime
    refresh_expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def normalize_login_name(value: str) -> str:
    name = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,40}", name):
        raise IdentityError("invalid_login_name", "账号需为3至40位字母、数字或下划线", 422)
    return name


def _password_key(password: str, salt: bytes) -> bytes:
    # Bound memory even if many login requests reach the synchronous pool.
    if not _PASSWORD_WORK.acquire(blocking=False):
        raise IdentityError("authentication_busy", "登录服务繁忙，请稍后重试", 429, retry_after=2)
    try:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                              r=_SCRYPT_R, p=_SCRYPT_P, maxmem=256 * 1024 * 1024, dklen=32)
    finally:
        _PASSWORD_WORK.release()


def hash_password(password: str) -> str:
    if not 15 <= len(password) <= 128 or len(password.encode("utf-8")) > 512:
        raise IdentityError("invalid_password_length", "密码需为15至128个字符", 422)
    salt = secrets.token_bytes(16)
    key = _password_key(password, salt)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if len(password) > 128 or len(password.encode("utf-8")) > 512:
        return False
    valid = False
    salt, expected = b"video-ai-dummy-1", bytes(32)
    if encoded:
        try:
            algorithm, n, r, p, salt_hex, key_hex = encoded.split("$")
            if (algorithm, n, r, p) == ("scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P)):
                salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(key_hex)
                valid = len(salt) == 16 and len(expected) == 32
        except (ValueError, TypeError):
            pass
    actual = _password_key(password, salt if valid else b"video-ai-dummy-1")
    return hmac.compare_digest(actual, expected) and valid


def throttle(db: Session, *, scope: str, subject: str, limit: int, seconds: int = 600) -> None:
    """Reserve an attempt atomically; a later rejected login cannot roll it back."""
    now = _now()
    start = int(now.timestamp()) // seconds * seconds
    key = hashlib.sha256(f"{scope}\n{subject}\n{start}".encode()).hexdigest()
    expires = datetime.fromtimestamp(start + seconds, timezone.utc)
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(models.AuthThrottle).values(id=key, attempts=1, expires_at=expires)
    statement = statement.on_conflict_do_update(
        index_elements=[models.AuthThrottle.id], set_={"attempts": models.AuthThrottle.attempts + 1})
    attempts = db.scalar(statement.returning(models.AuthThrottle.attempts))
    db.commit()
    if attempts > limit:
        raise IdentityError("authentication_rate_limited", "尝试次数较多，请稍后再试", 429,
                            retry_after=max(1, int((expires - now).total_seconds())))


def _event(db: Session, event: str, *, user_id: str | None = None,
           session_id: str | None = None, details: dict | None = None) -> None:
    db.add(models.AuthEvent(event_type=event, user_id=user_id, session_id=session_id,
                            details_json=details or {}))


def _token_hash(value: str, kind: str) -> str:
    if not _TOKEN.fullmatch(value) or not value.startswith("vas_" + kind + "_"):
        raise IdentityError("invalid_session", "登录已失效，请重新登录")
    return hashlib.sha256(value.encode()).hexdigest()


def _new_tokens() -> tuple[str, str]:
    return "vas_a_" + secrets.token_urlsafe(32), "vas_r_" + secrets.token_urlsafe(32)


def _active_user(db: Session, row: models.UserSession) -> models.User:
    user = db.get(models.User, row.user_id, populate_existing=True)
    member = db.scalar(select(models.Membership.id).where(
        models.Membership.user_id == row.user_id, models.Membership.tenant_id == row.tenant_id,
        models.Membership.status == "active", models.Membership.role.in_(("owner", "editor"))))
    if (user is None or user.status != "active" or user.credential_epoch != row.credential_epoch
            or member is None or row.revoked_at is not None):
        raise IdentityError("session_revoked", "登录已失效，请重新登录")
    return user


def issue_session(db: Session, user: models.User, *, client_type: str,
                  settings: Settings, method: str = "password") -> SessionGrant:
    if client_type not in {"web", "mini"} or user.status != "active":
        raise IdentityError("invalid_session_client", "无法建立登录会话", 422)
    access, refresh = _new_tokens()
    now = _now()
    row = models.UserSession(user_id=user.id, tenant_id=user.personal_tenant_id,
        client_type=client_type, login_method=method, credential_epoch=user.credential_epoch,
        access_token_hash=_token_hash(access, "a"),
        access_expires_at=now + timedelta(seconds=settings.auth_access_ttl_seconds),
        refresh_expires_at=now + timedelta(seconds=settings.auth_refresh_ttl_seconds))
    db.add(row)
    db.flush()
    db.add(models.RefreshToken(session_id=row.id, token_hash=_token_hash(refresh, "r"),
                               expires_at=row.refresh_expires_at))
    _event(db, "session_created", user_id=user.id, session_id=row.id,
           details={"client": client_type, "method": method})
    return SessionGrant(SessionContext(user.id, row.tenant_id, row.id, client_type), access, refresh,
                        row.access_expires_at, row.refresh_expires_at)


def register_password_account(db: Session, *, login_name: str, password: str,
                              display_name: str, client_type: str, settings: Settings) -> SessionGrant:
    if not settings.auth_registration_enabled:
        raise IdentityError("registration_disabled", "当前暂未开放注册", 403)
    name = normalize_login_name(login_name)
    if db.scalar(select(models.User.id).where(models.User.login_name == name)):
        raise IdentityError("login_name_unavailable", "该账号已被使用", 409)
    display = display_name.strip() or name
    if len(display) > 80 or any(ord(c) < 32 for c in display):
        raise IdentityError("invalid_display_name", "显示名称不符合要求", 422)
    password_hash = hash_password(password)
    tenant = models.Tenant(slug="personal-" + secrets.token_hex(16), name=display + "的工作台")
    db.add(tenant)
    db.flush()
    user = models.User(login_name=name, display_name=display, password_hash=password_hash,
                        personal_tenant_id=tenant.id, credential_epoch=0)
    db.add(user)
    db.flush()
    db.add(models.Membership(user_id=user.id, tenant_id=tenant.id, role="owner", status="active"))
    # New users receive a blocking budget. An unset limit cannot quietly grant
    # a new account unlimited access to the owner's paid cloud resources.
    db.add(models.TenantBudget(tenant_id=tenant.id, currency="CNY", monthly_limit_micros=0,
                               task_limit_micros=0, enforcement="block", enabled=True))
    _event(db, "account_created", user_id=user.id)
    return issue_session(db, user, client_type=client_type, settings=settings)


def password_login(db: Session, *, login_name: str, password: str,
                   client_type: str, settings: Settings) -> SessionGrant:
    # Malformed/nonexistent names follow the same password work path.
    name = login_name.strip().lower()
    user = db.scalar(select(models.User).where(models.User.login_name == name))
    encoded = user.password_hash if user and user.status == "active" else None
    if not verify_password(password, encoded):
        raise IdentityError("invalid_credentials", "账号或密码不正确")
    db.refresh(user, with_for_update=True)
    if user.status != "active" or user.password_hash != encoded:
        raise IdentityError("invalid_credentials", "账号或密码不正确")
    return issue_session(db, user, client_type=client_type, settings=settings)


def resolve_access(db: Session, token: str) -> SessionContext:
    digest = _token_hash(token, "a")
    row = db.scalar(select(models.UserSession).where(models.UserSession.access_token_hash == digest))
    if row is None or _utc(row.access_expires_at) <= _now():
        raise IdentityError("session_expired", "登录已失效，请重新登录")
    _active_user(db, row)
    return SessionContext(row.user_id, row.tenant_id, row.id, row.client_type)


def rotate_refresh(db: Session, token: str, *, client_type: str, settings: Settings) -> SessionGrant:
    digest = _token_hash(token, "r")
    old = db.scalar(select(models.RefreshToken).where(models.RefreshToken.token_hash == digest))
    if old is None:
        raise IdentityError("session_expired", "登录已失效，请重新登录")
    # Serialize the family first. Refresh, logout and credential revocation
    # share this order; two refreshes cannot both consume one token.
    row = db.scalar(select(models.UserSession).where(models.UserSession.id == old.session_id)
                    .with_for_update().execution_options(populate_existing=True))
    db.refresh(old)
    now = _now()
    if row is None or row.client_type != client_type:
        raise IdentityError("invalid_session", "登录已失效，请重新登录")
    if old.consumed_at is not None:
        row.revoked_at = row.revoked_at or now
        _event(db, "refresh_replay_revoked", user_id=row.user_id, session_id=row.id)
        # Preserve revocation when the HTTP response is a rejection.
        db.commit()
        raise IdentityError("session_revoked", "登录已失效，请重新登录")
    if min(_utc(old.expires_at), _utc(row.refresh_expires_at)) <= now:
        raise IdentityError("session_expired", "登录已失效，请重新登录")
    _active_user(db, row)
    access, refresh = _new_tokens()
    old.consumed_at = now
    row.access_token_hash = _token_hash(access, "a")
    row.access_expires_at = min(now + timedelta(seconds=settings.auth_access_ttl_seconds),
                                _utc(row.refresh_expires_at))
    db.add(models.RefreshToken(session_id=row.id, token_hash=_token_hash(refresh, "r"),
                               expires_at=row.refresh_expires_at))
    _event(db, "session_refreshed", user_id=row.user_id, session_id=row.id)
    db.flush()
    return SessionGrant(SessionContext(row.user_id, row.tenant_id, row.id, client_type), access, refresh,
                        row.access_expires_at, _utc(row.refresh_expires_at))


def revoke_session(db: Session, context: SessionContext) -> None:
    row = db.scalar(select(models.UserSession).where(models.UserSession.id == context.session_id,
                    models.UserSession.user_id == context.user_id).with_for_update())
    if row and row.revoked_at is None:
        row.revoked_at = _now()
        _event(db, "session_logged_out", user_id=row.user_id, session_id=row.id)


def logout_refresh(db: Session, token: str, *, client_type: str) -> None:
    """Logout remains usable when the short access token has expired."""
    try:
        digest = _token_hash(token, "r")
    except IdentityError:
        return
    session_id = db.scalar(select(models.RefreshToken.session_id).where(models.RefreshToken.token_hash == digest))
    row = db.scalar(select(models.UserSession).where(models.UserSession.id == session_id).with_for_update())
    if row and row.client_type == client_type and row.revoked_at is None:
        row.revoked_at = _now()
        _event(db, "session_logged_out", user_id=row.user_id, session_id=row.id)


def change_password(db: Session, context: SessionContext, *, current_password: str,
                    new_password: str, settings: Settings) -> SessionGrant:
    user = db.get(models.User, context.user_id)
    previous = user.password_hash if user else None
    if not verify_password(current_password, previous):
        raise IdentityError("invalid_credentials", "当前密码不正确")
    replacement = hash_password(new_password)
    db.refresh(user, with_for_update=True)
    if user.password_hash != previous or user.status != "active":
        raise IdentityError("session_revoked", "登录状态已变化，请重新登录")
    user.password_hash = replacement
    user.credential_epoch += 1
    db.execute(update(models.UserSession).where(models.UserSession.user_id == user.id,
               models.UserSession.revoked_at.is_(None)).values(revoked_at=_now()))
    _event(db, "password_changed", user_id=user.id, session_id=context.session_id)
    return issue_session(db, user, client_type=context.client_type, settings=settings)


def profile(db: Session, context: SessionContext) -> dict:
    user = db.get(models.User, context.user_id)
    memberships = db.scalars(select(models.Membership).where(
        models.Membership.user_id == user.id, models.Membership.status == "active"))
    identities = db.scalars(select(models.ExternalIdentity).where(models.ExternalIdentity.user_id == user.id))
    return {"id": user.id, "login_name": user.login_name, "display_name": user.display_name,
            "personal_tenant_id": user.personal_tenant_id, "tenant_id": context.tenant_id,
            "memberships": [{"tenant_id": m.tenant_id, "role": m.role} for m in memberships],
            "linked_providers": sorted({i.provider for i in identities})}
