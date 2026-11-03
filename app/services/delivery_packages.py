"""Metadata-only version comparison and bounded, credential-free file exports."""
from datetime import timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .artifact_delivery import _artifact
from .artifact_reviews import require_approved, review_report
from .clip_authoring import editor_view
from .completion import render_revision_binding
from .editing import (EditingError, digest, document_for_tenant, revision_for_document, timeline_for_revision)
from .subtitle_authoring import subtitle_export
from timeline_engine import RenderManifest, Timeline

MAX_EXPORT_BYTES = 16 * 1024 * 1024


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def artifact_timeline(db: Session, row: models.Artifact):
    job = db.get(models.Job, row.job_id) if row.job_id else None
    try:
        if job is None or job.tenant_id != row.tenant_id or job.project_id != row.project_id:
            raise ValueError("output context mismatch")
        payload = job.payload or {}; manifest = RenderManifest.model_validate(payload.get("manifest"))
        if not manifest.verify_hash() or (row.metadata_json or {}).get("content_hash") != manifest.content_hash:
            raise ValueError("manifest mismatch")
        canonical = Timeline.model_validate(manifest.source.get("timeline"))
        if (manifest.timeline_id != canonical.id or manifest.duration != canonical.duration or manifest.fps != canonical.fps
                or manifest.resolution != canonical.resolution.model_dump(mode="json")):
            raise ValueError("manifest headers mismatch")
        revision = None
        if payload.get("timeline_revision_id"):
            document = document_for_tenant(db, row.tenant_id, str(payload.get("edit_document_id") or ""))
            revision = revision_for_document(db, document, str(payload["timeline_revision_id"]))
            frozen = timeline_for_revision(db, revision)
            if (frozen.timeline_json != canonical.model_dump(mode="json") or payload.get("timeline_hash") != frozen.content_hash
                    or payload.get("plan_hash") != revision.content_hash or document.project_id != row.project_id):
                raise ValueError("revision mismatch")
        if any((row.metadata_json or {}).get(key) != value for key,value in render_revision_binding(job).items()):
            raise ValueError("artifact revision mismatch")
        return job, canonical, revision
    except (ValueError, TypeError, EditingError) as exc:
        raise EditingError("delivery_binding_invalid", "无法核对这份成片的输入版本，请保留记录并检查来源", 409) from exc


def _source_summary(canonical: Timeline, revision) -> list[dict]:
    frozen = {source["id"]:source for source in revision.sources_json} if revision else {}
    result = []
    for asset in canonical.assets:
        source = frozen.get(asset.id) or {}; metadata = source.get("metadata") or {}; storage = metadata.get("storage") or {}
        checksum = storage.get("sha256") or asset.checksum
        result.append({"id":asset.id,"name":source.get("name") or asset.metadata.get("name") or asset.id,
                       "type":asset.type.value,"sha256":checksum if isinstance(checksum,str) and len(checksum)==64 else None})
    return result


def _file(artifact_id: str, binding_hash: str, filename: str, content: str, **extra):
    raw = content.encode("utf-8")
    if len(raw) > MAX_EXPORT_BYTES:
        raise EditingError("delivery_export_too_large", "清单或字幕文件超过16MiB，请缩小项目后导出", 422)
    return {"artifact_id":artifact_id,"binding_hash":binding_hash,"filename":filename,"encoding":"UTF-8",
            "content":content,"sha256":hashlib.sha256(raw).hexdigest(),**extra}


