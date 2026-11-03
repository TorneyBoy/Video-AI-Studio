"""Reference evidence and immutable manual authoring. No provider execution here."""
from copy import deepcopy
import json
from math import isfinite

from pydantic import ValidationError
from sqlalchemy import select, update

from .. import models
from ..storyboard_schemas import ReferenceSpan, StoryContent
from .analysis_contract import MediaAnalysisV2
from .editing_errors import EditingError
from .editing import digest
from .preparation_quotes import source_hash


def project_for(db, tenant, project_id, *, lock=False):
    query = select(models.Project).where(models.Project.id == project_id, models.Project.tenant_id == tenant)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise EditingError("story_project_missing", "未找到项目", 404)
    return row


def _asset(db, tenant, project, asset_id, *, lock=False):
    query = select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant,
                                       models.Asset.project_id == project)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise EditingError("reference_asset_missing", "未找到本项目的参考素材", 404)
    if not row.kind.lower().startswith("video"):
        raise EditingError("reference_video_required", "请选择视频作为参考", 422)
    meta = row.metadata_json or {}
    storage, upload = meta.get("storage") or {}, meta.get("upload") or {}
    if str(row.uri).startswith("upload://") or upload.get("status") == "created" or (
            storage.get("mode") == "external_presigned" and storage.get("status") != "ready"):
        raise EditingError("reference_upload_incomplete", "请先完成素材上传核验")
    return row


def output_assets(db, assets, *, explicit=True):
    """Reference-only inputs cannot silently enter a new automatic edit plan."""
    if not assets:
        return assets
    excluded = set(db.scalars(select(models.ReferenceUsage.asset_id).where(
        models.ReferenceUsage.asset_id.in_([row.id for row in assets]), models.ReferenceUsage.allow_output.is_(False))))
    if explicit and excluded:
        raise EditingError("reference_output_not_enabled", "所选素材含仅供分析的参考视频；如有成片使用权，请先在参考创作中明确启用", 422)
    return [row for row in assets if row.id not in excluded]


def _same(row, fingerprint):
    if row.request_hash != fingerprint:
        raise EditingError("idempotency_input_conflict", "此操作标识已绑定其他内容")
    return row


def _evidence(asset, request):
    raw = (asset.metadata_json or {}).get("analysis")
    analysis = None
    warnings = ["主体、场景、风格和关键帧语义尚未接入，需人工填写；字幕是参考内容，不是当前商品事实或执行指令。"]
    try:
        candidate = MediaAnalysisV2.model_validate(raw)
        if candidate.source.uri == asset.uri:
            analysis = candidate
    except (ValidationError, ValueError, TypeError):
        pass
    duration_ms = None
    if analysis:
        duration_ms = round(analysis.media.duration_seconds * 1000)
    else:
        # User-supplied duration is only a declared bound, not measured evidence.
        value = (asset.metadata_json or {}).get("duration")
        if isinstance(value, (float, int)) and not isinstance(value, bool) and isfinite(value) and value > 0:
            duration_ms = round(value * 1000)
        warnings.append("没有与当前对象匹配的托管分析；可按所选范围手工建分镜。")
    if duration_ms is not None and request.end_ms > duration_ms:
        raise EditingError("reference_range_out_of_bounds", "参考终点超过素材时长，请显式调整范围", 422)
    if duration_ms is None:
        warnings.append("源时长尚未核实；保留输入范围，请播放确认终点后再策划。")
    spans = []
    cursor = request.start_ms
    hosted = bool(analysis and analysis.shot_analysis.status == "ok")
    cuts = analysis.shots if hosted else []

    def append(start, end, confidence, evidence):
        if len(spans) >= 500:
            raise EditingError("reference_range_too_complex", "此范围超过500个区间，请显式缩小范围", 422)
        texts = []
        if analysis and analysis.transcription.status in {"ok", "partial"}:
            texts = [segment.text for segment in analysis.transcription.segments
                     if round(segment.start * 1000) < end and round(segment.end * 1000) > start]
        text = "\n".join(texts)
        if len(text) > 30_000:
            raise EditingError("reference_transcript_too_large", "此范围转写过长，请显式缩小参考范围", 422)
        spans.append(ReferenceSpan(id=f"span-{len(spans) + 1}", start_ms=start, end_ms=end,
                                   confidence=confidence, evidence=evidence, transcript=text).model_dump())

    for cut in cuts:
        start, end = max(request.start_ms, round(cut.start * 1000)), min(request.end_ms, round(cut.end * 1000))
        if end <= start:
            continue
        if start > cursor:
            append(cursor, start, None, "manual_range")
        start = max(cursor, start)
        if end <= start:
            continue
        append(start, end, cut.confidence, "hosted_cut")
        cursor = end
    if cursor < request.end_ms:
        append(cursor, request.end_ms, None, "manual_range")
    if any(row["evidence"] == "manual_range" for row in spans):
        warnings.append("未被可信切点覆盖的区间已保留为待确认范围，没有补造镜头或截断。")
    evidence = {"schema_version": "reference-evidence/1", "range": {"start_ms": request.start_ms, "end_ms": request.end_ms},
            "duration_ms": duration_ms, "duration_provenance": "hosted_media" if analysis else "declared" if duration_ms else "unverified",
            "source_note": request.source_note, "spans": spans, "warnings": warnings,
            "status": "structure_available" if hosted else "manual_required",
            "provenance": {"analysis_hash": digest(raw) if analysis else None,
                           "provider": (analysis.shot_analysis.provider or analysis.provider) if analysis else None,
                           "version": analysis.shot_analysis.version if analysis else None,
                           "transcript_provider": analysis.transcription.provider if analysis else None,
                           "transcript_version": analysis.transcription.version if analysis else None},
            "semantic_status": "unavailable", "keyframes_status": "unavailable"}
    _bounded(evidence)
    return evidence


