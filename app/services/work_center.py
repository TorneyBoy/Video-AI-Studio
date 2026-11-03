"""Cursor-based work browsing without loading media or large render snapshots."""
import base64
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from .. import models
from .artifact_reviews import output_binding
from .delivery_packages import utc
from .editing import digest
from .editing_errors import EditingError

LABELS={"processing":"处理中","pending_review":"待审核","approved":"已批准","returned":"需修改",
        "stale":"需重新审核","unavailable":"暂不可领取","failed":"未完成"}
STATES={"all","processing","completed","attention",*LABELS}


def _cursor(value):
    if not value: return None
    try:
        raw=json.loads(base64.urlsafe_b64decode(value+"="*(-len(value)%4)))
        timestamp=datetime.fromisoformat(raw["t"])
        if timestamp.tzinfo is None or not isinstance(raw["id"],str) or not 1<=len(raw["id"])<=64: raise ValueError("bad cursor")
        return utc(timestamp),raw["id"]
    except (ValueError,KeyError,TypeError) as exc:
        raise EditingError("works_cursor_invalid","分页位置已失效，请刷新作品列表",422) from exc


def _encode(row):
    return base64.urlsafe_b64encode(json.dumps({"t":utc(row["created_at"]).isoformat(),"id":row["job_id"]},separators=(",",":")).encode()).decode().rstrip("=")


def _query(tenant_id,project_id):
    job=models.Job;artifact=models.Artifact;archive=models.ArtifactStorage;review=aliased(models.ArtifactReview)
    latest=select(models.ArtifactReview.id).where(models.ArtifactReview.artifact_id==artifact.id).order_by(models.ArtifactReview.number.desc()).limit(1).correlate(artifact).scalar_subquery()
    fields=[job.id.label("job_id"),job.type,job.status.label("job_status"),job.created_at,job.policy_blocked,job.failure_code,
        artifact.id.label("artifact_id"),artifact.project_id.label("artifact_project"),artifact.delivery_status,artifact.legacy_duplicate_of,
        artifact.output_role,artifact.output_index,artifact.metadata_json["quality_control"].label("qc"),
        archive.sha256,archive.size_bytes,archive.expires_at,
        review.id.label("review_id"),review.decision,review.binding_hash.label("review_binding"),models.PlanRevision.number.label("revision_number")]
    for key in ("edit_document_id","plan_revision_id","timeline_revision_id","plan_hash","timeline_hash"):
        fields.append(job.payload[key].as_string().label(key))
    fields.append(job.payload["manifest"]["content_hash"].as_string().label("manifest_hash"))
    return (select(*fields).select_from(job).outerjoin(artifact,and_(artifact.job_id==job.id,artifact.tenant_id==tenant_id,
        artifact.output_role=="primary",artifact.output_index==0)).outerjoin(archive,archive.artifact_id==artifact.id)
        .outerjoin(review,review.id==latest).outerjoin(models.PlanRevision,and_(models.PlanRevision.id==job.payload["timeline_revision_id"].as_string(),models.PlanRevision.tenant_id==tenant_id))
        .where(job.tenant_id==tenant_id,job.project_id==project_id,job.type.in_(("render.draft","render.high"))))


