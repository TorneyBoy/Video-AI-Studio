"""M2 account boundary checks; run with the M2 stage gate, not during implementation."""

from datetime import datetime, timedelta, timezone
import hashlib
import importlib

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models
from app.api import deps, identity as account_api, routers
from app.config import Settings
from app.database import Base, get_db
from app.main import app
from app.services.identity import IdentityError, cookie_names, throttle


PASSWORD = "M2 disposable account long passphrase"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'accounts.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(app_environment="test", auth_mode="session", auth_public_origin="http://testserver",
                        auth_registration_enabled=True)
    for module in (deps, account_api, routers, importlib.import_module("app.main")):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(importlib.import_module("app.main"), "init_db", lambda: None)

    def override_db():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, headers={"Origin": "http://testserver"}) as client:
            yield client, factory, settings
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def register(client, name="creator_a", *, channel="web"):
    response = client.post("/api/v1/auth/register", json={"login_name": name, "display_name": "创作者",
                           "password": PASSWORD, "client": channel})
    assert response.status_code == 201, response.text
    return response


def test_web_account_has_personal_space_hashed_credentials_and_blocking_budget(accounts):
    client, factory, settings = accounts
    response = register(client)
    data = response.json()
    assert "access_token" not in data and "refresh_token" not in data
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    identity = client.get("/api/v1/session").json()
    assert identity["auth_mode"] == "session" and identity["tenant_id"] == data["user"]["personal_tenant_id"]
    raw = client.cookies.get(cookie_names(settings)[0])
    with factory() as db:
        user = db.get(models.User, data["user"]["id"])
        assert user.password_hash.startswith("scrypt$131072$8$1$") and PASSWORD not in user.password_hash
        session = db.scalar(select(models.UserSession))
        assert session.access_token_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert session.access_token_hash != raw
        member = db.scalar(select(models.Membership))
        assert member.user_id == user.id and member.tenant_id == identity["tenant_id"] and member.role == "owner"
        budget = db.scalar(select(models.TenantBudget))
        assert budget.enabled and budget.monthly_limit_micros == 0 and budget.task_limit_micros == 0
    assert client.post("/api/v1/projects", json={"name": "手机和网页共享"}).status_code == 201


def test_cookie_auth_rejects_csrf_and_client_tenant_claims(accounts):
    client, _, _ = accounts
    register(client)
    assert client.post("/api/v1/projects", json={"name": "blocked"}, headers={"Origin": "https://other.test"}).status_code == 403
    assert client.get("/api/v1/projects", headers={"X-Tenant-ID": "forged"}).status_code == 400
    assert client.get("/api/v1/projects", headers={"X-Auth-Principal": "forged"}).status_code == 400
    assert client.post("/api/v1/tenants", json={"slug": "forged", "name": "forged"}).status_code == 403
    assert client.post("/api/v1/auth/refresh", json={"client": "mini", "refresh_token": "forged"}).status_code == 400
    assert client.post("/api/v1/auth/login", headers={"Origin": "https://other.test"},
                       json={"login_name": "creator_a", "password": PASSWORD}).status_code == 403


def test_mini_session_rotation_and_replay_revoke_the_family(accounts):
    client, factory, _ = accounts
    first = register(client, channel="mini").json()
    headers = {"Authorization": "Bearer " + first["access_token"]}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    second = client.post("/api/v1/auth/refresh", json={"client": "mini", "refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"client": "mini", "refresh_token": first["refresh_token"]}).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + second.json()["access_token"]}).status_code == 401
    with factory() as db:
        assert db.scalar(select(models.UserSession)).revoked_at is not None
        assert db.scalar(select(models.AuthEvent.id).where(models.AuthEvent.event_type == "refresh_replay_revoked"))
        for token in db.scalars(select(models.RefreshToken)):
            assert token.token_hash not in {first["refresh_token"], second.json()["refresh_token"]}


def test_logout_uses_refresh_cookie_even_after_access_expiry(accounts):
    client, factory, _ = accounts
    register(client)
    with factory() as db:
        db.scalar(select(models.UserSession)).access_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/logout", json={"client": "web"}).status_code == 204
    assert client.post("/api/v1/auth/refresh", json={"client": "web"}).status_code == 401
    with factory() as db:
        assert db.scalar(select(models.UserSession)).revoked_at is not None


def test_two_personal_accounts_cannot_read_or_revoke_each_other(accounts):
    client, factory, _ = accounts
    first = register(client, channel="mini").json()
    first_headers = {"Authorization": "Bearer " + first["access_token"]}
    project = client.post("/api/v1/projects", json={"name": "A private"}, headers=first_headers).json()
    with factory() as db:
        first_session = db.scalar(select(models.UserSession.id))
    second = register(client, "creator_b", channel="mini").json()
    second_headers = {"Authorization": "Bearer " + second["access_token"]}
    assert client.get("/api/v1/projects/" + project["id"], headers=second_headers).status_code == 404
    assert client.post("/api/v1/auth/sessions/" + first_session + "/revoke", headers=second_headers).status_code == 204
    assert client.get("/api/v1/auth/me", headers=first_headers).status_code == 200
    assert client.get("/api/v1/projects", headers=second_headers).json() == []


def test_password_change_revokes_old_sessions_on_both_clients(accounts):
    client, _, _ = accounts
    original = register(client, channel="mini").json()
    with TestClient(app, headers={"Origin": "http://testserver"}) as web:
        assert web.post("/api/v1/auth/login", json={"login_name": "creator_a", "password": PASSWORD, "client": "web"}).status_code == 200
        changed = web.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD + " changed"})
        assert changed.status_code == 200 and "access_token" not in changed.json()
        assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + original["access_token"]}).status_code == 401
        assert client.post("/api/v1/auth/refresh", json={"client": "mini", "refresh_token": original["refresh_token"]}).status_code == 401
        assert web.get("/api/v1/auth/me").status_code == 200


def test_validation_does_not_echo_password_or_unexpected_token(accounts):
    client, _, _ = accounts
    response = client.post("/api/v1/auth/login", json={"login_name": "a", "password": PASSWORD, "unexpected": "sensitive-fixture"})
    assert response.status_code == 422
    assert PASSWORD not in response.text and "sensitive-fixture" not in response.text


def test_throttle_attempt_survives_later_rejection(accounts):
    _, factory, _ = accounts
    with factory() as db:
        throttle(db, scope="fixture", subject="not-persisted-raw", limit=1)
        with pytest.raises(IdentityError) as error:
            throttle(db, scope="fixture", subject="not-persisted-raw", limit=1)
        assert error.value.status == 429
    with factory() as db:
        row = db.scalar(select(models.AuthThrottle))
        assert row.attempts == 2 and "not-persisted-raw" not in row.id


def test_private_environment_does_not_gain_public_registration(accounts):
    client, factory, settings = accounts
    settings.auth_mode = "trusted_proxy"
    response = client.post("/api/v1/auth/register", json={"login_name": "creator_a", "password": PASSWORD})
    assert response.status_code == 503
    with factory() as db:
        assert db.scalar(select(models.User.id)) is None
    capabilities = client.get("/api/v1/auth/capabilities").json()
    assert not capabilities["registration"] and not capabilities["account_login"]
    assert client.get("/login").status_code == 200