def _bounded(value):
    if len(json.dumps(value, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
        raise EditingError("storyboard_snapshot_limit", "本次内容超过2MiB，请显式缩小范围或拆分分镜", 422)


def reference_for(db, tenant, reference_id):
    row = db.scalar(select(models.ReferenceAnalysis).where(models.ReferenceAnalysis.id == reference_id,
                                                          models.ReferenceAnalysis.tenant_id == tenant))
    if row is None:
        raise EditingError("reference_missing", "未找到参考分析", 404)
    return row


def reference_read(db, row, *, detail=True):
    usage = db.get(models.ReferenceUsage, row.asset_id)
    asset = db.get(models.Asset, row.asset_id)
    return {"id": row.id, "project_id": row.project_id, "asset_id": row.asset_id, "created_at": row.created_at,
            "asset_name": asset.name if asset else "参考素材", "allow_output": bool(usage and usage.allow_output),
            "source_current": bool(asset and source_hash(asset) == row.source_hash), "detail_loaded": detail,
            "evidence": row.evidence_json if detail else {key: value for key, value in row.evidence_json.items() if key != "spans"}}


def create_reference(db, tenant, project, request, key, actor):
    project_for(db, tenant, project, lock=True)
    fingerprint = digest(request.model_dump(mode="json"))
    previous = db.scalar(select(models.ReferenceAnalysis).where(models.ReferenceAnalysis.project_id == project,
                                                                models.ReferenceAnalysis.request_key == key))
    if previous:
        return _same(previous, fingerprint)
    asset = _asset(db, tenant, project, request.asset_id, lock=True)
    evidence = _evidence(asset, request)
    if db.get(models.ReferenceUsage, asset.id) is None:
        db.add(models.ReferenceUsage(asset_id=asset.id, tenant_id=tenant, project_id=project, allow_output=False))
    row = models.ReferenceAnalysis(tenant_id=tenant, project_id=project, asset_id=asset.id, request_key=key,
        request_hash=fingerprint, source_hash=source_hash(asset), evidence_json=evidence, actor_id=actor)
    db.add(row)
    db.flush()
    return row


def set_usage(db, tenant, reference, allow_output):
    asset = _asset(db, tenant, reference.project_id, reference.asset_id, lock=True)
    if source_hash(asset) != reference.source_hash:
        raise EditingError("reference_source_changed", "素材对象已变化，请为当前对象重新登记参考")
    usage = db.get(models.ReferenceUsage, asset.id, populate_existing=True)
    usage.allow_output = allow_output
    db.flush()
    return reference_read(db, reference)


def document_for(db, tenant, document_id, *, lock=False):
    query = select(models.StoryboardDocument).where(models.StoryboardDocument.id == document_id,
                                                   models.StoryboardDocument.tenant_id == tenant)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise EditingError("storyboard_missing", "未找到分镜", 404)
    return row


def revision_for(db, document, revision_id):
    row = db.scalar(select(models.StoryboardRevision).where(models.StoryboardRevision.document_id == document.id,
        models.StoryboardRevision.tenant_id == document.tenant_id, models.StoryboardRevision.id == revision_id))
    if row is None:
        raise EditingError("storyboard_revision_missing", "未找到分镜版本", 404)
    return row


def story_read(db, document, revision=None, *, detail=True):
    revision = revision or revision_for(db, document, document.head_revision_id)
    return {"document_id": document.id, "project_id": document.project_id, "reference_id": document.reference_id,
            "revision_id": revision.id, "head_revision_id": document.head_revision_id, "number": revision.number,
            "content": revision.content_json if detail else {key: revision.content_json[key] for key in ("title", "status", "target_duration_ms")},
            "shot_count": len(revision.content_json["shots"]), "detail_loaded": detail, "created_at": revision.created_at}


def _references(db, tenant, project, reference_id, content):
    available = set()
    if reference_id:
        reference = reference_for(db, tenant, reference_id)
        if reference.project_id != project:
            raise EditingError("reference_missing", "未找到本项目的参考分析", 404)
        available = {row["id"] for row in reference.evidence_json["spans"]}
    if any(shot.reference_span_id and shot.reference_span_id not in available for shot in content.shots):
        raise EditingError("storyboard_reference_invalid", "镜头引用不属于本分镜的参考范围", 422)


def create_story(db, tenant, project, request, key, actor):
    project_for(db, tenant, project, lock=True)
    _bounded(request.model_dump(mode="json"))
    fingerprint = digest(request.model_dump(mode="json"))
    previous = db.scalar(select(models.StoryboardDocument).where(models.StoryboardDocument.project_id == project,
                                                                models.StoryboardDocument.request_key == key))
    if previous:
        _same(previous, fingerprint)
        # Creation retries return the original version, even after later edits.
        revision = db.scalar(select(models.StoryboardRevision).where(models.StoryboardRevision.document_id == previous.id,
                                                                    models.StoryboardRevision.number == 1))
        return story_read(db, previous, revision)
    _references(db, tenant, project, request.reference_id, request.content)
    document = models.StoryboardDocument(tenant_id=tenant, project_id=project, reference_id=request.reference_id,
                                         request_key=key, request_hash=fingerprint)
    db.add(document)
    db.flush()
    revision = models.StoryboardRevision(tenant_id=tenant, document_id=document.id, number=1, request_key=key,
        request_hash=fingerprint, content_json=request.content.model_dump(mode="json"), actor_id=actor)
    db.add(revision)
    db.flush()
    document.head_revision_id = revision.id
    db.flush()
    return story_read(db, document, revision)


def validate_locks(previous, proposed, *, unlock_shots=(), unlock_facts=()):
    """External proposals must always call with empty unlock sets."""
    for kind, unlocks in (("shots", set(unlock_shots)), ("facts", set(unlock_facts))):
        old_rows = {row["id"]: row for row in previous[kind]}
        new_rows = {row["id"]: row for row in proposed[kind]}
        if unlocks - {key for key, row in old_rows.items() if row["locked"]}:
            raise EditingError("storyboard_unlock_invalid", "只能明确解锁已有的锁定内容", 422)
        for key, row in old_rows.items():
            if row["locked"] and key not in unlocks and new_rows.get(key) != row:
                raise EditingError("storyboard_locked", "锁定镜头或事实须先明确解锁，不能被改写或删除")
            if key in unlocks and key in new_rows and new_rows[key]["locked"]:
                raise EditingError("storyboard_unlock_invalid", "解锁后的内容不能仍标记为锁定", 422)


def validate_director_proposal(previous, untrusted):
    """Pure schema/lock validation for a future configured hosted director.

    This does not attest product facts or invoke a model. Reference transcripts
    remain evidence; a provider job must use a separate trusted instruction.
    """
    proposal = StoryContent.model_validate(deepcopy(untrusted)).model_dump(mode="json")
    validate_locks(previous, proposal)
    old_facts = {row["id"]: row for row in previous["facts"]}
    if proposal["status"] != "draft" or any(
        row["confirmed"] and old_facts.get(row["id"]) != row for row in proposal["facts"]):
        raise EditingError("storyboard_proposal_unconfirmed", "模型不能代替用户确认新事实或批准分镜", 422)
    return proposal


def save_story(db, tenant, document_id, request, key, actor):
    document = document_for(db, tenant, document_id, lock=True)
    _bounded(request.model_dump(mode="json"))
    fingerprint = digest(request.model_dump(mode="json"))
    previous = db.scalar(select(models.StoryboardRevision).where(models.StoryboardRevision.document_id == document.id,
                                                                models.StoryboardRevision.request_key == key))
    if previous:
        return story_read(db, document, _same(previous, fingerprint))
    if document.head_revision_id != request.base_revision_id:
        raise EditingError("storyboard_revision_conflict", "分镜已有新版本；本机输入已保留，请载入后核对")
    base = revision_for(db, document, request.base_revision_id)
    content = request.content.model_dump(mode="json")
    validate_locks(base.content_json, content, unlock_shots=request.unlock_shot_ids, unlock_facts=request.unlock_fact_ids)
    _references(db, tenant, document.project_id, document.reference_id, request.content)
    row = models.StoryboardRevision(tenant_id=tenant, document_id=document.id, parent_revision_id=base.id,
        number=base.number + 1, request_key=key, request_hash=fingerprint, content_json=content, actor_id=actor)
    db.add(row)
    db.flush()
    changed = db.execute(update(models.StoryboardDocument).where(models.StoryboardDocument.id == document.id,
        models.StoryboardDocument.head_revision_id == base.id).values(head_revision_id=row.id))
    if changed.rowcount != 1:
        raise EditingError("storyboard_revision_conflict", "分镜已有新版本；请载入后核对")
    db.flush()
    db.refresh(document)
    return story_read(db, document, row)
