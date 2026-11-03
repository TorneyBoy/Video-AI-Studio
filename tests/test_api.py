from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app
from app import models  # noqa: F401


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    engine = create_engine(f"sqlite:///{tmp_path / (uuid4().hex + '.db')}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()


def test_health_and_tenant_scoped_project(client: TestClient):
    assert client.get('/healthz').json()['status'] == 'ok'
    ready = client.get('/readyz')
    assert ready.status_code == 200
    assert ready.json()['checks'] == {'database': 'ok', 'storage': 'ok'}
    tenant = client.post('/api/v1/tenants', json={'slug': 'studio', 'name': 'Studio'}).json()
    headers = {'X-Tenant-ID': tenant['id']}
    project = client.post('/api/v1/projects', headers=headers, json={'name': 'Launch'}).json()
    assert project['tenant_id'] == tenant['id']
    assert client.get('/api/v1/projects', headers=headers).json()[0]['id'] == project['id']
    assert client.get('/api/v1/projects').status_code == 400


def test_tenant_isolation(client: TestClient):
    first = client.post('/api/v1/tenants', json={'slug': 'one', 'name': 'One'}).json()
    second = client.post('/api/v1/tenants', json={'slug': 'two', 'name': 'Two'}).json()
    one_headers = {'X-Tenant-ID': first['id']}
    two_headers = {'X-Tenant-ID': second['id']}
    project = client.post('/api/v1/projects', headers=one_headers, json={'name': 'Private'}).json()
    assert client.get(f"/api/v1/projects/{project['id']}", headers=two_headers).status_code == 404
    assert client.post('/api/v1/assets', headers=two_headers, json={'name': 'bad', 'kind': 'video', 'uri': 'file://x', 'project_id': project['id']}).status_code == 404


def test_path_tenant_endpoints_require_matching_development_identity(client: TestClient):
    first = client.post('/api/v1/tenants', json={'slug': 'path-one', 'name': 'One'}).json()
    second = client.post('/api/v1/tenants', json={'slug': 'path-two', 'name': 'Two'}).json()
    first_headers = {'X-Tenant-ID': first['id']}

    # URL tenant IDs are still accepted in local development, but they must
    # agree with the authenticated X-Tenant-ID rather than acting as an
    # authorization switch for another tenant.
    assert client.get(f"/api/v1/tenants/{second['id']}", headers=first_headers).status_code == 403
    assert client.get(f"/api/v1/tenants/{second['id']}/budget", headers=first_headers).status_code == 403
    assert client.put(
        f"/api/v1/tenants/{second['id']}/budget",
        headers=first_headers,
        json={"currency": "USD", "monthly_limit_micros": 1000000},
    ).status_code == 403
    assert client.get(f"/api/v1/tenants/{second['id']}/budget").status_code == 400
