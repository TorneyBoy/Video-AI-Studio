"""One account contract: HttpOnly web cookies and native bearer sessions."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..database import get_db
from ..services.identity import (
    IdentityError, SessionContext, SessionGrant, change_password, cookie_names, logout_refresh,
    password_login, profile, register_password_account, revoke_session, rotate_refresh, throttle,
)


class PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default `input` error field may echo credentials.
                detail = [{key: value for key, value in error.items() if key in {"loc", "type", "msg"}}
                          for error in exc.errors()]
                return JSONResponse({"detail": detail}, status_code=422, headers={"Cache-Control": "no-store"})
        return private_handler


router = APIRouter(prefix="/auth", tags=["identity"], route_class=PrivateValidationRoute)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_name: str = Field(min_length=1, max_length=40)
    password: SecretStr
    client: Literal["web", "mini"] = "web"


class RegisterRequest(LoginRequest):
    display_name: str = Field(default="", max_length=80)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client: Literal["web", "mini"] = "web"
    refresh_token: SecretStr | None = None


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr
    new_password: SecretStr


def require_session_mode() -> None:
    if get_settings().auth_mode != "session":
        raise IdentityError("account_login_unavailable", "当前环境尚未启用账号登录", 503)


def check_browser_origin(request: Request) -> None:
    if request.headers.get("Origin") != get_settings().auth_public_origin:
        raise IdentityError("request_origin_forbidden", "请从工作台页面重新操作", 403)


def session_context(request: Request, db: Session = Depends(get_db)) -> SessionContext:
    from .deps import authenticate_request
    require_session_mode()
    identity = authenticate_request(request=request, db=db)
    if not identity.user_id or not identity.session_id:
        raise IdentityError("account_login_required", "请先登录账号")
    return SessionContext(identity.user_id, identity.tenant_id, identity.session_id, identity.client_type)


def _client_request(request: Request, client: str) -> None:
    require_session_mode()
    if client == "web":
        check_browser_origin(request)
    else:
        # A cross-site caller cannot opt out of CSRF checks while still using
        # the victim browser's cookies as credentials.
        access, refresh = cookie_names(get_settings())
        if request.cookies.get(access) or request.cookies.get(refresh):
            raise IdentityError("ambiguous_session_client", "请使用一致的登录方式", 400)


def _refresh_value(request: Request, payload: RefreshRequest) -> str:
    _client_request(request, payload.client)
    if payload.client == "web":
        if payload.refresh_token is not None:
            raise IdentityError("refresh_source_forbidden", "网页登录状态应通过安全Cookie恢复", 400)
        return request.cookies.get(cookie_names(get_settings())[1], "")
    return payload.refresh_token.get_secret_value() if payload.refresh_token else ""


def _source(request: Request) -> str:
    # Only ASGI's verified peer address; deployment must overwrite forwarded
    # client headers at the trusted TLS proxy, never trust a body/IP field.
    return request.client.host if request.client else "unknown"


def _grant_response(response: Response, db: Session, grant: SessionGrant) -> dict:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    result = {"user": profile(db, grant.context), "access_expires_at": grant.access_expires_at,
              "refresh_expires_at": grant.refresh_expires_at}
    if grant.context.client_type == "web":
        secure = get_settings().app_environment == "production"
        names = cookie_names(get_settings())
        now = datetime.now(timezone.utc)
        for name, token, expires in ((names[0], grant.access_token, grant.access_expires_at),
                                     (names[1], grant.refresh_token, grant.refresh_expires_at)):
            response.set_cookie(name, token, secure=secure, httponly=True, samesite="strict", path="/",
                                 max_age=max(1, int((expires - now).total_seconds())))
    else:
        result.update(token_type="Bearer", access_token=grant.access_token, refresh_token=grant.refresh_token)
    return result


@router.get("/capabilities")
def capabilities(response: Response):
    response.headers["Cache-Control"] = "no-store"
    active = get_settings().auth_mode == "session"
    return {"account_login": active, "registration": active and get_settings().auth_registration_enabled,
            "wechat_login": False, "wechat_reason": "wechat_provider_not_configured"}


@router.post("/register", status_code=201)
def register(payload: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    _client_request(request, payload.client)
    throttle(db, scope="registration", subject=_source(request), limit=5, seconds=3600)
    try:
        grant = register_password_account(db, login_name=payload.login_name,
            password=payload.password.get_secret_value(), display_name=payload.display_name,
            client_type=payload.client, settings=get_settings())
        db.commit()
    except IntegrityError:
        db.rollback()
        raise IdentityError("login_name_unavailable", "该账号已被使用", 409) from None
    return _grant_response(response, db, grant)


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    _client_request(request, payload.client)
    throttle(db, scope="login-ip", subject=_source(request), limit=60)
    throttle(db, scope="login-account", subject=payload.login_name.strip().lower(), limit=10)
    grant = password_login(db, login_name=payload.login_name, password=payload.password.get_secret_value(),
                            client_type=payload.client, settings=get_settings())
    db.commit()
    return _grant_response(response, db, grant)


@router.post("/refresh")
def refresh(payload: RefreshRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    token = _refresh_value(request, payload)
    throttle(db, scope="refresh-ip", subject=_source(request), limit=120)
    grant = rotate_refresh(db, token, client_type=payload.client, settings=get_settings())
    db.commit()
    return _grant_response(response, db, grant)


@router.post("/logout", status_code=204)
def logout(payload: RefreshRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    token = _refresh_value(request, payload)
    logout_refresh(db, token, client_type=payload.client)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    if payload.client == "web":
        for name in cookie_names(get_settings()):
            response.delete_cookie(name, path="/", secure=get_settings().app_environment == "production",
                                    httponly=True, samesite="strict")


@router.get("/me")
def me(response: Response, context: SessionContext = Depends(session_context), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return profile(db, context)


@router.post("/password")
def update_password(payload: PasswordRequest, response: Response,
                    context: SessionContext = Depends(session_context), db: Session = Depends(get_db)):
    throttle(db, scope="password-change", subject=context.user_id, limit=5)
    grant = change_password(db, context, current_password=payload.current_password.get_secret_value(),
                            new_password=payload.new_password.get_secret_value(), settings=get_settings())
    db.commit()
    return _grant_response(response, db, grant)


@router.get("/sessions")
def sessions(response: Response, context: SessionContext = Depends(session_context), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    rows = db.scalars(select(models.UserSession).where(models.UserSession.user_id == context.user_id,
                       models.UserSession.revoked_at.is_(None)).order_by(models.UserSession.created_at.desc()).limit(100))
    return [{"id": s.id, "client": s.client_type, "created_at": s.created_at,
             "refresh_expires_at": s.refresh_expires_at, "current": s.id == context.session_id} for s in rows]


@router.post("/sessions/{session_id}/revoke", status_code=204)
def revoke(session_id: str, response: Response, context: SessionContext = Depends(session_context),
           db: Session = Depends(get_db)):
    revoke_session(db, SessionContext(context.user_id, context.tenant_id, session_id, context.client_type))
    db.commit()
    response.headers["Cache-Control"] = "no-store"
