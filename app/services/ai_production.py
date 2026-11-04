"""Quoted AI shots and identity edits, reusing Job, budget and source revisions.

No media bytes pass through this module. A completed provider task becomes a
candidate; only a checked, durable result can be explicitly adopted as an Asset.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import re
from uuid import NAMESPACE_URL, uuid5
from urllib.parse import urlparse

from sqlalchemy import select

from .. import models
from ..ai_production_schemas import ProductionInput
from .budgets import capability_reservation, normalize_cost, preflight_job_budget
from .capabilities import capability_catalog
from .delivery_packages import utc
from .editing import digest
from .editing_errors import EditingError
from .preparation_quotes import source_hash
from .remote_uri import RemoteURIError, validate_remote_uri
from .render_quotes import affordability
from .shot_matching import media_facts, ready
from .model_requests import choose_model, prepare_model_request


CAPABILITIES = {"video_generation": "video_generation", "face_replace": "face_transform",
                "person_replace": "person_replacement"}
QUOTE_KIND = "ai_production"


def _request_json(body):
    # Preserve request fingerprints written before optional scene bindings
    # existed; missing scene fields must not turn an old replay into a new job.
    excluded = {"scenario_id", "scenario_version"} if body.scenario_id is None else set()
    return body.model_dump(mode="json", exclude=excluded)


def _scenario(body):
    if body.scenario_id is None:
        return None
    from .scenario_profiles import get_scenario_profile
    try:
        profile = get_scenario_profile(body.scenario_id, body.scenario_version)
        if body.operation not in profile.allowed_operations:
            raise ValueError("operation is outside scene scope")
        return profile.snapshot()
    except ValueError as exc:
        raise EditingError("ai_scenario_unavailable", "所选场景版本不存在或不支持此业务", 409) from exc


def _scenario_summary(snapshot):
    scenario = snapshot.get("scenario")
    return {key: scenario[key] for key in ("id", "version", "content_hash")} if scenario else None


def project_for(db, tenant, project):
    row = db.scalar(select(models.Project).where(models.Project.id == project, models.Project.tenant_id == tenant))
    if row is None:
        raise EditingError("ai_project_missing", "未找到项目", 404)
    return row


def _asset(db, tenant, project, id):
    row = db.scalar(select(models.Asset).where(models.Asset.id == id, models.Asset.tenant_id == tenant,
                                             models.Asset.project_id == project))
    if row is None:
        raise EditingError("ai_asset_missing", "请选择当前项目的素材", 404)
    if not ready(row):
        raise EditingError("ai_upload_incomplete", "请先完成素材上传核验", 409)
    usage = db.get(models.ReferenceUsage, row.id)
    if usage and not usage.allow_output:
        raise EditingError("ai_reference_output_forbidden", "仅供参考的素材尚未授权用于生成或替换输出", 409)
    try:
        validate_remote_uri(row.uri)
    except RemoteURIError as exc:
        raise EditingError("ai_remote_asset_required", "AI制作需要外部存储的素材", 422) from exc
    from .derived_rights import assert_asset_rights
    assert_asset_rights(db, row, tenant_id=tenant, project_id=project)
    return row


def _identity(db, tenant, project, body):
    profile = db.scalar(select(models.AvatarProfile).where(models.AvatarProfile.id == body.avatar_profile_id,
        models.AvatarProfile.tenant_id == tenant))
    if profile is None or profile.project_id not in {None, project}:
        raise EditingError("ai_profile_missing", "未找到本项目可用的人物档案", 404)
    now = datetime.now(timezone.utc)
    if (profile.status in {"revoked", "suspended", "deleted"} or profile.revoked_at
            or profile.expires_at and utc(profile.expires_at) <= now):
        raise EditingError("consent_profile_inactive", "人物档案已失效", 409)
    required = {"avatar", "face", body.operation}
    if not required.intersection(profile.scopes or []):
        raise EditingError("consent_profile_scope", "人物档案未授权此操作", 422)
    consent = db.scalar(select(models.Consent).where(models.Consent.id == profile.consent_id,
        models.Consent.tenant_id == tenant))
    if (consent is None or consent.project_id not in {None, project} or not consent.granted or consent.revoked_at
            or consent.expires_at and utc(consent.expires_at) <= now):
        raise EditingError("consent_inactive", "人物授权不存在、已撤回或已到期", 409)
    scopes = set(consent.scope.replace(",", " ").lower().split())
    if not required.intersection(scopes):
        raise EditingError("consent_scope_mismatch", "授权范围不包含换脸或人物替换", 422)
    if not profile.reference_asset_id:
        raise EditingError("ai_reference_required", "人物档案需要目标人物图片", 422)
    target = _asset(db, tenant, project, profile.reference_asset_id)
    if target.kind.split("/")[0] != "image":
        raise EditingError("ai_reference_image_required", "目标人物素材须为图片", 422)
    authorization = db.get(models.Artifact, str(uuid5(NAMESPACE_URL, "ai-profile-authorization:" + profile.id)))
    if authorization and authorization.metadata_json.get("source_hash") != source_hash(target):
        raise EditingError("consent_reference_changed", "授权人物图片已变化，请重新建立授权档案", 409)
    return profile, target


def _story(db, tenant, project, body):
    if not body.storyboard:
        return None
    binding = body.storyboard
    document = db.scalar(select(models.StoryboardDocument).where(models.StoryboardDocument.id == binding.document_id,
        models.StoryboardDocument.tenant_id == tenant, models.StoryboardDocument.project_id == project))
    if not document:
        raise EditingError("ai_story_missing", "未找到分镜", 404)
    if document.head_revision_id != binding.revision_id:
        raise EditingError("ai_story_changed", "分镜版本已变化，请重新选择缺镜头", 409)
    revision = db.get(models.StoryboardRevision, binding.revision_id)
    shot = next((s for s in revision.content_json.get("shots", []) if s["id"] == binding.shot_id), None)
    if not shot:
        raise EditingError("ai_shot_missing", "未找到选定镜头", 404)
    head = db.get(models.StoryboardSourceHead, document.id)
    source = db.get(models.StoryboardSourceRevision, head.revision_id) if head else None
    if source and source.story_revision_id == revision.id:
        choice = next((s for s in source.content_json.get("choices", []) if s["shot_id"] == shot["id"]), {})
        if choice.get("locked") or choice.get("mode") in {"candidate", "material"}:
            raise EditingError("ai_shot_already_sourced", "已有或锁定的镜头请先显式修改来源，再生成补镜头", 409)
    return {**binding.model_dump(), "shot": deepcopy(shot), "story_hash": digest(revision.content_json),
            "source_revision_id": head.revision_id if head else None}


def context(db, tenant, project, body):
    project_for(db, tenant, project)
    scenario = _scenario(body)
    assets = [_asset(db, tenant, project, id) for id in body.reference_asset_ids]
    identity = None
    source = None
    if body.operation != "video_generation":
        source = _asset(db, tenant, project, body.source_asset_id)
        if source.kind.split("/")[0] != "video":
            raise EditingError("ai_source_video_required", "请选择需要替换的原视频", 422)
        _, duration, _ = media_facts(source)
        if not duration or not 2 < duration < 30:
            raise EditingError("ai_source_duration", "换人原视频需有可信时长，且大于2秒、小于30秒", 422)
        profile, target = _identity(db, tenant, project, body)
        assets.extend([source, target])
        identity = {"profile_id": profile.id, "consent_id": profile.consent_id,
                    "reference_asset_id": target.id, "source_duration": duration}
    else:
        duration = body.duration_seconds
        if any(row.kind.split("/")[0] != "image" for row in assets):
            raise EditingError("ai_generation_reference_type", "当前补镜头仅接受图片参考", 422)
    story = _story(db, tenant, project, body)
    if story and duration * 1000 < story["shot"].get("duration_ms", 0):
        raise EditingError("ai_generation_too_short", "生成时长短于分镜，请调整生成设置", 422)
    snapshot = {"sources": {a.id: source_hash(a) for a in assets}, "asset_uris": {a.id: a.uri for a in assets},
                "identity": identity, "story": story}
    choices = []
    for entry in capability_catalog()["capabilities"]:
        if not entry["available"] or entry["capability"] != CAPABILITIES[body.operation]:
            continue
        if any(getattr(body, field) and getattr(body, field) != entry[field] for field in ("provider", "model_id", "model_version")):
            continue
        limits = entry.get("limits") or {}
        if body.operation == "face_replace" and "face_only" not in limits.get("features", []):
            continue  # Full character replacement must not masquerade as a face-only edit.
        bound = limits.get("max_duration_seconds")
        output_bound = limits.get("max_output_duration_seconds")
        if bound and duration > bound or output_bound and duration > output_bound:
            continue
        if limits.get("max_inputs") and len(assets) > limits["max_inputs"]:
            continue
        if "resolution" in body.options and limits.get("generation_resolutions") and body.options["resolution"] not in limits["generation_resolutions"]:
            continue
        if body.operation == "video_generation" and limits.get("allowed_durations") and duration not in limits["allowed_durations"]:
            continue
        references = len(body.reference_asset_ids) if identity is None else 1
        if references < limits.get("min_reference_inputs", 0) or references > limits.get("max_reference_inputs", 4):
            continue
        if len(body.prompt) > limits.get("max_prompt_chars", 20000) or len(body.options.get("negative_prompt", "")) > limits.get("max_negative_prompt_chars", 10000):
            continue
        if "mode" in body.options and not limits.get("request_contract") and body.options["mode"] not in limits.get("generation_modes", []):
            continue
        try:
            execution = prepare_model_request(entry, body, duration, scenario)
        except EditingError:
            # Never drop an option to force an unsuitable model to accept it.
            continue
        choices.append((entry, execution))
    if not choices:
        raise EditingError("ai_capability_unavailable", "没有覆盖所选操作和参数的已验收托管服务，请先完成服务配置及效果验收", 409)
    selected, execution, selection = choose_model(choices, body, scenario)
    snapshot.update(execution, selection=selection, scenario=scenario)
    return snapshot, selected


def quote_read(db, quote):
    snapshot = quote.snapshot_json
    selected = snapshot["capability"]
    decision = affordability(db, quote.tenant_id, quote.project_id, snapshot["currency"], snapshot["amount_micros"])
    expired = utc(quote.expires_at) <= datetime.now(timezone.utc)
    return {"id": quote.id, "summary": {"operation": quote.request_json["operation"],
        "duration_seconds": (snapshot.get("identity") or {}).get("source_duration", quote.request_json["duration_seconds"]),
        "storyboard": quote.request_json.get("storyboard")},
        "amount_micros": snapshot["amount_micros"], "currency": snapshot["currency"],
        "expires_at": utc(quote.expires_at), "can_submit": not expired and not quote.consumed_key and decision.allowed,
        "reasons": decision.audit["reasons"], "provider": selected["provider"], "model_id": selected["model_id"],
        "model_version": selected["model_version"], "price_version": selected["price"]["version"],
        "status": "consumed" if quote.consumed_key else "expired" if expired else "ready",
        "job_id": (quote.result_json or {}).get("job_id"), "bill_confirmed": False,
        "selection_reason": snapshot.get("selection", {"method": "legacy_frozen"}),
        "scenario": _scenario_summary(snapshot), "prompt_version": snapshot.get("prompt_version", "legacy-user-prompt/1")}


def create_quote(db, tenant, project, body, key):
    request = _request_json(body)
    previous = db.scalar(select(models.PreparationQuote).where(models.PreparationQuote.tenant_id == tenant,
        models.PreparationQuote.idempotency_key == key))
    if previous:
        if previous.kind != QUOTE_KIND or previous.project_id != project or previous.request_json != request:
            raise EditingError("ai_quote_conflict", "原操作标识已用于其他报价", 409)
        return quote_read(db, previous)
    snapshot, selected = context(db, tenant, project, body)
    envelope = capability_reservation(selected)
    amount, currency = normalize_cost(envelope)
    quote = models.PreparationQuote(tenant_id=tenant, project_id=project, kind=QUOTE_KIND, idempotency_key=key,
        input_hash=digest({"request": request, "snapshot": snapshot}), request_json=request,
        snapshot_json={**snapshot, "capability": selected, "amount_micros": amount, "currency": currency},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    db.add(quote)
    db.flush()
    return quote_read(db, quote)


def job_for(db, tenant, project, id, *, lock=False):
    query = select(models.Job).where(models.Job.id == id, models.Job.tenant_id == tenant, models.Job.project_id == project)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None or not (row.payload or {}).get("ai_production"):
        raise EditingError("ai_job_missing", "未找到AI制作任务", 404)
    return row


def _artifact(db, job):
    return db.scalar(select(models.Artifact).where(models.Artifact.job_id == job.id,
        models.Artifact.tenant_id == job.tenant_id).order_by(models.Artifact.created_at.desc(), models.Artifact.id).limit(1))


def _media(artifact, job):
    if not artifact:
        return None
    media = (artifact.metadata_json or {}).get("metrics", {}).get("media", {})
    if (media.get("status") != "ok" or media.get("object_uri") != artifact.uri
            or not re.fullmatch(r"[0-9a-f]{64}", str(media.get("sha256", "")))
            or type(media.get("size_bytes")) is not int or media["size_bytes"] <= 0):
        return None
    for field in ("duration_seconds", "width", "height", "fps"):
        value = media.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            return None
    try:
        validate_remote_uri(artifact.uri)
    except RemoteURIError:
        return None
    # AI supplier links expire. Adoption requires the gateway's durable tenant object.
    uri = urlparse(artifact.uri)
    if uri.scheme != "oss" or not uri.path.startswith(f"/tenants/{artifact.tenant_id}/generated/") or uri.query or uri.fragment:
        return None
    for field in ("width", "height"):
        if type(media[field]) is not int or not 16 <= media[field] <= 8192:
            return None
    if not 1 <= media["fps"] <= 120:
        return None
    production = job.payload["ai_production"]
    request = production["request"]
    expected = (production["snapshot"].get("identity") or {}).get("source_duration", request["duration_seconds"])
    if abs(media["duration_seconds"] - expected) > max(0.25, 2 / media["fps"]):
        return None
    selected = job.payload["capability_snapshot"]
    model = artifact.metadata_json.get("model") or {}
    if artifact.metadata_json.get("provider") != selected["provider"] or any(model.get(k) != selected[f] for k, f in (("model_id", "model_id"), ("version", "model_version"))):
        return None
    limits = selected.get("limits") or {}
    options = production["snapshot"].get("execution_options", {})
    contract = production["snapshot"].get("request_contract")
    if contract:
        edge = contract.get("output_short_edges", {}).get(options.get("resolution"))
        if edge is not None and min(media["width"], media["height"]) != edge:
            return None
    if request["operation"] == "person_replace":
        # Character replacement has a service mode, not a requested pixel
        # resolution. Wan's nominal 720P tier can produce e.g. 1252x704;
        # preserve the probed dimensions instead of relabelling/upscaling them.
        mode = options.get("mode", request.get("options", {}).get("mode", "wan-std"))
        mode_fps = limits.get("output_fps_by_mode") or {}
        if selected["model_id"] == "wan2.2-animate-mix":
            # Paid historical jobs froze the old catalogue without this field.
            # Their original result can still be adopted under the same mode.
            mode_fps = {"wan-std": 15, "wan-pro": 25, **mode_fps}
        if mode_fps and (mode not in mode_fps or not math.isclose(media["fps"], mode_fps[mode], rel_tol=0, abs_tol=0.01)):
            return None
        return media
    if contract:
        return media
    resolutions = (([options.get("resolution", "1080P")])
        if request["operation"] == "video_generation" else limits.get("generation_resolutions", []))
    if resolutions and min(media["width"], media["height"]) not in {int(resolution[:-1]) for resolution in resolutions}:
        return None
    return media


def job_read(db, job):
    production = job.payload["ai_production"]
    artifact = _artifact(db, job)
    authorized = True
    try:
        validate_dispatch(db, job, check_story=False)
    except EditingError:
        authorized = False
    return {"id": job.id, "status": job.status, "operation": production["request"]["operation"],
        "prompt": production["request"]["prompt"], "failure_code": job.failure_code,
        "can_adopt": job.status == "succeeded" and not job.policy_blocked and authorized and bool(_media(artifact, job)),
        "adopted_asset_id": production.get("adopted_asset_id"), "artifact_id": artifact.id if artifact else None,
        "storyboard": production["request"].get("storyboard"), "execution_generation": job.execution_generation,
        "attempt_count": job.attempt_count,
        "provider": job.payload["capability_snapshot"]["provider"],
        "model_id": job.payload["capability_snapshot"]["model_id"],
        "model_version": job.payload["capability_snapshot"]["model_version"],
        "selection_reason": production["snapshot"].get("selection", {"method": "legacy_frozen"}),
        "scenario": _scenario_summary(production["snapshot"]),
        "prompt_version": production["snapshot"].get("prompt_version", "legacy-user-prompt/1")}


def receipt(db, tenant, project, key):
    row = db.scalar(select(models.Job).where(models.Job.tenant_id == tenant, models.Job.idempotency_key == key))
    if row is None:
        return {"found": False, "result": None}
    if row.project_id != project or not (row.payload or {}).get("ai_production"):
        raise EditingError("ai_submit_conflict", "原操作标识属于其他任务", 409)
    return {"found": True, "result": job_read(db, row)}


def submit(db, tenant, project, quote_id, key):
    quote = db.scalar(select(models.PreparationQuote).where(models.PreparationQuote.id == quote_id,
        models.PreparationQuote.tenant_id == tenant, models.PreparationQuote.project_id == project,
        models.PreparationQuote.kind == QUOTE_KIND).with_for_update().execution_options(populate_existing=True))
    if not quote:
        raise EditingError("ai_quote_missing", "未找到报价", 404)
    previous = receipt(db, tenant, project, key)
    if previous["found"]:
        job = job_for(db, tenant, project, previous["result"]["id"])
        if job.payload["ai_production"]["quote_id"] != quote_id:
            raise EditingError("ai_submit_conflict", "原操作标识已用于另一报价", 409)
        return previous["result"]
    if quote.consumed_key or utc(quote.expires_at) <= datetime.now(timezone.utc):
        raise EditingError("ai_quote_expired", "报价已使用或过期，请核对原任务", 409)
    body = ProductionInput.model_validate(quote.request_json)
    snapshot, selected = context(db, tenant, project, body)
    # Unconsumed historical quotes did not contain optional execution audit
    # fields. Compare their original fingerprint shape, preserving all original
    # assets/rights/options and the exact capability/price snapshot.
    compared = {key: value for key, value in snapshot.items()
                if key in quote.snapshot_json or key not in {"selection", "scenario", "prompt_version", "execution_prompt"}}
    if quote.input_hash != digest({"request": quote.request_json, "snapshot": compared}) or selected != quote.snapshot_json["capability"]:
        raise EditingError("ai_quote_changed", "素材、授权、分镜或服务报价已变化，请重新获取报价", 409)
    identity = snapshot["identity"]
    request = {"capability": CAPABILITIES[body.operation], "prompt": snapshot.get("execution_prompt", body.prompt),
        "source_asset_id": body.source_asset_id, "reference_asset_ids": body.reference_asset_ids,
        "provider": selected["provider"], "model_id": selected["model_id"], "model_version": selected["model_version"],
        "options": snapshot["execution_options"], "operation": body.operation}
    if identity:
        request.update(avatar_profile_id=identity["profile_id"], consent_id=identity["consent_id"],
                       reference_asset_ids=[identity["reference_asset_id"]])
    job = models.Job(tenant_id=tenant, project_id=project,
        type="video.generate" if not identity else f"avatar.{body.operation}", status="queued",
        idempotency_key=key, max_attempts=3, payload={"provider_request": request, "capability_snapshot": selected,
            "ai_production": {"quote_id": quote.id, "request": quote.request_json, "snapshot": snapshot}})
    db.add(job)
    db.flush()
    decision = preflight_job_budget(db, job, selected_capability=selected)
    if decision is not None and not decision.allowed:
        db.rollback()
        raise EditingError("ai_budget_exceeded", "当前预算不足，本次没有创建生成任务", 409)
    quote.consumed_key = key
    quote.result_json = {"job_id": job.id}
    db.flush()
    return job_read(db, job)


def validate_dispatch(db, job, *, check_story=True):
    """Revalidate mutable source and identity immediately before billable dispatch."""
    production = (job.payload or {}).get("ai_production")
    if not production:
        return
    body = ProductionInput.model_validate(production["request"])
    snapshot = production["snapshot"]
    if check_story:
        selected = job.payload["capability_snapshot"]
        try:
            current = next((entry for entry in capability_catalog()["capabilities"]
                if all(entry.get(field) == selected.get(field) for field in ("capability", "provider", "model_id", "model_version"))), None)
        except (ValueError, OSError) as exc:
            raise EditingError("ai_configuration_unavailable", "AI服务配置暂不可用，本次尚未提交", 409) from exc
        if current != selected:
            raise EditingError("ai_price_changed", "服务验收、价格或能力已变化，本次尚未提交，请重新报价", 409)
        if snapshot.get("scenario") is not None and _scenario(body) != snapshot["scenario"]:
            raise EditingError("ai_scenario_changed", "场景规则版本已变化，本次尚未提交，请重新报价", 409)
    for id, expected in snapshot["sources"].items():
        asset = _asset(db, job.tenant_id, job.project_id, id)
        if source_hash(asset) != expected:
            raise EditingError("ai_source_changed", "原素材已变化，请重新报价", 409)
    if body.operation != "video_generation":
        profile, target = _identity(db, job.tenant_id, job.project_id, body)
        if profile.consent_id != snapshot["identity"]["consent_id"] or target.id != snapshot["identity"]["reference_asset_id"]:
            raise EditingError("consent_profile_changed", "人物授权或参考图已变化", 409)
    if check_story and body.storyboard and _story(db, job.tenant_id, job.project_id, body) != snapshot["story"]:
        raise EditingError("ai_story_changed", "分镜或镜头来源已变化，请重新报价", 409)


def adopt(db, tenant, project, id, key):
    job = job_for(db, tenant, project, id, lock=True)
    production = deepcopy(job.payload["ai_production"])
    # Revocation also blocks repeated retrieval via this adoption endpoint.
    validate_dispatch(db, job, check_story=False)
    if production.get("adopted_asset_id"):
        return db.get(models.Asset, production["adopted_asset_id"])
    artifact = _artifact(db, job)
    media = _media(artifact, job)
    if job.status != "succeeded" or job.policy_blocked or not media:
        raise EditingError("ai_result_not_adoptable", "结果尚未通过实际媒体核验或未保存到持久存储", 409)
    asset_id = str(uuid5(NAMESPACE_URL, "video-ai-produced-asset:" + artifact.id))
    from .analysis_contract import MediaAnalysisV2
    measured = MediaAnalysisV2.model_validate({"schema_version": "media-analysis/2", "provider": "hosted-generated-media-probe",
        "source": {"uri": artifact.uri}, "media": {"status": "ok", "duration_seconds": media["duration_seconds"],
            "size_bytes": media["size_bytes"], "format": "mp4"},
        "video": {"width": media["width"], "height": media["height"], "frame_rate": media["fps"]},
        "shots": [], "shot_analysis": {"status": "not_requested", "provider": "not_requested"},
        "transcription": {"status": "not_requested", "provider": "not_requested", "segments": []},
        "warnings": ["镜头分析与语音转写尚未运行；此处仅保存实际输出规格"]}).model_dump(mode="json")
    asset = models.Asset(id=asset_id, tenant_id=tenant, project_id=project, kind="video/mp4",
        name={"video_generation": "AI补镜头", "face_replace": "AI换脸", "person_replace": "AI人物替换"}[production["request"]["operation"]] + "-" + job.id[:8],
        uri=artifact.uri, metadata_json={"duration": media["duration_seconds"],
            "storage": {"mode": "external", "status": "ready", "size_bytes": media["size_bytes"], "sha256": media["sha256"]},
            "generated_media": deepcopy(media), "analysis": measured, "provenance": {"job_id": job.id, "artifact_id": artifact.id,
                "model": artifact.metadata_json.get("model"), "operation": production["request"]["operation"],
                "storyboard": production["request"].get("storyboard"), "input_sources": production["snapshot"]["sources"],
                "identity": production["snapshot"]["identity"], "scenario": _scenario_summary(production["snapshot"]),
                "prompt_version": production["snapshot"].get("prompt_version", "legacy-user-prompt/1"),
                "request_contract": {field: production["snapshot"]["request_contract"][field] for field in ("id", "version")}
                    if production["snapshot"].get("request_contract") else None,
                "model_selection": deepcopy(production["snapshot"].get("selection", {"method": "legacy_frozen"}))}})
    db.add(asset)
    production.update(adopted_asset_id=asset.id, adoption_key=key)
    job.payload = {**job.payload, "ai_production": production}
    db.flush()
    return asset


def profile_receipt(db, tenant, project, key):
    profile_id = str(uuid5(NAMESPACE_URL, "ai-profile:" + tenant + ":" + key))
    authorization = db.get(models.Artifact, str(uuid5(NAMESPACE_URL, "ai-profile-authorization:" + profile_id)))
    if not authorization:
        return {"found": False, "result": None}
    if authorization.tenant_id != tenant or authorization.project_id != project:
        raise EditingError("ai_profile_conflict", "原操作标识属于其他项目", 409)
    from ..schemas import AvatarProfileRead
    return {"found": True, "result": AvatarProfileRead.model_validate(db.get(models.AvatarProfile, profile_id))}


def create_profile(db, tenant, project, body, key, actor):
    project_for(db, tenant, project)
    profile_id = str(uuid5(NAMESPACE_URL, "ai-profile:" + tenant + ":" + key))
    receipt_id = str(uuid5(NAMESPACE_URL, "ai-profile-authorization:" + profile_id))
    fingerprint = digest({"project": project, "request": body.model_dump(mode="json")})
    previous = db.get(models.Artifact, receipt_id)
    if previous:
        if previous.tenant_id != tenant or previous.metadata_json.get("request_hash") != fingerprint:
            raise EditingError("ai_profile_conflict", "原操作标识已用于其他人物授权", 409)
        return db.get(models.AvatarProfile, profile_id)
    target = _asset(db, tenant, project, body.reference_asset_id)
    if target.kind.split("/")[0] != "image":
        raise EditingError("ai_reference_image_required", "请选择项目中的人物图片", 422)
    consent = models.Consent(tenant_id=tenant, project_id=project, subject_id=body.subject_id,
        scope="avatar face person_replace", granted=True, granted_at=datetime.now(timezone.utc),
        evidence_uri="urn:video-ai:authorization:" + receipt_id)
    db.add(consent)
    db.flush()
    profile = models.AvatarProfile(id=profile_id, tenant_id=tenant, project_id=project, consent_id=consent.id,
        name=body.name, reference_asset_id=target.id, provider="pending", status="consent_verified",
        scopes=["avatar", "face_replace", "person_replace"])
    db.add(profile)
    db.add(models.Artifact(id=receipt_id, tenant_id=tenant, project_id=project, kind="identity.authorization",
        uri=consent.evidence_uri, metadata_json={"request_hash": fingerprint, "source_hash": source_hash(target),
            "profile_id": profile.id, "consent_id": consent.id, "actor_id": actor,
            "scope": consent.scope, "granted": True}))
    db.flush()
    return profile