def delivery_file(db: Session, *, tenant_id: str, artifact_id: str, kind: str, visibility: str = "internal",
                  expected_binding: str | None = None) -> dict:
    row = _artifact(db, tenant_id, artifact_id); report = review_report(db, tenant_id=tenant_id, artifact_id=artifact_id)
    if expected_binding and report["binding_hash"] != expected_binding:
        raise EditingError("delivery_content_changed", "成片或审核证据已变化，请刷新后重新导出")
    if visibility not in {"internal","public"} or kind not in {"manifest","srt","vtt"}:
        raise EditingError("delivery_export_invalid", "导出格式无效", 422)
    if visibility == "public":
        require_approved(db, tenant_id=tenant_id, artifact_id=artifact_id)
    job, canonical, revision = artifact_timeline(db, row)
    if kind != "manifest":
        exported = subtitle_export(canonical,revision_id=revision.id if revision else canonical.id,
                                   timeline_hash=(job.payload or {}).get("timeline_hash") or digest(canonical.model_dump(mode="json")),format=kind)
        return _file(artifact_id,report["binding_hash"],f"subtitles-{artifact_id}.{kind}",exported["content"],
                     kind=kind,visibility=visibility,cue_count=exported["cue_count"],warnings=exported["warnings"])
    archive = db.get(models.ArtifactStorage,artifact_id)
    # Public exports describe the approved output. Input identities, review text,
    # models, costs and all object locations remain private.
    value = {"schema_version":"delivery-manifest/1","visibility":visibility,
        "created_at":utc(row.created_at).isoformat(),"output":{"sha256":archive.sha256 if archive else None,
            "size_bytes":archive.size_bytes if archive else None,"width":canonical.resolution.width,"height":canonical.resolution.height,
            "fps":canonical.fps,"duration":canonical.duration,"format":"mp4"},
        "edit_revision":report["revision_number"],"review_status":report["status"],
        "delivery_eligible":report["can_download"],"quality_scope":"structure",
        "rights_verification":"not_assessed_by_this_manifest"}
    if visibility == "internal":
        metadata = row.metadata_json or {}; model = metadata.get("model") or {}
        if not isinstance(model,dict): model = {}
        ledgers = list(db.scalars(select(models.UsageLedger).where(models.UsageLedger.job_id==job.id)))
        adjustments = list(db.scalars(select(models.UsageAdjustment).where(models.UsageAdjustment.job_id==job.id)))
        value.update(artifact_id=artifact_id,job_id=job.id,binding=report["binding"],binding_hash=report["binding_hash"],
            sources=_source_summary(canonical,revision),timeline=editor_view(canonical.model_dump(mode="json")),
            model={key:model[key] for key in ("provider","model_id","version","weights_sha256","price_version")
                   if key in model and (model[key] is None or isinstance(model[key],(str,int,float,bool)))},
            review=report["latest"],cost_records=[{"amount_micros":entry.amount_micros,"currency":entry.currency,
                "status":entry.status,"decision":entry.decision,"reservation":bool((entry.metadata_json or {}).get("reservation"))} for entry in ledgers])
        value["cost_adjustments"]=[{"delta_micros":entry.delta_micros,"currency":entry.currency,"reason":entry.reason} for entry in adjustments]
        if value["review"]:
            value["review"] = {**value["review"],"created_at":utc(value["review"]["created_at"]).isoformat()}
    value["manifest_hash"] = digest(value)
    content = json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n"
    return _file(artifact_id,report["binding_hash"],f"delivery-{artifact_id}-{visibility}.json",content,
                 kind=kind,visibility=visibility,warnings=["清单不等同于内容权利验证或C2PA签名。"])


def compare_outputs(db: Session, *, tenant_id: str, project_id: str, left_id: str, right_id: str,
                    skip: int = 0, limit: int = 100) -> dict:
    sides = []
    for artifact_id in (left_id,right_id):
        row = _artifact(db,tenant_id,artifact_id)
        if row.project_id != project_id:
            raise EditingError("comparison_project_mismatch", "请选择同一项目的两份成片", 404)
        _,canonical,revision = artifact_timeline(db,row)
        report = review_report(db,tenant_id=tenant_id,artifact_id=artifact_id)
        sides.append({"artifact_id":artifact_id,"report":report,"timeline":editor_view(canonical.model_dump(mode="json")),
                      "sources":_source_summary(canonical,revision)})
    def clips(side):
        return {clip["id"]:{"track_id":track["id"],"track_type":track["type"],"track_muted":track["muted"],
                            "track_index":index,"clip_index":clip_index+1,**clip} for index,track in enumerate(side["timeline"]["tracks"]) for clip_index,clip in enumerate(track["clips"])}
    left,right = (clips(side) for side in sides); changes = []
    for clip_id in sorted(set(left)|set(right)):
        before,after = left.get(clip_id),right.get(clip_id)
        fields = {key:{"before":before.get(key) if before else None,"after":after.get(key) if after else None}
                  for key in sorted(set(before or {})|set(after or {})) if (before or {}).get(key)!=(after or {}).get(key)}
        if fields:
            changes.append({"clip_id":clip_id,"kind":"added" if before is None else "removed" if after is None else "changed",
                            "track_type":(after or before)["track_type"],"fields":fields,
                            "before_asset_id":before.get("asset_id") if before else None,"after_asset_id":after.get("asset_id") if after else None,
                            "position":(after or before)["clip_index"]})
    # Metadata comparison uses saved snapshots, never regenerates either output.
    comparison_hash = digest({"left":sides[0]["artifact_id"],"right":sides[1]["artifact_id"],
                              "left_timeline":sides[0]["timeline"],"right_timeline":sides[1]["timeline"]})
    page=changes[skip:skip+limit]
    visible_assets={value for item in page for value in (item["before_asset_id"],item["after_asset_id"]) if isinstance(value,str)}
    def summary(side):
        report=side["report"];timeline=side["timeline"]
        return {"artifact_id":side["artifact_id"],"revision_number":report["revision_number"],"review_status":report["status"],
            "can_preview":report["can_preview"],"reason":report["reason"],"duration":timeline["duration"],
            "resolution":timeline["resolution"],"fps":timeline["fps"],
            "tracks":[{"id":track["id"],"type":track["type"],"muted":track["muted"],"clips":len(track["clips"])} for track in timeline["tracks"]],
            "source_labels":{source["id"]:source["name"] for source in side["sources"] if source["id"] in visible_assets}}
    summaries=[summary(side) for side in sides]
    return {"left":summaries[0],"right":summaries[1],"comparison_hash":comparison_hash,"total_changes":len(changes),
            "changes":changes[skip:skip+limit],"skip":skip,"limit":limit,"has_more":skip+limit<len(changes),
            "sync_duration":min(side["timeline"]["duration"] for side in sides),
            "timing_note":"按成片时间比较；同一时间点不代表同一源镜头。"}
