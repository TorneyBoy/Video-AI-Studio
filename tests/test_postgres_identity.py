"""M2 refresh races on the same disposable PostgreSQL harness as M0/M1."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app import models
from app.config import Settings
from app.services.identity import IdentityError, register_password_account, resolve_access, rotate_refresh
from test_postgres_completion import postgres_database as _postgres_database

postgres_database = _postgres_database


def test_one_refresh_token_cannot_issue_two_live_sessions(postgres_database):
    factory, _ = postgres_database
    settings = Settings(auth_mode="session", auth_public_origin="http://testserver", auth_registration_enabled=True)
    with factory() as db:
        grant = register_password_account(db, login_name="race_creator", password="Disposable PostgreSQL identity passphrase",
                                          display_name="Race", client_type="mini", settings=settings)
        db.commit()
    barrier = Barrier(2)

    def refresh():
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                current = rotate_refresh(db, grant.refresh_token, client_type="mini", settings=settings)
                db.commit()
                return current
            except IdentityError as exc:
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(refresh) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    successful = [result for result in results if not isinstance(result, str)]
    assert len(successful) == 1 and "session_revoked" in results
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(models.UserSession)) == 1
        assert db.scalar(select(func.count()).select_from(models.RefreshToken)) == 2
        with pytest.raises(IdentityError, match="登录已失效"):
            resolve_access(db, successful[0].access_token)
