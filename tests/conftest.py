from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models  # noqa: F401 - register all ORM tables
from app.database import Base, get_db
from app.main import app


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """Provide a tenant-isolated SQLite API client for boundary tests."""

    engine = create_engine(
        f"sqlite:///{tmp_path / (uuid4().hex + '.db')}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