def _view(row,tenant_id,project_id):
    qc=row["qc"] if isinstance(row["qc"],dict) else {};state="pending_review";reason="待人工审阅"
    if row["job_status"] in {"failed","cancelled"}: state="failed";reason="任务未完成，可到制作页核对原任务"
    elif row["job_status"]!="succeeded": state="processing";reason="服务端仍在处理，离开页面不代表取消"
    elif not row["artifact_id"] or row["artifact_project"]!=project_id: state="unavailable";reason="成片记录尚不可用"
    elif row["policy_blocked"] or row["delivery_status"] in {"revoked","deleted","archived_duplicate"} or row["legacy_duplicate_of"]:
        state="unavailable";reason="当前交付策略不允许领取"
    elif row["expires_at"] is None: state="processing";reason="成片正在归档"
    elif utc(row["expires_at"])<=datetime.now(timezone.utc): state="unavailable";reason="成片已超过保留期限"
    elif qc.get("status")!="passed" or qc.get("output_sha256")!=row["sha256"] or qc.get("size_bytes")!=row["size_bytes"]:
        state="unavailable";reason="结构质检或归档证据仍待处理"
    else:
        item=SimpleNamespace(id=row["artifact_id"],tenant_id=tenant_id,project_id=project_id,job_id=row["job_id"],
            output_role=row["output_role"],output_index=row["output_index"],metadata_json={"quality_control":qc})
        archive=SimpleNamespace(sha256=row["sha256"],size_bytes=row["size_bytes"])
        payload={key:row[key] for key in ("edit_document_id","plan_revision_id","timeline_revision_id","plan_hash","timeline_hash")}
        payload["manifest"]={"content_hash":row["manifest_hash"]}
        binding=digest(output_binding(item,payload,archive))
        if row["review_id"]:
            state="stale" if row["review_binding"]!=binding else "approved" if row["decision"]=="approved" else "returned"
        reason="批准仅适用于这份输出；正式领取时仍会核对当前媒体和来源权限" if state=="approved" else LABELS[state]
    return {"job_id":row["job_id"],"artifact_id":row["artifact_id"],"state":state,"status_label":LABELS[state],"reason":reason,
        "job_status":row["job_status"],"quality":"draft" if row["type"]=="render.draft" else "high", "revision_number":row["revision_number"],
        "created_at":utc(row["created_at"]),"failure_code":row["failure_code"] if state=="failed" else None,
        "duration":qc.get("duration_seconds"),"width":qc.get("width"),"height":qc.get("height"),
        "can_open":bool(row["artifact_id"]),"can_compare":bool(row["artifact_id"] and row["manifest_hash"])}


def work_lookup(db: Session,*,tenant_id: str,project_id: str,job_ids: list[str]):
    """Bounded metadata projection for a fixed batch wave; no media/source IO."""
    if len(job_ids)>100:raise EditingError('works_lookup_limit','一次最多查看100项',422)
    return {row['job_id']:_view(row,tenant_id,project_id) for row in db.execute(_query(tenant_id,project_id).where(models.Job.id.in_(job_ids))).mappings()}


def work_page(db: Session,*,tenant_id: str,project_id: str,state: str="all",cursor: str | None=None,limit: int=20):
    if state not in STATES: raise EditingError("works_filter_invalid","作品筛选无效",422)
    position=_cursor(cursor);items=[];last=None;scanned=0;more=False
    query=_query(tenant_id,project_id)
    while len(items)<limit and scanned<200:
        current=query
        if position:
            current=current.where(or_(models.Job.created_at<position[0],and_(models.Job.created_at==position[0],models.Job.id<position[1])))
        take=min(50,200-scanned)
        rows=list(db.execute(current.order_by(models.Job.created_at.desc(),models.Job.id.desc()).limit(take)).mappings())
        if not rows: break
        for row in rows:
            scanned+=1;last=row;view=_view(row,tenant_id,project_id)
            matches=(state=="all" or view["state"]==state or (state=="completed" and view["job_status"]=="succeeded")
                     or (state=="attention" and view["state"] in {"failed","returned","stale","unavailable"}))
            if matches: items.append(view)
            if len(items)>=limit or scanned>=200: break
        position=(utc(last["created_at"]),last["job_id"])
        if len(rows)<take and len(items)<limit: break
    if last:
        more=bool(db.scalar(select(models.Job.id).where(models.Job.tenant_id==tenant_id,models.Job.project_id==project_id,
            models.Job.type.in_(("render.draft","render.high")),or_(models.Job.created_at<utc(last["created_at"]),
            and_(models.Job.created_at==utc(last["created_at"]),models.Job.id<last["job_id"]))).limit(1)))
    total=db.scalar(select(func.count()).select_from(models.Job).where(models.Job.tenant_id==tenant_id,
        models.Job.project_id==project_id,models.Job.type.in_(("render.draft","render.high"))))
    outputs=db.scalar(select(func.count()).select_from(models.Artifact).where(models.Artifact.tenant_id==tenant_id,
        models.Artifact.project_id==project_id,models.Artifact.kind=="render",models.Artifact.output_role=="primary",
        models.Artifact.delivery_status.notin_(("deleted","revoked","archived_duplicate"))))
    return {"items":items,"next_cursor":_encode(last) if more else None,"has_more":more,"scanned":scanned,"total_jobs":total,"total_outputs":outputs}
