"""The same immutable authoring API for web and native personal clients."""
from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import RequestIdentity, authenticate_request, ensure_project, require_tenant_id
from .identity import PrivateValidationRoute
from .. import editing_schemas as edit_schemas, models, schemas
from ..database import get_db
from ..services.editing import (EditingError, apply_edit, candidates_read, document_for_tenant,
    initialize_document, revision_for_document, revision_read, timeline_for_revision)
from timeline_engine import TemplateDefinition
from ..services.template_presets import ensure_presets
from ..services.subtitle_authoring import subtitle_export
from timeline_engine import Timeline

router = APIRouter(tags=["editing"], route_class=PrivateValidationRoute)


@router.post("/projects/{project_id}/template-presets", response_model=list[schemas.TemplateRead])
def initialize_presets(project_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    ensure_project(db, project_id, tenant_id)
    rows = ensure_presets(db, tenant_id=tenant_id, project_id=project_id)
    db.commit()
    return rows


@router.post("/jobs/{job_id}/editing", response_model=edit_schemas.EditRevisionRead)
def open_editing(job_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                 identity: RequestIdentity = Depends(authenticate_request)):
    # Job -> head is the same order used by the worker. This lazy conversion
    # also keeps pre-M2 completed plans usable without modifying their media.
    job = db.scalar(select(models.Job).where(models.Job.id == job_id, models.Job.tenant_id == tenant_id)
                    .with_for_update().execution_options(populate_existing=True))
    if job is None:
        raise EditingError("edit_plan_not_found", "未找到剪辑计划", 404)
    if job.type != "auto_edit.plan" or job.status != "succeeded":
        raise EditingError("edit_plan_not_ready", "请先等待剪辑计划完成")
    document = db.scalar(select(models.EditDocument).where(models.EditDocument.origin_job_id == job.id,
                                                            models.EditDocument.tenant_id == tenant_id))
    if document is None:
        payload = job.payload or {}
        timeline = db.scalar(select(models.Timeline).where(models.Timeline.id == str(payload.get("timeline_id") or ""),
                                                           models.Timeline.tenant_id == tenant_id))
        if timeline is None:
            raise EditingError("edit_plan_not_ready", "计划时间线尚未保存")
        template_row = db.scalar(select(models.Template).where(models.Template.id == str(payload.get("template_id") or ""),
                                                               models.Template.tenant_id == tenant_id))
        template = payload.get("template_snapshot") or (template_row.content if template_row else None)
        if not template:
            raise EditingError("edit_template_snapshot_missing", "旧计划的模板信息已不可用，请保留原成片并新建计划")
        ids = set(payload.get("asset_ids") or []) | {row["id"] for row in timeline.timeline_json.get("assets", [])}
        assets = list(db.scalars(select(models.Asset).where(models.Asset.id.in_(ids), models.Asset.tenant_id == tenant_id,
                                                            models.Asset.project_id == job.project_id)))
        initial = initialize_document(db, job=job, timeline_row=timeline, assets=assets,
            template=TemplateDefinition.model_validate(template), actor=identity.principal_id or "development_user")
        job.payload = {**payload, "edit_document_id": initial.document_id, "plan_revision_id": initial.id,
                       "timeline_revision_id": initial.id}
        db.commit()
        document = document_for_tenant(db, tenant_id, initial.document_id)
    head = db.get(models.EditHead, document.id)
    return revision_read(db, document, revision_for_document(db, document, head.revision_id))


@router.get("/editing/{document_id}", response_model=edit_schemas.EditRevisionRead)
def current_edit(document_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = document_for_tenant(db, tenant_id, document_id)
    head = db.get(models.EditHead, document.id)
    return revision_read(db, document, revision_for_document(db, document, head.revision_id))


@router.get("/editing/{document_id}/revisions", response_model=list[edit_schemas.EditRevisionRead])
def revisions(document_id: str, skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
               tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = document_for_tenant(db, tenant_id, document_id)
    rows = db.scalars(select(models.PlanRevision).where(models.PlanRevision.document_id == document.id,
        models.PlanRevision.tenant_id == tenant_id).order_by(models.PlanRevision.number.desc()).offset(skip).limit(limit))
    return [revision_read(db, document, row) for row in rows]


@router.get("/editing/{document_id}/requests/{request_key}")
def edit_request_receipt(document_id: str, request_key: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = document_for_tenant(db, tenant_id, document_id)
    if not 1 <= len(request_key) <= 255:
        raise EditingError("idempotency_key_invalid", "修改操作标识无效", 422)
    row = db.scalar(select(models.PlanRevision).where(models.PlanRevision.document_id == document.id,
        models.PlanRevision.tenant_id == tenant_id, models.PlanRevision.idempotency_key == request_key))
    return {"found": row is not None, "revision": revision_read(db, document, row) if row else None}


@router.get("/editing/{document_id}/revisions/{revision_id}", response_model=edit_schemas.EditRevisionRead)
def get_revision(document_id: str, revision_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = document_for_tenant(db, tenant_id, document_id)
    return revision_read(db, document, revision_for_document(db, document, revision_id))


@router.get("/editing/{document_id}/revisions/{revision_id}/timeline")
def revision_timeline(document_id: str, revision_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = document_for_tenant(db, tenant_id, document_id)
    row = timeline_for_revision(db, revision_for_document(db, document, revision_id))
    return {"revision_id": row.id, "content_hash": row.content_hash, "timeline": row.timeline_json}


@router.get("/editing/{document_id}/revisions/{revision_id}/candidates")
def revision_candidates(document_id: str, revision_id: str, selection_id: str | None = None,
                         skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500),
                         tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = document_for_tenant(db, tenant_id, document_id)
    revision = revision_for_document(db, document, revision_id)
    frozen = timeline_for_revision(db, revision)
    rows = candidates_read(revision, fps=float(frozen.timeline_json["fps"]))
    if selection_id:
        selection = next((row for row in revision.plan_json.get("selected", []) if row["selection_id"] == selection_id), None)
        if selection is None:
            raise EditingError("edit_selection_not_found", "未找到该版本的镜头", 404)
        used = {row["asset_id"] for row in revision.plan_json.get("selected", []) if row["selection_id"] != selection_id}
        rows = [row for row in rows if selection["slot"] in row["eligible_slots"]
                and row["duration"] + 1e-8 >= selection["output_duration"] and row["candidate_id"] not in used]
    return {"items": rows[skip:skip + limit], "total": len(rows), "skip": skip, "limit": limit}


@router.get("/editing/{document_id}/revisions/{revision_id}/transcripts")
def original_transcripts(document_id: str, revision_id: str, skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                         tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = document_for_tenant(db, tenant_id, document_id)
    revision = revision_for_document(db, document, revision_id); timeline_for_revision(db, revision)
    rows = []; total = 0
    for source in revision.sources_json:
        snapshot = source.get("transcript") or {}
        segments = snapshot.get("segments", []); start = max(0, skip - total); end = max(0, skip + limit - total)
        for segment in segments[start:end]:
            rows.append({"asset_id": source["id"], "name": source["name"], "transcript_hash": snapshot["hash"],
                "language": snapshot.get("language"), **segment})
        total += len(segments)
    return {"items": rows, "total": total, "skip": skip, "limit": limit}


@router.get("/editing/{document_id}/revisions/{revision_id}/subtitles")
def export_subtitles(document_id: str, revision_id: str, format: str = Query("srt", pattern="^(srt|vtt)$"),
                     track_id: str | None = Query(None, max_length=100), tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    document = document_for_tenant(db, tenant_id, document_id)
    revision = revision_for_document(db, document, revision_id); frozen = timeline_for_revision(db, revision)
    return subtitle_export(Timeline.model_validate(frozen.timeline_json), revision_id=revision.id,
                           timeline_hash=frozen.content_hash, format=format, track_id=track_id)


@router.post("/editing/{document_id}/revisions", response_model=edit_schemas.EditRevisionRead, status_code=201)
def save_revision(document_id: str, payload: edit_schemas.EditApplyRequest,
                   idempotency_key: str = Header(min_length=1, max_length=255, alias="Idempotency-Key"),
                   tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                   identity: RequestIdentity = Depends(authenticate_request)):
    document = document_for_tenant(db, tenant_id, document_id)
    key = idempotency_key.strip()
    if not key:
        raise EditingError("idempotency_key_required", "缺少本次修改的操作标识", 422)
    revision = apply_edit(db, document=document, request=payload, key=key,
                          actor=identity.principal_id or "development_user")
    db.commit()
    return revision_read(db, document, revision)


@router.post("/editing/{document_id}/revisions/{revision_id}/render", status_code=201)
def render_revision(document_id: str, revision_id: str, payload: schemas.RenderRequest,
                      idempotency_key: str = Header(min_length=1, max_length=255, alias="Idempotency-Key"),
                      tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    from .routers import _render_timeline_job
    if not idempotency_key.strip():
        raise EditingError("idempotency_key_required", "缺少本次渲染的操作标识", 422)
    document = document_for_tenant(db, tenant_id, document_id)
    revision = revision_for_document(db, document, revision_id)
    timeline = timeline_for_revision(db, revision)
    row = db.get(models.Timeline, timeline.timeline_id)
    if row is None:
        raise EditingError("edit_snapshot_missing", "版本对应的时间线记录不可用")
    return _render_timeline_job(db, row, payload, tenant_id=tenant_id, key=idempotency_key.strip())
