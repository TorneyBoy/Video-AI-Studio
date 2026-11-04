"""Human review binds exact bytes, QC evidence, manifest and editor revision.

No media IO, external service calls, or new jobs are performed here.
"""
from math import isfinite
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..review_schemas import ReviewSubmit
from .artifact_delivery import _artifact, _eligible_delivery
from .artifact_quality_gate import latest_quality_review, quality_evidence
from .completion import render_revision_binding
from .editing import EditingError, digest, validate_render_revision
from .media_quality import validate_render_quality
from .providers import ProviderExecutionError
from .storage_gateway import StorageGatewayError
from timeline_engine import RenderManifest, Timeline


QC_FIELDS = ("schema_version", "status", "object_uri", "output_sha256", "size_bytes", "width", "height",
             "duration_seconds", "fps", "has_audio", "video_codec", "container", "inspector", "inspector_version")


def can_review(db: Session, tenant_id: str, actor_id: str | None) -> bool:
    if get_settings().auth_mode == "development_header":
        return True
    # Personal owners can self-review. Editors cannot grant themselves approval.
    # Team reviewer roles are introduced with the complete F03 permission model.
    return bool(actor_id and db.scalar(select(models.Membership.id).join(models.User, models.User.id == models.Membership.user_id).where(
        models.Membership.tenant_id == tenant_id, models.Membership.user_id == actor_id,
        models.Membership.status == "active", models.Membership.role == "owner", models.User.status == "active")))


def latest_review(db: Session, artifact_id: str):
    return db.scalar(select(models.ArtifactReview).where(models.ArtifactReview.artifact_id == artifact_id)
                     .order_by(models.ArtifactReview.number.desc()).limit(1))


def review_read(row: models.ArtifactReview) -> dict:
    created = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at.astimezone(timezone.utc)
    return {"id": row.id, "number": row.number, "artifact_id": row.artifact_id,
            "binding_hash": row.binding_hash, "actor_id": row.actor_id, "decision": row.decision,
            "note": row.note, "issues": row.issues_json, "created_at": created}


def output_binding(row, payload: dict, archive, qc: dict | None = None, *, quality_binding=None) -> dict:
    """The same lightweight content fingerprint used by review and works lists."""
    if qc is None: qc = (row.metadata_json or {}).get("quality_control") or {}
    qc = {key: qc[key] for key in QC_FIELDS if key in qc} if isinstance(qc, dict) else {}
    result = {"schema_version": "artifact-review-binding/1", "artifact_id": row.id,
        "tenant_id": row.tenant_id, "project_id": row.project_id, "job_id": row.job_id,
        "output_role": row.output_role, "output_index": row.output_index,
        "archive_sha256": archive.sha256 if archive else None, "archive_size": archive.size_bytes if archive else None,
        "quality_hash": digest(qc), "manifest_hash": (payload.get("manifest") or {}).get("content_hash"),
        **{key: payload.get(key) for key in ("edit_document_id", "plan_revision_id", "timeline_revision_id", "plan_hash", "timeline_hash")}}
    if quality_binding:
        result["quality_evidence"] = quality_binding
    return result


def _context(db: Session, row: models.Artifact) -> dict:
    metadata = row.metadata_json or {}; qc = metadata.get("quality_control") or {}
    qc = {key: qc[key] for key in QC_FIELDS if key in qc} if isinstance(qc, dict) else {}
    archive = db.get(models.ArtifactStorage, row.id)
    job = db.get(models.Job, row.job_id) if row.job_id else None
    payload = job.payload or {} if job else {}
    quality = quality_evidence(row, payload, latest_review=latest_quality_review(db, row))
    binding = output_binding(row,payload,archive,qc,quality_binding=quality["binding"])
    ready = True; reason = "结构质检和归档已通过，请人工审阅画面、声音与字幕。"; code = None
    duration = qc.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not isfinite(duration) or duration <= 0:
        duration = None
    revision_number = None; origin_job_id = None
    try:
        _eligible_delivery(db, tenant_id=row.tenant_id, artifact_id=row.id)
        if job is None or job.tenant_id != row.tenant_id or job.project_id != row.project_id or not job.type.startswith("render."):
            raise ValueError("missing render context")
        manifest = RenderManifest.model_validate(payload.get("manifest"))
        if not manifest.verify_hash() or metadata.get("content_hash") != manifest.content_hash:
            raise ValueError("manifest identity mismatch")
        canonical = Timeline.model_validate(manifest.source.get("timeline"))
        validate_render_quality(qc, timeline=canonical, output_uri=row.uri)
        validate_render_revision(db, job, manifest)
        if any(metadata.get(key) != value for key, value in render_revision_binding(job).items()):
            raise ValueError("output revision mismatch")
        duration = canonical.duration
        if payload.get("timeline_revision_id"):
            revision = db.get(models.PlanRevision, payload["timeline_revision_id"])
            document = db.get(models.EditDocument, payload.get("edit_document_id"))
            revision_number = revision.number
            origin_job_id = document.origin_job_id
    except StorageGatewayError as exc:
        ready = False; code = exc.code
        reason = {"artifact_archive_pending": "成片仍在归档，暂不能批准或领取。", "artifact_quality_pending": "归档内容尚未通过结构质检。",
                  "artifact_retention_expired": "成片已超过保留期限。", "artifact_delivery_forbidden": "成片访问已撤销。",
                  "artifact_job_not_deliverable": "任务尚未达到交付条件。"}.get(exc.code, "当前成片尚不可交付。")
    except (ValueError, TypeError, EditingError, ProviderExecutionError):
        ready = False; code = "review_binding_invalid"; reason = "成片内容、质检或输入版本无法核对，不能批准或领取。"
    preview_ready = ready
    if ready and not quality["ready"]:
        ready = False; reason = quality["reason"]; code = quality["code"]
    return {"binding": binding, "binding_hash": digest(binding), "ready": ready, "preview_ready": preview_ready, "reason": reason, "code": code,
            "duration": duration, "revision_number": revision_number, "origin_job_id": origin_job_id,
            "measurements": {key: qc[key] for key in ("width", "height", "duration_seconds", "fps", "has_audio", "video_codec", "container") if key in qc}}


