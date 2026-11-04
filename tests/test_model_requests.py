"""Multi-model request/routing contracts; no vendor or media acceptance claims."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from app.ai_production_schemas import ProductionInput
from app.services import ai_production as service
from app.services.capabilities import capability_catalog
from app.services.editing_errors import EditingError
from app.services.model_requests import ModelRequestContract, choose_model, prepare_model_request
from app.services.provider_adapters import HTTPProvider
from app.services.provider_registry import ConfiguredProviderRegistry
from app.services.providers import ModelSnapshot, ProviderRequest
from app.services.scenario_profiles import get_scenario_profile
from test_ai_production import mutate, post, production as production  # noqa: F401
from test_capability_catalog import config as catalog_config


def contract(**changes):
    value = {"id": "normalized-text-video", "version": "1", "operations": ["video_generation"],
        "input_mode": "text", "prompt_version": "text-shot-1", "prompt_template": "{scenario}\n{prompt}",
        "parameters": {"duration": {"type": "number", "minimum": 2, "maximum": 10, "required": True},
                       "resolution": {"type": "string", "enum": ["720P"], "default": "720P"},
                       "guidance": {"type": "number", "minimum": 1, "maximum": 10, "default": 5},
                       "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295}},
        "parameter_mapping": {"guidance": "cfg_scale", "seed": "random_seed"},
        "output_short_edges": {"720P": 720}}
    value.update(changes)
    return value


def body(**changes):
    return ProductionInput(operation="video_generation", prompt="原商品保持颜色和结构", **changes)


def entry(name="provider-a", **changes):
    value = {"id": f"{name}:video_generation:1", "provider": name, "model_id": f"{name}-text-video",
        "model_version": "1", "capability": "video_generation", "available": True,
        "limits": {"request_contract": contract()}, "price": {"version": "p1"}}
    value.update(changes)
    return value


def accepted_scenario():
    # Explicit offline acceptance is local to tests, never persisted to the
    # built-in draft scene profiles or deployable provider configuration.
    return get_scenario_profile("product_showcase", "1").model_copy(update={
        "validation_status": "verified", "acceptance_report": "offline-fixture-not-media-acceptance"}).snapshot()


def evidence(score, *, scenario=None, **changes):
    scenario = scenario or accepted_scenario()
    row = {"scenario_id": scenario["id"], "scenario_version": scenario["version"],
           "content_hash": scenario["content_hash"], "benchmark_version": "fixture-bench-1",
           "report_id": "offline-routing-contract-not-media-acceptance", "status": "verified", "quality_score": score}
    row.update(changes)
    return {"version": "route-1", "scenarios": [row]}


def pair(row, request=None, scenario=None):
    return row, prepare_model_request(row, request or body(), 5.0, scenario)


def install_multiple(case, monkeypatch):
    first = deepcopy(case["entry"])
    second = deepcopy(first)
    first["limits"]["request_contract"] = contract()
    second.update(id="z-other-provider:video_generation:2", provider="other-provider", model_id="other-video", model_version="2")
    second["limits"]["request_contract"] = contract(id="other-text-video", prompt_version="other-shot-2",
        prompt_template="主体与运动要求：{prompt}\n场景约束：{scenario}",
        parameter_mapping={"duration": "clip_seconds", "guidance": "conditioning_scale", "seed": "sample_seed"})
    rows = [first, second]
    monkeypatch.setattr(service, "capability_catalog", lambda: {"capabilities": deepcopy(rows)})
    return rows


def test_two_normalized_gateway_mappings_preserve_intent_without_native_api_claims():
    request = body(options={"guidance": 7.5, "seed": 4_000_000_000})
    scenario = accepted_scenario()
    first = entry()
    second = entry("provider-b", limits={"request_contract": contract(
        prompt_version="b-prompt-1", prompt_template="{prompt}\n要求：{scenario}",
        parameter_mapping={"duration": "clip_seconds", "guidance": "conditioning_scale", "seed": "sample_seed"})})
    for row, expected in ((first, {"duration": 5.0, "cfg_scale": 7.5, "random_seed": 4_000_000_000, "resolution": "720P"}),
                          (second, {"clip_seconds": 5.0, "conditioning_scale": 7.5, "sample_seed": 4_000_000_000, "resolution": "720P"})):
        frozen = prepare_model_request(row, request, 5.0, scenario)
        gateway = HTTPProvider(name=row["provider"], base_url="https://gateway.example", capabilities=(),
            model=ModelSnapshot(row["provider"], row["model_id"], row["model_version"]))
        wire = json.loads(gateway._request_body(ProviderRequest(request_id="contract-only", capability="video_generation",
            model_id=row["model_id"], model_version=row["model_version"],
            inputs={"prompt": frozen["execution_prompt"]}, parameters=frozen["provider_parameters"])))
        assert wire["parameters"] == expected
        assert request.prompt in wire["inputs"]["prompt"] and scenario["prompt_context"] in wire["inputs"]["prompt"]
        assert frozen["execution_options"]["guidance"] == 7.5


def test_final_prompt_limit_includes_template_and_scenario_before_quote(production, monkeypatch):
    from app import models
    from sqlalchemy import func, select
    case = production
    row = deepcopy(case["entry"])
    row["limits"]["request_contract"] = contract(prompt_template="镜头要求：{prompt}\n场景：{scenario}")
    scenario = get_scenario_profile("product_showcase", "1").snapshot()
    request = {"operation": "video_generation", "prompt": "商品转动",
               "scenario_id": scenario["id"], "scenario_version": scenario["version"]}
    final_prompt = f"镜头要求：{request['prompt']}\n场景：{scenario['prompt_context']}"
    row["limits"]["max_prompt_chars"] = len(final_prompt) - 1
    assert len(request["prompt"]) < row["limits"]["max_prompt_chars"]
    monkeypatch.setattr(service, "capability_catalog", lambda: {"capabilities": [deepcopy(row)]})
    denied = post(case, "/quotes", request, "expanded-prompt-over-limit")
    assert denied.status_code == 409 and "ai_capability_unavailable" in denied.text
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.PreparationQuote))) == 0
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.Job))) == 0

    # The exact expanded boundary is valid and the accepted text is frozen.
    row["limits"]["max_prompt_chars"] = len(final_prompt)
    accepted = post(case, "/quotes", request, "expanded-prompt-at-limit")
    assert accepted.status_code == 201, accepted.text
    assert mutate(lambda db: db.get(models.PreparationQuote, accepted.json()["id"]).snapshot_json["execution_prompt"]) == final_prompt


def test_duration_mapping_allows_seconds_without_changing_legacy_wan_parameters():
    request = body(options={"seed": 123, "resolution": "720P"})
    mapped = prepare_model_request(entry(limits={"request_contract": contract(
        parameter_mapping={"duration": "seconds", "seed": "random_seed"})}), request, 5.0)
    assert mapped["provider_parameters"]["seconds"] == mapped["execution_options"]["duration"] == 5.0
    assert "duration" not in mapped["provider_parameters"]
    legacy = prepare_model_request(entry(limits={"generation_resolutions": ["720P"]}), request, 5.0)
    assert legacy["execution_options"] == {"seed": 123, "resolution": "720P", "duration": 5.0}
    assert legacy["execution_prompt"] == request.prompt
    assert "provider_parameters" not in legacy


@pytest.mark.parametrize("options", [{"guidance": 11}, {"guidance": True}, {"unrecognized": 5},
                                     {"duration": 10}, {"resolution": "480P"}, {"seed": -1}])
def test_model_parameters_reject_before_execution(options):
    with pytest.raises(EditingError, match="输入或参数"):
        prepare_model_request(entry(), body(options=options), 5)


@pytest.mark.parametrize("change", [
    {"parameters": {"duration": {"type": "number"}, "api_key": {"type": "string"}}},
    {"parameter_mapping": {"guidance": "provider_job_id"}},
    {"parameter_mapping": {"guidance": "duration"}},
    {"prompt_template": "{prompt} {source_uri}"},
    {"prompt_template": "{prompt} {prompt}"},
    {"parameters": {"duration": {"type": "number"}, "guidance": {"type": "integer", "default": True}}},
    {"parameters": {"duration": {"type": "number"}, "guidance": {"type": "number", "minimum": 10, "maximum": 1}}},
])
def test_invalid_operator_contract_is_rejected(change):
    with pytest.raises(ValidationError):
        ModelRequestContract.model_validate(contract(**change))


@pytest.mark.parametrize("change", [{"scenario_id": "product_showcase"}, {"scenario_version": "1"},
                                    {"scenario_id": "../unsafe", "scenario_version": "1"},
                                    {"options": {"guidance": {"nested": 1}}}])
def test_public_input_requires_explicit_scene_version_and_scalar_options(change):
    with pytest.raises(ValidationError):
        body(**change)


def test_multiple_models_never_use_catalog_order_as_quality():
    with pytest.raises(EditingError) as caught:
        choose_model([pair(entry("z-first")), pair(entry("a-second"))], body(), None)
    assert caught.value.code == "ai_model_selection_required"


def test_explicit_model_and_unique_configured_default_are_not_quality_ranking():
    selected = entry("z-explicit")
    assert choose_model([pair(selected)], body(provider=selected["provider"]), None)[2] == {"method": "user_selected"}
    selected["limits"]["model_routing"] = {"version": "operator-policy-1", "default": True}
    result = choose_model([pair(entry("a-other")), pair(selected)], body(), None)
    assert result[0]["provider"] == "z-explicit"
    assert result[2]["method"] == "operator_default" and result[2]["quality_ranked"] is False


@pytest.mark.parametrize("invalid", [{"status": "testing"}, {"content_hash": "a" * 64}, {"scenario_version": "old"}])
def test_scene_routing_requires_matching_verified_evidence(invalid):
    scenario = accepted_scenario()
    preferred = entry("preferred")
    preferred["limits"]["model_routing"] = evidence(0.99, **invalid)
    with pytest.raises(EditingError) as caught:
        choose_model([pair(preferred), pair(entry("other"))], body(), scenario)
    assert caught.value.code == "ai_model_selection_required"


def test_verified_scene_selection_is_order_independent_and_ties_require_choice():
    scenario = accepted_scenario()
    low, high = entry("a-low"), entry("z-high")
    low["limits"]["model_routing"], high["limits"]["model_routing"] = evidence(0.3), evidence(0.8)
    result = choose_model([pair(low), pair(high)], body(), scenario)
    assert result[0]["provider"] == "z-high" and result[2]["method"] == "verified_scenario"
    assert result[2]["evidence"]["report_id"].startswith("offline-routing-contract")
    high["limits"]["model_routing"] = evidence(0.3)
    with pytest.raises(EditingError) as caught:
        choose_model([pair(high), pair(low)], body(), scenario)
    assert caught.value.code == "ai_model_selection_required"


def test_quote_selects_then_freezes_model_prompt_mapping_scene_and_cost(production, monkeypatch):
    case = production; rows = install_multiple(case, monkeypatch)
    monkeypatch.setattr(service, "_scenario", lambda body: accepted_scenario())
    rows[0]["limits"]["model_routing"] = evidence(0.4)
    rows[1]["limits"]["model_routing"] = evidence(0.9)
    request = {"operation": "video_generation", "prompt": "自己的蓝杯在柔光下转动", "scenario_id": "product_showcase",
               "scenario_version": "1", "options": {"guidance": 7}}
    response = post(case, "/quotes", request, "multi-quote")
    assert response.status_code == 201, response.text
    q = response.json()
    assert q["provider"] == "other-provider" and q["selection_reason"]["method"] == "verified_scenario"
    assert q["scenario"]["id"] == "product_showcase" and q["prompt_version"] == "other-shot-2"
    job = post(case, "/jobs", {"quote_id": q["id"]}, "multi-submit")
    assert job.status_code == 201, job.text
    from app import models
    frozen = mutate(lambda db: deepcopy(db.get(models.Job, job.json()["id"]).payload))
    snapshot = frozen["ai_production"]["snapshot"]
    assert snapshot["provider_parameters"] == {"clip_seconds": 5.0, "resolution": "720P", "conditioning_scale": 7}
    assert snapshot["execution_options"]["duration"] == 5
    assert request["prompt"] in snapshot["execution_prompt"] and "商品资料" in snapshot["execution_prompt"]
    assert frozen["provider_request"]["provider"] == "other-provider"
    assert frozen["capability_snapshot"]["price"]["version"] == q["price_version"]
    # An existing request replays its original receipt rather than rerouting.
    rows[1]["available"] = False
    same = post(case, "/jobs", {"quote_id": q["id"]}, "multi-submit")
    assert same.json()["id"] == job.json()["id"]


def test_ambiguous_api_quote_creates_no_job_or_quote(production, monkeypatch):
    case = production; install_multiple(case, monkeypatch)
    response = post(case, "/quotes", {"operation": "video_generation", "prompt": "原商品"}, "ambiguous")
    assert response.status_code == 409 and "ai_model_selection_required" in response.text
    from app import models
    from sqlalchemy import func, select
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.Job))) == 0
    assert mutate(lambda db: db.scalar(select(func.count()).select_from(models.PreparationQuote))) == 0


def test_changed_model_contract_or_scene_invalidates_new_dispatch(production, monkeypatch):
    case = production; rows = install_multiple(case, monkeypatch)
    request = {"operation": "video_generation", "prompt": "原商品", "provider": rows[0]["provider"],
               "scenario_id": "product_showcase", "scenario_version": "1"}
    q = post(case, "/quotes", request, "freeze").json()
    rows[0]["limits"]["request_contract"]["prompt_version"] = "changed"
    denied = post(case, "/jobs", {"quote_id": q["id"]}, "no-new-job")
    assert denied.status_code == 409 and "ai_quote_changed" in denied.text


def test_draft_scene_cannot_claim_verified_automatic_quality_ranking(production, monkeypatch):
    case = production; rows = install_multiple(case, monkeypatch)
    draft = get_scenario_profile("product_showcase", "1").snapshot()
    rows[0]["limits"]["model_routing"] = evidence(0.9, scenario=draft)
    response = post(case, "/quotes", {"operation": "video_generation", "prompt": "自己的商品",
        "scenario_id": "product_showcase", "scenario_version": "1"}, "draft-cannot-rank")
    assert response.status_code == 409 and "ai_model_selection_required" in response.text


def test_worker_uses_frozen_mapping_and_original_paid_task_after_rules_change(production, monkeypatch):
    from pathlib import Path
    from app import models
    from app.services.worker import _provider_request, _validate_provider_policy
    case = production; rows = install_multiple(case, monkeypatch)
    request = {"operation": "video_generation", "prompt": "原商品",
               "provider": "other-provider", "scenario_id": "product_showcase", "scenario_version": "1",
               "options": {"guidance": 8}}
    q = post(case, "/quotes", request, "frozen-prompt").json()
    response = post(case, "/jobs", {"quote_id": q["id"]}, "frozen-job")
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    rows[1]["limits"]["request_contract"]["parameter_mapping"]["guidance"] = "changed_target"

    def inspect(db):
        job = db.get(models.Job, job_id)
        provider_request = _provider_request(job, Path("unused"), db)
        assert provider_request.parameters["conditioning_scale"] == 8
        assert provider_request.parameters["clip_seconds"] == 5
        assert "changed_target" not in provider_request.parameters
        assert "商品资料" in provider_request.inputs["prompt"]
        with pytest.raises(Exception, match="已变化"):
            _validate_provider_policy(db, job, provider_request)
        db.add(models.ProviderOperation(tenant_id=case["tenant"], job_id=job.id, request_id=job.id,
            external_job_id="paid-original-do-not-resubmit", state="pending"))
        db.flush()
        _validate_provider_policy(db, job, provider_request)
    mutate(inspect)


def test_old_quote_fingerprint_and_missing_scene_fields_are_replay_compatible(production):
    from app import models
    from app.services.editing import digest
    case = production
    request = {"operation": "video_generation", "prompt": "旧请求的商品镜头"}
    response = post(case, "/quotes", request, "old-key")
    assert response.status_code == 201, response.text
    quote_id = response.json()["id"]

    def historic_shape(db):
        quote = db.get(models.PreparationQuote, quote_id)
        original = {key: value for key, value in quote.snapshot_json.items()
                    if key not in {"selection", "scenario", "prompt_version", "execution_prompt"}}
        context = {key: value for key, value in original.items() if key not in {"capability", "amount_micros", "currency"}}
        quote.snapshot_json = original
        quote.input_hash = digest({"request": quote.request_json, "snapshot": context})
        assert "scenario_id" not in quote.request_json
    mutate(historic_shape)
    same = post(case, "/quotes", {**request, "scenario_id": None, "scenario_version": None}, "old-key")
    assert same.status_code == 201 and same.json()["id"] == quote_id
    job = post(case, "/jobs", {"quote_id": quote_id}, "old-confirm")
    assert job.status_code == 201, job.text


def test_named_model_output_resolution_uses_declared_pixel_contract(production, monkeypatch):
    from test_ai_production import finish
    case = production
    row = case["entry"]
    spec = contract()
    spec["parameters"]["resolution"] = {"type": "string", "enum": ["small"], "default": "small"}
    spec["output_short_edges"] = {"small": 480}
    row["limits"].update(request_contract=spec, generation_resolutions=["small"])
    q = post(case, "/quotes", {"operation": "video_generation", "prompt": "商品"}, "pixel-contract").json()
    response = post(case, "/jobs", {"quote_id": q["id"]}, "pixel-job")
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    finish(case, job_id, width=832, height=480)
    assert case["client"].get(case["prefix"] + "/jobs/" + job_id, headers=case["headers"]).json()["can_adopt"]
    # A valid-looking provider result of the wrong actual size is still refused.
    from app import models
    def change(db):
        from sqlalchemy import select
        artifact = db.scalar(select(models.Artifact).where(models.Artifact.job_id == job_id))
        value = deepcopy(artifact.metadata_json)
        value["metrics"]["media"]["height"] = 720
        artifact.metadata_json = value
    mutate(change)
    assert not case["client"].get(case["prefix"] + "/jobs/" + job_id, headers=case["headers"]).json()["can_adopt"]


def test_adopted_asset_retains_frozen_scene_prompt_and_selection_provenance(production, monkeypatch):
    from test_ai_production import finish
    case = production; rows = install_multiple(case, monkeypatch)
    q = post(case, "/quotes", {"operation": "video_generation", "prompt": "商品细节", "provider": rows[0]["provider"],
        "scenario_id": "product_showcase", "scenario_version": "1"}, "adoption-provenance").json()
    response = post(case, "/jobs", {"quote_id": q["id"]}, "adoptable-job")
    assert response.status_code == 201, response.text
    finish(case, response.json()["id"])
    adopted = post(case, "/jobs/" + response.json()["id"] + "/adopt", {}, "adopt")
    assert adopted.status_code == 200, adopted.text
    provenance = adopted.json()["metadata"]["provenance"]
    assert provenance["scenario"]["id"] == "product_showcase"
    assert provenance["scenario"]["content_hash"] == get_scenario_profile("product_showcase", "1").content_hash
    assert provenance["prompt_version"] == "text-shot-1"
    assert provenance["request_contract"] == {"id": "normalized-text-video", "version": "1"}
    assert provenance["model_selection"]["method"] == "user_selected"


def test_disabled_template_has_two_text_contracts_without_live_or_quality_admission():
    from pathlib import Path
    value = json.loads((Path(__file__).resolve().parents[1] / "config/providers.multimodel.example.json").read_text(encoding="utf8"))
    result = capability_catalog(ConfiguredProviderRegistry.from_dict(value))
    assert len(result["capabilities"]) == 2
    for row in result["capabilities"]:
        assert not row["available"] and row["status"] == "disabled"
        assert row["input_types"] == ["text"]
        assert row["limits"]["request_contract"]["input_mode"] == "text"
        assert "model_routing" not in row["limits"]
        assert row["price"]["version"] is None


def test_public_catalog_exposes_safe_contract_and_fails_closed_on_malformed_config(monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_API_KEY", "catalog-fixture-not-a-secret")
    value = catalog_config()
    limits = value["providers"][0]["capabilities"][0]["limits"]
    limits.update(request_contract=contract(), model_routing={"version": "route-1", "default": True})
    result = capability_catalog(ConfiguredProviderRegistry.from_dict(value))["capabilities"][0]
    assert result["available"] and result["limits"]["request_contract"]["prompt_version"] == "text-shot-1"
    assert "catalog-fixture-not-a-secret" not in json.dumps(result)
    limits["request_contract"]["api_key"] = "must-not-leak"
    result = capability_catalog(ConfiguredProviderRegistry.from_dict(value))["capabilities"][0]
    assert not result["available"] and result["status"] == "invalid_model_contract"
    assert "request_contract" not in result["limits"] and "must-not-leak" not in json.dumps(result)
