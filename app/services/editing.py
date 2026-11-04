"""Append-only plan/timeline authoring. No provider calls or paid generation.

Lock order: originating Job -> new document/head during initialization;
existing head only during edits. Never acquire a Job/budget lock from an edit.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session
from .shot_requirements import assess, template_requirement

from timeline_engine import AssetRef, TemplateDefinition, Timeline, validate_timeline
from timeline_engine.timing import FrameClock

from .. import editing_schemas as schemas, models
from ..config import get_settings
from .analysis_contract import require_planning_analysis, validate_hosted_analysis
from .auto_edit import AutoEditPlanner, compile_auto_edit_timeline, shot_features_from_assets
from .providers import ProviderExecutionError
from .editing_errors import EditingError
from .clip_authoring import CLIP_ACTIONS, apply_clip_action, editor_view, resize_precise_sequence
from .subtitle_authoring import (SUBTITLE_ACTIONS, capture_transcript, delete_subtitle, import_transcript,
                                save_subtitle, sync_subtitle_anchors)
from .track_authoring import TRACK_ACTIONS, apply_track_action
from .render_features import render_support


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def document_for_tenant(db: Session, tenant_id: str, document_id: str) -> models.EditDocument:
    row = db.scalar(select(models.EditDocument).where(models.EditDocument.id == document_id,
                                                      models.EditDocument.tenant_id == tenant_id))
    if row is None:
        raise EditingError("edit_document_not_found", "未找到编辑项目", 404)
    return row


def revision_for_document(db: Session, document: models.EditDocument, revision_id: str) -> models.PlanRevision:
    row = db.scalar(select(models.PlanRevision).where(models.PlanRevision.id == revision_id,
        models.PlanRevision.document_id == document.id, models.PlanRevision.tenant_id == document.tenant_id))
    if row is None:
        raise EditingError("edit_revision_not_found", "未找到该项目的编辑版本", 404)
    return row


def timeline_for_revision(db: Session, revision: models.PlanRevision) -> models.TimelineRevision:
    row = db.get(models.TimelineRevision, revision.id)
    if row is None or row.document_id != revision.document_id or row.tenant_id != revision.tenant_id:
        raise EditingError("edit_snapshot_missing", "时间线版本尚未完整保存")
    if row.content_hash != digest(row.timeline_json) or revision.content_hash != digest({
            "template": revision.template_json, "sources": revision.sources_json, "plan": revision.plan_json}):
        raise EditingError("edit_snapshot_changed", "版本校验未通过，请保留原记录并联系管理员")
    return row


def revision_read(db: Session, document: models.EditDocument, revision: models.PlanRevision) -> dict:
    timeline = timeline_for_revision(db, revision)
    head = db.get(models.EditHead, document.id)
    if head is None:
        raise EditingError("edit_snapshot_missing", "编辑项目尚未完整保存")
    return {"document_id": document.id, "project_id": document.project_id, "origin_job_id": document.origin_job_id,
        "name": document.name, "revision_id": revision.id, "head_revision_id": head.revision_id,
        "parent_revision_id": revision.parent_revision_id, "undo_revision_id": revision.operation_json.get("undo_revision_id"),
        "number": revision.number, "plan_hash": revision.content_hash, "timeline_hash": timeline.content_hash,
        "timeline_id": timeline.timeline_id, "source_asset_ids": [row["id"] for row in revision.sources_json],
        "template": deepcopy(revision.template_json),
        "plan": deepcopy(revision.plan_json), "operation": deepcopy(revision.operation_json),
        "editor": {**editor_view(timeline.timeline_json), "advanced_support": {
            key: value for key, value in render_support(timeline.timeline_json).items() if key != "selected"},
            "sources": [{"id": source["id"], "name": source["name"], "kind": source["kind"],
                         "has_audio": source["kind"].split("/", 1)[0] == "audio" or (source["kind"].split("/", 1)[0] == "video" and bool((source["metadata"].get("analysis") or {}).get("audio")))}
                        for source in revision.sources_json]}, "created_at": revision.created_at}


def _capture_sources(assets: list[models.Asset]) -> list[dict]:
    sources = []
    shot_count = 0
    for asset in sorted(assets, key=lambda item: item.id):
        metadata = asset.metadata_json or {}
        storage = metadata.get("storage") or {}
        analysis = deepcopy(metadata.get("analysis") or {})
        shot_count += max(1, len(analysis.get("shots") or []))
        if shot_count > 20000:
            raise EditingError("edit_candidates_limit", "候选镜头过多，请选择更少的素材创建计划", 422)
        if get_settings().app_environment == "production":
            if storage.get("status") != "ready" or not storage.get("sha256"):
                raise EditingError("edit_source_unverified", "请先完成素材上传核验", 422)
            if asset.kind.startswith(("video", "audio")):
                try:
                    require_planning_analysis(analysis, source_uri=asset.uri, kind=asset.kind)
                except ProviderExecutionError as exc:
                    raise EditingError("edit_analysis_required", "请先完成视频镜头分析", 422) from exc
        transcript = capture_transcript(analysis)
        # Keep media/shot facts separate from the immutable ASR original.
        if isinstance(analysis.get("transcription"), dict) and not analysis["transcription"].get("words"):
            analysis["transcription"]["segments"] = []
        sources.append({"id": asset.id, "name": asset.name, "kind": asset.kind, "uri": asset.uri, "transcript": transcript,
            "metadata": {"analysis": analysis, "duration": metadata.get("duration"), "user_tags": deepcopy(metadata.get("user_tags") or []),
                "storage": {key: storage.get(key) for key in ("mode", "status", "size_bytes", "sha256")}}})
    if len(json.dumps(sources, ensure_ascii=False).encode()) > 16 * 1024 * 1024:
        raise EditingError("edit_snapshot_limit", "素材分析快照过大，请减少单次计划的素材数量", 422)
    return sources


def _source_assets(sources: list[dict]) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=row["id"], name=row["name"], kind=row["kind"], uri=row["uri"],
                            metadata_json=deepcopy(row["metadata"])) for row in sources]


def _candidate_map(sources: list[dict], fps: float = 30, *, scenario=None) -> dict[str, dict]:
    source_map = {row["id"]: row for row in sources}
    result = {}
    clock = FrameClock(fps)
    if scenario:
        from .scenario_profiles import resolve_scenario_binding
        scenario = resolve_scenario_binding(scenario) if isinstance(scenario, dict) else scenario
    for feature in shot_features_from_assets(_source_assets(sources), scenario=scenario):
        if scenario:
            from .auto_edit import scenario_feature_eligibility
            if not scenario_feature_eligibility(scenario, feature)["eligible"]:
                continue
        raw = feature.metadata or {}; start = float(raw.get("source_start", raw.get("start", 0)) or 0)
        end = float(raw.get("source_end", raw.get("end", start + feature.duration)) or start + feature.duration)
        try:
            first, last = clock.window(start, end)
        except ValueError:
            continue
        start, end = clock.seconds(first), clock.seconds(last)
        score, rationale = AutoEditPlanner(weights=scenario.selection_weights if scenario else None).score(feature)
        source = source_map[feature.asset_id.split("#", 1)[0]]
        result[feature.asset_id] = {"candidate_id": feature.asset_id, "source_asset_id": source["id"],
            "name": source["name"], "kind": source["kind"], "source_start": start, "source_end": end,
            "duration": end - start, "orientation": feature.orientation, "metadata": raw,
            "score": score, "rationale": rationale,
            "selection_basis": "measured_features" if rationale else "source_order"}
    return result


def _eligible(candidate: dict, slot, template: TemplateDefinition, duration: float = 0) -> bool:
    policy = (template.metadata.get("slot_policies") or {}).get(slot.name) or {}
    requirement = replace(template_requirement(slot, policy), duration_seconds=max(0, duration))
    result = assess(requirement, kind=candidate["kind"].split("/", 1)[0], duration=candidate["duration"],
        orientation=candidate["orientation"], metadata=candidate["metadata"],
        tags=candidate["metadata"].get("_user_tags") or (), image_hold=False)
    return result["verdict"] == "supported"


def candidates_read(revision: models.PlanRevision, *, fps: float = 30) -> list[dict]:
    template = TemplateDefinition.model_validate(revision.template_json)
    rows = []
    from .scenario_profiles import template_scenario
    for candidate in _candidate_map(revision.sources_json, fps, scenario=template_scenario(template)).values():
        rows.append({key: deepcopy(value) for key, value in candidate.items() if key != "metadata"} | {
            "eligible_slots": [slot.name for slot in template.slots if _eligible(candidate, slot, template)]})
    return rows


def _plan_rows(document_id: str, timeline: Timeline, plan: dict, sources: list[dict]) -> list[dict]:
    candidates = _candidate_map(sources, timeline.fps, scenario=timeline.metadata.get("scenario"))
    prior = {(row.get("slot"), row.get("asset_id")): row for row in plan.get("selected", [])}
    by_clip = {row.get("clip_id"): row for row in plan.get("selected", []) if row.get("clip_id")}
    rows = []
    for track in timeline.tracks:
        if track.type.value != "video":
            continue
        for clip in track.clips:
            shot_id = str(clip.metadata.get("source_shot_id") or f"{clip.asset_id}#shot-0")
            slot = str(clip.metadata.get("slot") or "slot")
            original = by_clip.get(clip.id) or prior.get((slot, shot_id)) or {}
            candidate = candidates.get(shot_id) or {}
            start = clip.source_start; end = clip.source_end or start + clip.range.duration
            rows.append({"selection_id": str(uuid5(NAMESPACE_URL, f"{document_id}:{clip.id}")),
                "clip_id": clip.id, "slot": slot, "asset_id": shot_id, "source_asset_id": clip.asset_id,
                "name": next((source["name"] for source in sources if source["id"] == clip.asset_id), str(clip.asset_id)),
                "source_start": start, "source_end": end,
                "window_start": candidate.get("source_start", start), "window_end": candidate.get("source_end", end),
                "timeline_start": clip.range.start, "timeline_end": clip.range.end, "output_duration": clip.range.duration,
                "score": original.get("score", clip.metadata.get("score", 0)),
                "rationale": deepcopy(original.get("rationale", clip.metadata.get("rationale", {}))),
                "selection_basis": original.get("selection_basis", "measured_features" if clip.metadata.get("rationale") else "source_order"),
                "locked": bool(by_clip.get(clip.id, {}).get("locked", False))})
    if not rows:
        raise EditingError("edit_plan_empty", "计划没有可编辑镜头", 422)
    return rows


def _assert_sources_current(db: Session, document: models.EditDocument, sources: list[dict], used: set[str]) -> None:
    frozen = {row["id"]: row for row in sources}
    assets = {row.id: row for row in db.scalars(select(models.Asset).where(models.Asset.id.in_(used),
        models.Asset.tenant_id == document.tenant_id, models.Asset.project_id == document.project_id))} if used else {}
    for asset_id in used:
        original = frozen.get(asset_id); current = assets.get(asset_id)
        if original is None or current is None:
            raise EditingError("edit_source_unavailable", "版本引用的素材已不可用", 409)
        metadata = current.metadata_json or {}; storage = metadata.get("storage") or {}
        if current.uri != original["uri"] or current.kind != original["kind"] or storage.get("sha256") != original["metadata"]["storage"].get("sha256"):
            raise EditingError("edit_source_changed", "素材对象与版本快照不一致，不能继续使用该版本", 409)
        if storage.get("mode") == "external_presigned" and storage.get("status") != "ready":
            raise EditingError("edit_source_unverified", "版本素材尚未通过上传核验", 409)


def assert_render_sources(db: Session, document: models.EditDocument, revision: models.PlanRevision) -> models.TimelineRevision:
    timeline = timeline_for_revision(db, revision)
    used = {asset["id"] for asset in timeline.timeline_json.get("assets", [])}
    _assert_sources_current(db, document, revision.sources_json, used)
    from .derived_rights import assert_timeline_rights
    assert_timeline_rights(db, tenant_id=document.tenant_id, project_id=document.project_id, timeline=timeline.timeline_json)
    _assert_scenario_sources(Timeline.model_validate(timeline.timeline_json), revision.sources_json,
                             TemplateDefinition.model_validate(revision.template_json))
    return timeline


def _assert_scenario_sources(timeline: Timeline, sources: list[dict], template: TemplateDefinition) -> None:
    from .auto_edit import assert_scenario_timeline_sources
    try:
        assert_scenario_timeline_sources(timeline, _source_assets(sources), template)
    except ValueError as exc:
        raise EditingError("edit_scenario_boundary", "修改未满足场景的来源、完整讲话或词级边界要求，请保留完整单元后重试", 422) from exc


def validate_render_revision(db: Session, job: models.Job, manifest, *, check_sources: bool = True) -> None:
    payload = job.payload or {}
    revision_id = payload.get("timeline_revision_id")
    if not revision_id:
        return  # Legacy M1 renders keep their original manifest-only contract.
    document = document_for_tenant(db, job.tenant_id, str(payload.get("edit_document_id") or ""))
    revision = revision_for_document(db, document, str(revision_id))
    frozen = assert_render_sources(db, document, revision) if check_sources else timeline_for_revision(db, revision)
    if (payload.get("plan_revision_id") != revision.id or payload.get("plan_hash") != revision.content_hash
            or payload.get("timeline_hash") != frozen.content_hash or job.project_id != document.project_id
            or manifest.timeline_id != frozen.timeline_id or manifest.source.get("timeline") != frozen.timeline_json
            or manifest.template_id != revision.template_json["id"] or manifest.template_version != revision.template_json["version"]):
        raise EditingError("render_revision_changed", "渲染请求与保存的编辑版本不一致，已停止执行")


def _insert_revision(db: Session, document: models.EditDocument, *, number: int, parent: str | None,
                     key: str, request_hash: str, template: dict, sources: list[dict], plan: dict,
                     timeline: Timeline, operation: dict, existing_timeline: models.Timeline | None = None):
    _assert_scenario_sources(timeline, sources, TemplateDefinition.model_validate(template))
    if len(json.dumps(sources, ensure_ascii=False).encode()) > 16 * 1024 * 1024:
        raise EditingError("edit_snapshot_limit", "素材及原文快照超过16MiB，请拆分项目", 422)
    revision_id = models.new_id()
    if existing_timeline is None:
        timeline = timeline.model_copy(deep=True); timeline.id = "tlr_" + revision_id.replace("-", "")
        row = models.Timeline(id=timeline.id, tenant_id=document.tenant_id, project_id=document.project_id,
            name=f"{document.name[:170]} · v{number}", fps=timeline.fps, duration_ms=round(timeline.duration * 1000),
            timeline_json=timeline.model_dump(mode="json"))
        db.add(row); db.flush()
    revision = models.PlanRevision(id=revision_id, tenant_id=document.tenant_id, document_id=document.id,
        number=number, parent_revision_id=parent, idempotency_key=key, request_hash=request_hash,
        content_hash=digest({"template": template, "sources": sources, "plan": plan}), template_json=deepcopy(template),
        sources_json=deepcopy(sources), plan_json=deepcopy(plan), operation_json=deepcopy(operation))
    db.add(revision); db.flush()
    canonical = timeline.model_dump(mode="json")
    db.add(models.TimelineRevision(id=revision.id, tenant_id=document.tenant_id, document_id=document.id,
        timeline_id=timeline.id, content_hash=digest(canonical), timeline_json=canonical))
    db.flush()
    return revision


def initialize_document(db: Session, *, job: models.Job, timeline_row: models.Timeline,
                        assets: list[models.Asset], template: TemplateDefinition, actor: str = "worker") -> models.PlanRevision:
    """Caller holds the originating Job lock and owns commit/rollback."""
    document = db.scalar(select(models.EditDocument).where(models.EditDocument.origin_job_id == job.id,
                                                            models.EditDocument.tenant_id == job.tenant_id))
    if document is not None:
        return db.scalar(select(models.PlanRevision).where(models.PlanRevision.document_id == document.id,
                                                          models.PlanRevision.number == 1))
    if job.type != "auto_edit.plan" or not job.project_id or timeline_row.project_id != job.project_id or timeline_row.tenant_id != job.tenant_id:
        raise EditingError("edit_source_plan_required", "需要本项目已编译的剪辑计划", 409)
    document = models.EditDocument(tenant_id=job.tenant_id, project_id=job.project_id, origin_job_id=job.id,
                                  name=timeline_row.name[:200])
    db.add(document); db.flush()
    canonical = Timeline.model_validate(timeline_row.timeline_json)
    sources = _capture_sources(assets)
    plan = deepcopy((job.payload or {}).get("plan") or {})
    plan["selected"] = _plan_rows(document.id, canonical, plan, sources)
    revision = _insert_revision(db, document, number=1, parent=None, key="initialize:" + job.id,
        request_hash=digest({"job_id": job.id}), template=template.model_dump(mode="json"), sources=sources, plan=plan,
        timeline=canonical, existing_timeline=timeline_row,
        operation={"kind": "initialize", "actor": actor, "undo_revision_id": None, "at": datetime.now(timezone.utc).isoformat()})
    db.add(models.EditHead(document_id=document.id, revision_id=revision.id, number=1)); db.flush()
    return revision


def _replace(row: dict, candidate: dict, rows: list[dict], template: TemplateDefinition) -> None:
    if row["locked"]:
        raise EditingError("edit_selection_locked", "该镜头已锁定，请先解锁")
    slot = next((slot for slot in template.slots if slot.name == row["slot"]), None)
    if slot is None or not _eligible(candidate, slot, template, row["output_duration"]):
        raise EditingError("edit_candidate_ineligible", "该素材不满足镜头类型、时长或方向要求", 422)
    from .scenario_profiles import template_scenario
    profile = template_scenario(template)
    if profile and profile.preserve_speech_meaning and abs(candidate["duration"] - row["output_duration"]) > 1e-8:
        raise EditingError("edit_scenario_boundary", "口播替换需保留完整讲话单元，请选择相同时长的单元或重新编排计划", 422)
    if any(other["selection_id"] != row["selection_id"] and other["asset_id"] == candidate["candidate_id"] for other in rows):
        raise EditingError("edit_candidate_already_used", "该镜头已在其他位置使用，请选择其他候选", 422)
    row.update(asset_id=candidate["candidate_id"], source_asset_id=candidate["source_asset_id"], name=candidate["name"],
        source_start=candidate["source_start"], source_end=candidate["source_start"] + row["output_duration"],
        window_start=candidate["source_start"], window_end=candidate["source_end"], score=candidate["score"],
        rationale=deepcopy(candidate["rationale"]), selection_basis=candidate["selection_basis"])


def _rows_to_timeline(canonical: Timeline, rows: list[dict], sources: list[dict]) -> Timeline:
    value = canonical.model_dump(mode="json"); by_clip = {row["clip_id"]: row for row in rows}
    scenario = canonical.metadata.get("scenario")
    candidates = _candidate_map(sources, canonical.fps, scenario=scenario) if scenario else {}
    for track in value["tracks"]:
        for clip in track["clips"]:
            row = by_clip.get(clip["id"])
            if row is None:
                continue
            clip.update(asset_id=row["source_asset_id"], source_start=row["source_start"], source_end=row["source_end"])
            clip["metadata"].update(source_shot_id=row["asset_id"], slot=row["slot"], score=row["score"], rationale=deepcopy(row["rationale"]))
            if scenario:
                candidate = candidates.get(row["asset_id"])
                if candidate is None:
                    raise EditingError("edit_candidate_ineligible", "该来源缺少本场景必需的分析证据，请先完成分析", 422)
                speech = candidate["metadata"].get("speech_unit")
                if speech:
                    clip["metadata"]["speech_unit"] = deepcopy(speech)
                else:
                    clip["metadata"].pop("speech_unit", None)
    used = {clip["asset_id"] for track in value["tracks"] for clip in track["clips"] if clip.get("asset_id")}
    original = {row["id"]: row for row in value["assets"]}; frozen = {row["id"]: row for row in sources}
    value["assets"] = []
    for asset_id in sorted(used):
        if asset_id in original:
            value["assets"].append(original[asset_id]); continue
        source = frozen.get(asset_id)
        if source is None:
            raise EditingError("edit_source_unavailable", "替换素材未包含在版本中", 422)
        value["assets"].append(AssetRef(id=asset_id, type=source["kind"].split("/", 1)[0], uri=source["uri"],
                                      metadata={"name": source["name"]}).model_dump(mode="json"))
    return Timeline.model_validate(value)


def apply_edit(db: Session, *, document: models.EditDocument, request: schemas.EditApplyRequest,
               key: str, actor: str) -> models.PlanRevision:
    request_hash = digest(request.model_dump(mode="json"))
    head = db.scalar(select(models.EditHead).where(models.EditHead.document_id == document.id).with_for_update()
                     .execution_options(populate_existing=True))
    if head is None:
        raise EditingError("edit_snapshot_missing", "编辑项目尚未初始化")
    existing = db.scalar(select(models.PlanRevision).where(models.PlanRevision.document_id == document.id,
                                                           models.PlanRevision.idempotency_key == key))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise EditingError("idempotency_input_conflict", "同一操作标识已经用于不同修改")
        return existing
    if head.revision_id != request.base_revision_id:
        raise EditingError("edit_revision_conflict", "其他页面已保存新版，请保留当前输入并重新载入后合并", details={"head_revision_id": head.revision_id})
    base = revision_for_document(db, document, head.revision_id)
    frozen = timeline_for_revision(db, base)
    template = TemplateDefinition.model_validate(base.template_json)
    sources = deepcopy(base.sources_json); plan = deepcopy(base.plan_json)
    canonical = Timeline.model_validate(frozen.timeline_json)
    restore = [op for op in request.operations if op.op in {"restore", "undo"}]
    if restore and (len(request.operations) != 1 or request.source_asset_ids):
        raise EditingError("edit_restore_must_be_single", "恢复或撤销需作为独立操作提交", 422)
    undo_target = base.id
    operation = {"kind": "edit", "actor": actor, "note": request.note,
        "operations": [op.model_dump(mode="json") for op in request.operations], "undo_revision_id": undo_target,
        "at": datetime.now(timezone.utc).isoformat()}
    if restore:
        action = restore[0]
        target_id = action.revision_id if action.op == "restore" else base.operation_json.get("undo_revision_id")
        if not target_id:
            raise EditingError("edit_nothing_to_undo", "没有可撤销的修改")
        target = revision_for_document(db, document, target_id)
        target_timeline = timeline_for_revision(db, target)
        template = TemplateDefinition.model_validate(target.template_json)
        sources = deepcopy(target.sources_json); plan = deepcopy(target.plan_json)
        canonical = Timeline.model_validate(target_timeline.timeline_json)
        operation.update(kind=action.op, restored_revision_id=target.id)
        if action.op == "undo":
            operation["undo_revision_id"] = target.operation_json.get("undo_revision_id")
    else:
        if any(action.op == "refresh_candidates" for action in request.operations) and not request.source_asset_ids:
            raise EditingError("edit_new_sources_required", "请先选择需要加入候选库的素材", 422)
        if request.source_asset_ids:
            known = {source["id"] for source in sources}; wanted = set(request.source_asset_ids) - known
            assets = list(db.scalars(select(models.Asset).where(models.Asset.id.in_(wanted),
                models.Asset.tenant_id == document.tenant_id, models.Asset.project_id == document.project_id))) if wanted else []
            if len(assets) != len(wanted):
                raise EditingError("edit_source_unavailable", "新增素材不属于当前项目", 404)
            from .storyboards import output_assets
            output_assets(db, assets)
            sources.extend(_capture_sources(assets))
        rows = plan.get("selected") or []; candidates = _candidate_map(sources, canonical.fps, scenario=canonical.metadata.get("scenario"))
        def selected(selection_id):
            row = next((row for row in rows if row["selection_id"] == selection_id), None)
            if row is None:
                raise EditingError("edit_selection_not_found", "未找到该版本的镜头", 404)
            return row
        for action_index, action in enumerate(request.operations):
            if action.op == "refresh_candidates":
                pass  # Source snapshots were captured above; selected clips stay fixed.
            elif action.op == "set_lock":
                selected(action.selection_id)["locked"] = action.locked
            elif action.op == "replace":
                candidate = candidates.get(action.candidate_id)
                if candidate is None:
                    raise EditingError("edit_candidate_not_found", "未找到版本内的候选镜头", 404)
                _replace(selected(action.selection_id), candidate, rows, template)
            elif action.op == "replan":
                targets = [selected(value) for value in dict.fromkeys(action.selection_ids)] if action.selection_ids else [row for row in rows if not row["locked"]]
                if any(row["locked"] for row in targets):
                    raise EditingError("edit_selection_locked", "局部重算包含已锁定镜头，请先解锁")
                for row in targets:
                    slot = next((slot for slot in template.slots if slot.name == row["slot"]), None)
                    used = {other["asset_id"] for other in rows}
                    options = [item for item in candidates.values() if item["candidate_id"] not in used and slot is not None
                               and _eligible(item, slot, template, row["output_duration"])]
                    options.sort(key=lambda item: (-item["score"], item["source_asset_id"], item["source_start"]))
                    if options:
                        _replace(row, options[0], rows, template)
                    else:
                        plan.setdefault("warnings", []).append(f"镜头 {row['slot']} 没有合适的替代候选，保留原镜头。")
            elif action.op in TRACK_ACTIONS:
                canonical = _rows_to_timeline(canonical, rows, sources)
                canonical = apply_track_action(canonical, sources, rows, action, intent=f"{document.id}:{base.id}:{key}:{action_index}")
                rows[:] = _plan_rows(document.id, canonical, plan, sources)
                if action.op == "set_transition":
                    template.duration = canonical.duration
            elif action.op in SUBTITLE_ACTIONS:
                canonical = _rows_to_timeline(canonical, rows, sources)
                if action.op == "import_transcript":
                    ids = set(action.asset_ids) or {clip.asset_id for track in canonical.tracks if track.type.value == "video" for clip in track.clips}
                    known = {row["id"]: row for row in sources}
                    if not ids <= set(known):
                        raise EditingError("subtitle_source_not_found", "识别原文来源不属于当前版本", 404)
                    wanted = {asset_id for asset_id in ids if action.refresh_sources or not known[asset_id].get("transcript")}
                    if wanted:
                        _assert_sources_current(db, document, sources, wanted)
                        assets = list(db.scalars(select(models.Asset).where(models.Asset.id.in_(wanted),
                            models.Asset.tenant_id == document.tenant_id, models.Asset.project_id == document.project_id)))
                        for asset in assets:
                            analysis = (asset.metadata_json or {}).get("analysis") or {}
                            if get_settings().app_environment == "production":
                                try:
                                    validate_hosted_analysis(analysis, source_uri=asset.uri)
                                except ProviderExecutionError as exc:
                                    raise EditingError("subtitle_analysis_invalid", "当前识别结果未通过来源校验，原文和修订均未改变", 422) from exc
                            known[asset.id]["transcript"] = capture_transcript(analysis)
                            known[asset.id]["transcript_captured_later"] = True
                    canonical, warnings = import_transcript(canonical, sources, action)
                    plan["warnings"] = list(dict.fromkeys([*(plan.get("warnings") or []), *warnings]))[-100:]
                elif action.op == "save_subtitle":
                    canonical = save_subtitle(canonical, action, intent=f"{document.id}:{base.id}:{key}:{action_index}")
                else:
                    canonical = delete_subtitle(canonical, action.cue_id)
            elif action.op in CLIP_ACTIONS:
                canonical = _rows_to_timeline(canonical, rows, sources)
                canonical = apply_clip_action(canonical, rows, action,
                    intent=f"{document.id}:{base.id}:{key}:{action_index}")
                rows[:] = _plan_rows(document.id, canonical, plan, sources)
                if action.op != "set_clip_audio":
                    template.duration = canonical.duration
                    if any(track.type.value != "video" and track.clips for track in canonical.tracks):
                        warning = "主视频已顺接调整，其他轨道保留原时间点，请审阅音画和字幕同步。"
                        plan["warnings"] = list(dict.fromkeys([*(plan.get("warnings") or []), warning]))[-100:]
            elif action.op == "configure":
                changing_duration = action.target_duration != template.duration
                if changing_duration and any(row["locked"] for row in rows):
                    raise EditingError("edit_locked_duration", "请先解锁镜头，再调整整体目标时长")
                template.duration = action.target_duration
                template.metadata = {**template.metadata, "resolution": {"9:16": {"width": 1080, "height": 1920},
                    "16:9": {"width": 1920, "height": 1080}, "1:1": {"width": 1080, "height": 1080}}[action.aspect_ratio]}
                if changing_duration:
                    if canonical.metadata.get("precise_clip_edits"):
                        canonical = _rows_to_timeline(canonical, rows, sources)
                        canonical = resize_precise_sequence(canonical, action.target_duration)
                        canonical.resolution = template.metadata["resolution"]
                        rows[:] = _plan_rows(document.id, canonical, plan, sources)
                        if action.target_duration and action.target_duration > canonical.duration + 1e-8:
                            plan["warnings"] = list(dict.fromkeys([*(plan.get("warnings") or []),
                                "目标时长大于精细裁切后的可用片段，保留已裁切区间，不隐式恢复被剪掉的内容。"]))[-100:]
                    else:
                        chosen = [{**row, "source_start": row["window_start"], "source_end": row["window_end"]} for row in rows]
                        canonical = compile_auto_edit_timeline(template, chosen, _source_assets(sources), timeline_id=canonical.id,
                                                               resolution=template.metadata["resolution"])
                        for original_row, clip in zip(rows, canonical.tracks[0].clips):
                            clip.id = original_row["clip_id"]
                        fresh = _plan_rows(document.id, canonical, plan, sources)
                        for old, new in zip(rows, fresh):
                            old.update({key: new[key] for key in ("source_start", "source_end", "timeline_start", "timeline_end", "output_duration")})
                        from .scenario_profiles import template_scenario
                        planner = AutoEditPlanner().plan(template, shot_features_from_assets(_source_assets(sources), scenario=template_scenario(template)))
                        plan["warnings"] = list(planner.warnings)
                else:
                    canonical.resolution = template.metadata["resolution"]
        canonical = _rows_to_timeline(canonical, rows, sources)
        if any(track.type.value == "subtitle" for track in canonical.tracks):
            canonical, subtitle_warnings = sync_subtitle_anchors(canonical)
            plan["warnings"] = list(dict.fromkeys([*(plan.get("warnings") or []), *subtitle_warnings]))[-100:]
        plan["missing_required_slots"] = [slot.name for slot in template.slots if slot.required and
            len({row["asset_id"] for row in rows if row["slot"] == slot.name}) < max(1, slot.constraints.min_items)]
    used = {asset.id for asset in canonical.assets}
    for track in canonical.tracks:
        if track.type.value == "overlay":
            for clip in track.clips:
                placement = clip.placement
                if placement and (placement.x < 0 or placement.y < 0 or placement.x + (placement.width or 0) > canonical.resolution.width or placement.y + (placement.height or 0) > canonical.resolution.height):
                    raise EditingError("edit_overlay_bounds", "当前叠加区域超出画布，请先调整位置和尺寸，再修改画幅", 422)
    _assert_sources_current(db, document, sources, used)
    report = validate_timeline(canonical)
    if not report.valid:
        raise EditingError("edit_timeline_invalid", "修改后的时间线未通过校验", 422,
                           {"issues": report.as_dict()["issues"]})
    revision = _insert_revision(db, document, number=head.number + 1, parent=base.id, key=key,
        request_hash=request_hash, template=template.model_dump(mode="json"), sources=sources, plan=plan,
        timeline=canonical, operation=operation)
    head.revision_id = revision.id; head.number = revision.number; db.flush()
    return revision
