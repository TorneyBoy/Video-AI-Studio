"""Offline HTTP-to-adoption contracts, not generated-video quality acceptance.

The real registry, API, worker, HTTP serializer and gateway run together.
Only the cloud transport/OSS/ICE boundaries use explicit synthetic fixtures.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.database import get_db
from app.main import app
from app.services.capabilities import capability_catalog
from app.services.provider_registry import ConfiguredProviderRegistry
from app.services.release_bundle import admitted
from app.services.shot_matching import media_facts
from app.services.worker import process_queued_provider_jobs
from media_gateway.wan_jobs import WanOperation
from test_wan_gateway import DATA, gateway as gateway  # Explicit pytest fixture re-export.


CONFIG = Path(__file__).resolve().parents[1] / "config/providers.aliyun.ai.example.json"


@contextmanager
def control_db():
    iterator = app.dependency_overrides[get_db]()
    db = next(iterator)
    try:
        yield db
        db.commit()
    finally:
        iterator.close()


@pytest.fixture
def registry(monkeypatch):
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    # Synthetic acceptance is local to these tests, never persisted to config.
    for entry in value["providers"]:
        entry["enabled"] = True
        entry["poll_interval_seconds"] = 0
        entry["max_poll_attempts"] = 3
        entry["model"]["license_snapshot"].update(commercial_allowed=True, status="approved")
        for capability in entry["capabilities"]:
            capability["commercial_allowed"] = True
            capability["acceptance"].update(status="verified", report_ref="urn:offline-contract:not-media-quality-acceptance")
    monkeypatch.setenv("MEDIA_GATEWAY_AUTHORIZATION", "Bearer gateway-fixture")
    monkeypatch.setenv("PROVIDER_REGISTRY_JSON", json.dumps(value))
    monkeypatch.setenv("PROVIDER_MODE", "api_only")
    monkeypatch.setenv("ASSET_STORAGE_MODE", "external")
    get_settings.cache_clear()
    yield ConfiguredProviderRegistry.from_dict(value)
    get_settings.cache_clear()


@pytest.fixture
def pipeline(client, gateway, registry):
    calls, faults = [], {"next_poll_timeout": False}

    def bridge(request, timeout):
        assert urlparse(request.full_url).hostname == "video-ai-gateway.internal"
        calls.append((request.method, urlparse(request.full_url).path,
                      {key.lower(): value for key, value in request.header_items()}, request.data))
        if request.method == "GET" and faults["next_poll_timeout"]:
            faults["next_poll_timeout"] = False
            raise TimeoutError("synthetic connection loss to the existing gateway operation")
        response = gateway.client.request(request.method, urlparse(request.full_url).path,
                                          headers=dict(request.header_items()), content=request.data)
        return response.status_code, response.content

    for adapter in registry.providers:
        adapter._transport = bridge
    tenant = client.post("/api/v1/tenants", json={"slug": "wan-pipeline", "name": "Offline pipeline"}).json()["id"]
    headers = {"X-Tenant-ID": tenant}
    project = client.post("/api/v1/projects", headers=headers, json={"name": "Offline AI"}).json()["id"]
    with control_db() as db:
        db.add(models.TenantBudget(tenant_id=tenant, currency="CNY", monthly_limit_micros=100_000_000,
                                  task_limit_micros=50_000_000))
        image = models.Asset(tenant_id=tenant, project_id=project, name="Own authorized reference", kind="image/png",
            uri=f"oss://fixture-bucket/tenants/{tenant}/uploads/reference.png", metadata_json={"storage": {"status": "ready"}})
        video = models.Asset(tenant_id=tenant, project_id=project, name="Own source", kind="video/mp4",
            uri=f"oss://fixture-bucket/tenants/{tenant}/uploads/source.mp4",
            metadata_json={"duration": 5, "width": 720, "height": 1280, "fps": 30, "storage": {"status": "ready"}})
        db.add_all([image, video]); db.flush()
        image_id, video_id = image.id, video.id
    return {"client": client, "gateway": gateway, "registry": registry, "calls": calls, "faults": faults, "tenant": tenant,
            "project": project, "headers": headers, "image": image_id, "video": video_id,
            "prefix": f"/api/v1/projects/{project}/ai-production"}


def post(case, path, body, key):
    return case["client"].post(case["prefix"] + path,
        headers={**case["headers"], "Idempotency-Key": key}, json=body)


def submit(case, provider="aliyun-wan-t2v"):
    entry = next(row for row in case["registry"].registrations if row.name == provider)
    body = {"operation": "video_generation", "prompt": "自己的蓝色商品在固定机位下缓慢转动",
            "provider": provider, "model_id": entry.model.model_id, "model_version": entry.model.version,
            "duration_seconds": 5, "options": {"resolution": "1080P", "seed": 7, "negative_prompt": "不要文字"}}
    if provider == "aliyun-wan-i2v":
        body["reference_asset_ids"] = [case["image"]]
    elif provider == "aliyun-wan-person":
        case["gateway"].probe.width, case["gateway"].probe.height = 1252, 704
        case["gateway"].probe.fps, case["gateway"].probe.duration = 25, 5.2
        profile = post(case, "/profiles", {"name": "自己的形象", "subject_id": "授权的离线测试主体",
                       "reference_asset_id": case["image"], "granted": True}, "profile")
        assert profile.status_code == 201, profile.text
        body.update(operation="person_replace", prompt="", options={"mode": "wan-pro"},
                    source_asset_id=case["video"], avatar_profile_id=profile.json()["id"])
    quote = post(case, "/quotes", body, "quote")
    assert quote.status_code == 201, quote.text
    assert not case["gateway"].transport.calls
    response = post(case, "/jobs", {"quote_id": quote.json()["id"]}, "confirm")
    assert response.status_code == 201, response.text
    replay = post(case, "/jobs", {"quote_id": quote.json()["id"]}, "confirm")
    assert replay.json()["id"] == response.json()["id"] and not case["gateway"].transport.calls
    return response.json()["id"], quote.json()


def run(case):
    with control_db() as db:
        return process_queued_provider_jobs(db, router=case["registry"].router())


def allow_original_retry(job_id):
    with control_db() as db:
        db.get(models.Job, job_id).next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        for workflow in db.scalars(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job_id)):
            workflow.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)


@pytest.mark.parametrize("provider", ["aliyun-wan-t2v", "aliyun-wan-i2v", "aliyun-wan-person"])
def test_registered_model_round_trip_produces_one_durable_candidate_then_adopts(pipeline, provider):
    case = pipeline; job_id, quote = submit(case, provider)
    assert quote["amount_micros"] == (27_000_000 if provider.endswith("person") else 3_500_000)
    assert run(case)[0]["status"] == "succeeded"
    with control_db() as db:
        artifact = db.scalar(select(models.Artifact).where(models.Artifact.job_id == job_id))
        assert artifact.metadata_json["provider"] == provider
        media = artifact.metadata_json["metrics"]["media"]
        assert media["status"] == "ok" and media["sha256"] == hashlib.sha256(DATA).hexdigest()
        assert media["size_bytes"] == len(DATA) and media["object_uri"] == artifact.uri
        ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job_id))
        assert ledger.status == "pending_reconciliation" and ledger.amount_micros == quote["amount_micros"]
        assert ledger.cost_json["status"] == "unknown" and "amount" not in ledger.cost_json
        operation_id = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job_id)).external_job_id
    with case["gateway"].sessions() as db:
        assert db.get(WanOperation, operation_id).specification["provider"] == provider
    sent = next(call for call in case["calls"] if call[0] == "POST")
    assert sent[2]["x-wan-provider"] == provider
    assert sent[2]["idempotency-key"] == sent[2]["x-request-id"] == job_id
    normalized = json.loads(sent[3])
    assert normalized["request_id"] == job_id and normalized["tenant_id"] == case["tenant"]
    assert not {"output_dir", "output_path"} & set(normalized["parameters"])
    upstream = json.loads(case["gateway"].transport.calls[0][3])
    assert upstream["model"] == normalized["model_id"]
    if provider.endswith("person"):
        assert set(upstream["input"]) == {"image_url", "video_url"}
        assert upstream["parameters"] == {"mode": "wan-pro", "check_image": True}
    else:
        assert upstream["input"]["negative_prompt"] == "不要文字"
    assert [call[0] for call in case["gateway"].transport.calls].count("POST") == 1
    view = case["client"].get(case["prefix"] + "/jobs/" + job_id, headers=case["headers"])
    assert view.json()["can_adopt"], view.text
    adopted = post(case, "/jobs/" + job_id + "/adopt", {}, "adopt")
    assert adopted.status_code == 200, adopted.text
    repeated = post(case, "/jobs/" + job_id + "/adopt", {}, "adopt")
    assert repeated.json()["id"] == adopted.json()["id"]
    assert adopted.json()["metadata"]["storage"]["sha256"] == media["sha256"]
    assert adopted.json()["metadata"]["analysis"]["schema_version"] == "media-analysis/2"
    with control_db() as db:
        assert db.scalar(select(func.count()).select_from(models.Asset)) == 3
        analysis, duration, orientation = media_facts(db.get(models.Asset, adopted.json()["id"]))
        assert analysis is not None
        if provider.endswith("person"):
            assert duration == 5.2 and orientation == "landscape"
            assert (analysis.video.width, analysis.video.height, analysis.video.frame_rate) == (1252, 704, 25)
        else:
            assert duration == 5 and orientation == "portrait"
        assert analysis.shots == [] and analysis.shot_analysis.status == "not_requested"
    assert run(case) == []


def test_failed_media_probe_keeps_charge_hold_without_publishing_or_adopting(pipeline):
    case = pipeline; job_id, quote = submit(case); case["gateway"].probe.fps = 0
    assert run(case)[0]["status"] == "failed"
    with control_db() as db:
        assert db.scalar(select(func.count()).select_from(models.Artifact).where(models.Artifact.job_id == job_id)) == 0
        ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job_id))
        assert ledger.status == "pending_reconciliation" and ledger.amount_micros == quote["amount_micros"]
        assert db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job_id)).external_job_id
    assert post(case, "/jobs/" + job_id + "/adopt", {}, "adopt").status_code == 409


def test_unknown_paid_submission_retries_only_original_gateway_operation(pipeline):
    case = pipeline; job_id, _ = submit(case); case["gateway"].transport.submit_failure = "timeout"
    assert run(case)[0]["status"] == "retry_scheduled"
    with control_db() as db:
        operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job_id))
        original_id = operation.external_job_id
    assert original_id
    allow_original_retry(job_id)
    calls_before = len(case["calls"])
    assert run(case)[0]["status"] == "retry_scheduled"
    assert all(call[0] == "GET" and call[1].endswith(original_id) for call in case["calls"][calls_before:])
    assert [call[0] for call in case["gateway"].transport.calls].count("POST") == 1
    with control_db() as db:
        assert db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job_id)).status == "reserved"
        assert db.scalar(select(func.count()).select_from(models.Artifact).where(models.Artifact.job_id == job_id)) == 0


def test_gateway_poll_network_failure_resumes_accepted_operation_without_another_post(pipeline):
    case = pipeline; job_id, _ = submit(case); case["faults"]["next_poll_timeout"] = True
    assert run(case)[0]["status"] == "retry_scheduled"
    assert not case["gateway"].transport.calls
    with control_db() as db:
        original_id = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job_id)).external_job_id
    assert original_id
    allow_original_retry(job_id)
    calls_before = len(case["calls"])
    assert run(case)[0]["status"] == "succeeded"
    assert all(call[0] == "GET" and call[1].endswith(original_id) for call in case["calls"][calls_before:])
    assert [call[0] for call in case["calls"]].count("POST") == 1
    assert [call[0] for call in case["gateway"].transport.calls].count("POST") == 1
    assert post(case, "/jobs/" + job_id + "/adopt", {}, "adopt").status_code == 200


def test_ai_example_is_release_admitted_but_disabled_unverified_and_secret_free(monkeypatch):
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    registry = ConfiguredProviderRegistry.from_dict(value)
    catalogue = capability_catalog(registry)["capabilities"]
    assert len(catalogue) == 3 and all(not row["available"] and row["status"] == "disabled" for row in catalogue)
    assert admitted("config/" + CONFIG.name)
    for entry in value["providers"]:
        assert entry["name"] == entry["headers"]["X-Wan-Provider"] == entry["model"]["provider"]
        assert not entry["enabled"] and entry["capabilities"][0]["acceptance"]["status"] == "unverified"
        assert "Authorization" not in entry["headers"]
    monkeypatch.setenv("MEDIA_GATEWAY_AUTHORIZATION", "Bearer gateway-fixture")
    value = deepcopy(value)
    for entry in value["providers"]:
        entry["enabled"] = True
        entry["capabilities"][0]["commercial_allowed"] = True
    assert all(not row["available"] for row in capability_catalog(ConfiguredProviderRegistry.from_dict(value))["capabilities"])


def test_wan_alias_is_bound_to_model_and_cannot_change_on_replay(gateway):
    from test_wan_gateway import headers, payload

    value = payload(); selected = {**headers(), "X-Wan-Provider": "aliyun-wan-t2v"}
    created = gateway.client.post("/v1/wan", json=value, headers=selected)
    assert created.status_code == 200
    for alias in ("aliyun-wan-i2v", "unrecognized"):
        wrong = gateway.client.post("/v1/wan", json=value, headers={**headers(), "X-Wan-Provider": alias})
        assert wrong.status_code == 422
    replay = gateway.client.post("/v1/wan", json=value, headers=headers())
    assert replay.status_code == 409
    operation_id = created.json()["provider_job_id"]
    for _ in range(2):
        result = gateway.client.get("/v1/wan/jobs/" + operation_id, headers={**headers(), "X-Wan-Provider": "ignored-after-freeze"})
        assert result.status_code == 200
    assert result.json()["model"]["provider"] == "aliyun-wan-t2v"