def review_report(db: Session, *, tenant_id: str, artifact_id: str, actor_id: str | None = None) -> dict:
    row = _artifact(db, tenant_id, artifact_id); context = _context(db, row)
    latest = latest_review(db, artifact_id)
    status = "pending" if latest is None else latest.decision if latest.binding_hash == context["binding_hash"] else "stale"
    return {"artifact_id": artifact_id, "job_id": row.job_id, "project_id": row.project_id,
        "binding_hash": context["binding_hash"], "binding": context["binding"], "status": status,
        "latest": review_read(latest) if latest else None, "can_review": can_review(db, tenant_id, actor_id),
        "can_preview": context["preview_ready"], "can_download": context["ready"] and status == "approved",
        "quality_ready": context["ready"], "quality_scope": "结构质检、现有质量问题和场景分项评审；正式领取仍需所有者批准",
        "reason": context["reason"], "duration": context["duration"], "measurements": context["measurements"],
        "revision_number": context["revision_number"], "origin_job_id": context["origin_job_id"]}


def submit_review(db: Session, *, tenant_id: str, artifact_id: str, actor_id: str | None,
                  key: str, request: ReviewSubmit) -> models.ArtifactReview:
    if not can_review(db, tenant_id, actor_id):
        raise EditingError("review_permission_denied", "当前仅空间所有者可批准或退回成片", 403)
    row = _artifact(db, tenant_id, artifact_id, lock=True)
    actor = actor_id or "development_user"
    request_hash = digest({"actor_id": actor, "request": request.model_dump(mode="json")})
    existing = db.scalar(select(models.ArtifactReview).where(models.ArtifactReview.artifact_id == artifact_id,
                                                             models.ArtifactReview.idempotency_key == key))
    if existing:
        if existing.request_hash != request_hash:
            raise EditingError("review_request_conflict", "同一审核操作标识已用于不同内容")
        return existing
    latest = latest_review(db, artifact_id); context = _context(db, row)
    if request.binding_hash != context["binding_hash"]:
        raise EditingError("review_content_changed", "成片或质检证据已变化，请载入当前状态后重新审阅")
    if request.base_review_id != (latest.id if latest else None):
        raise EditingError("review_revision_conflict", "审核状态已被其他页面修改，请载入新版后再决定")
    if request.decision == "approved" and not context["ready"]:
        raise EditingError(context["code"] or "review_not_ready", context["reason"], 409)
    for issue in request.issues:
        if issue.start is not None and (context["duration"] is None or issue.start > context["duration"] or
                                       (issue.end is not None and issue.end > context["duration"])):
            raise EditingError("review_issue_bounds", "问题时间点必须在当前成片时长内", 422)
    result = models.ArtifactReview(tenant_id=tenant_id, artifact_id=artifact_id, number=(latest.number + 1 if latest else 1),
        base_review_id=request.base_review_id, binding_hash=context["binding_hash"], binding_json=context["binding"],
        actor_id=actor, decision=request.decision, note=request.note, issues_json=[issue.model_dump(mode="json") for issue in request.issues],
        idempotency_key=key, request_hash=request_hash)
    db.add(result); db.flush()
    return result


def require_approved(db: Session, *, tenant_id: str, artifact_id: str) -> str:
    row = _artifact(db, tenant_id, artifact_id); context = _context(db, row)
    latest = latest_review(db, artifact_id)
    if not context["ready"]:
        raise StorageGatewayError(context["code"] or "review_not_ready", context["reason"], status_code=409)
    if latest is None or latest.decision != "approved" or latest.binding_hash != context["binding_hash"]:
        raise StorageGatewayError("artifact_review_required", "请先审阅并批准当前成片，再正式领取", status_code=409)
    return latest.id
