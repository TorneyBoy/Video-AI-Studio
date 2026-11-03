"""Versioned evidence -> explained candidates -> human sourcing decisions.

All work here is metadata-only. It never generates, downloads or renders media.
Lock order is document -> selected assets in ID order; annotation locks only its
asset. Reports freeze evidence; changing inputs requires an explicit new report.
"""
from copy import deepcopy
from math import ceil, floor, isfinite

from pydantic import ValidationError
from sqlalchemy import select, update

from .. import models
from ..matching_schemas import RequirementOverride
from ..storyboard_schemas import StoryContent
from .analysis_contract import MediaAnalysisV2
from .editing import digest
from .editing_errors import EditingError
from .preparation_quotes import source_hash
from .shot_requirements import assess, storyboard_requirement
from .storyboards import _bounded, _same, document_for, revision_for


def asset_for(db, tenant, asset_id, *, lock=False):
    query = select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    asset = db.scalar(query)
    if asset is None:
        raise EditingError("matching_asset_missing", "未找到素材", 404)
    return asset


def semantic_head(db, asset_id):
    return db.scalar(select(models.AssetSemanticRevision).where(models.AssetSemanticRevision.asset_id == asset_id)
        .order_by(models.AssetSemanticRevision.number.desc()).limit(1))


def semantic_read(db, asset, row=None):
    head = semantic_head(db, asset.id)
    row = row or head
    return {"asset_id": asset.id, "revision_id": row.id if row else None,
            "head_revision_id": head.id if head else None, "number": row.number if row else 0,
            "source_current": row is None or row.source_hash == source_hash(asset),
            "content": row.content_json if row else None}


def media_facts(asset):
    meta = asset.metadata_json or {}
    analysis = None
    try:
        parsed = MediaAnalysisV2.model_validate(meta.get("analysis"))
        if parsed.source.uri == asset.uri:
            analysis = parsed
    except (ValidationError, ValueError, TypeError):
        pass
    value = analysis.media.duration_seconds if analysis else meta.get("duration")
    duration = value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0 else None
    orientation = None
    if analysis and analysis.video:
        w, h = analysis.video.width, analysis.video.height
        orientation = "square" if w == h else "portrait" if h > w else "landscape"
    return analysis, duration, orientation


def ready(asset):
    meta = asset.metadata_json or {}
    storage, upload = meta.get("storage") or {}, meta.get("upload") or {}
    return not (str(asset.uri).startswith("upload://") or upload.get("status") == "created" or
        storage.get("mode") == "external_presigned" and storage.get("status") != "ready")


def save_semantics(db, tenant, asset_id, request, key, actor):
    asset = asset_for(db, tenant, asset_id, lock=True)
    fingerprint = digest(request.model_dump(mode="json"))
    previous = db.scalar(select(models.AssetSemanticRevision).where(models.AssetSemanticRevision.asset_id == asset.id,
        models.AssetSemanticRevision.request_key == key))
    if previous:
        return semantic_read(db, asset, _same(previous, fingerprint))
    head = semantic_head(db, asset.id)
    if (head.id if head else None) != request.base_revision_id:
        raise EditingError("semantic_revision_conflict", "内容标注已有更新；请保留输入并载入当前版本")
    kind = asset.kind.split("/", 1)[0]
    if kind not in {"video", "image"} or not ready(asset):
        raise EditingError("semantic_asset_not_ready", "请先完成视频或图片的上传核验", 422)
    _, duration, _ = media_facts(asset)
    spans = sorted((row for row in request.spans if row.start_ms is not None), key=lambda row: (row.start_ms, row.end_ms))
    if sum(row.start_ms is None for row in request.spans) > 1:
        raise EditingError("semantic_range_ambiguous", "整条素材只能有一条基础标注", 422)
    if kind == "image" and spans:
        raise EditingError("semantic_image_range", "图片使用整条标注，不填写源时间段", 422)
    if any(a.end_ms > b.start_ms for a, b in zip(spans, spans[1:])):
        raise EditingError("semantic_range_overlap", "细分标注不能互相重叠，可使用一条整片基础标注", 422)
    if duration is not None and any(row.end_ms > floor(duration * 1000) for row in spans):
        raise EditingError("semantic_range_out_of_bounds", "标注区间超过素材时长，请显式调整", 422)
    content = {"schema_version": "user-shot-semantics/1", "spans": [row.model_dump(mode="json") for row in request.spans],
        "note": request.note, "provenance": {"kind": "user_confirmed", "actor_id": actor, "not_model_inference": True}}
    _bounded(content)
    row = models.AssetSemanticRevision(tenant_id=tenant, asset_id=asset.id, number=head.number + 1 if head else 1,
        request_key=key, request_hash=fingerprint, source_hash=source_hash(asset), content_json=content, actor_id=actor)
    db.add(row); db.flush()
    return semantic_read(db, asset, row)


def input_info(db, asset):
    semantic = semantic_head(db, asset.id)
    usage = db.get(models.ReferenceUsage, asset.id)
    annotations = semantic.content_json["spans"] if semantic and semantic.source_hash == source_hash(asset) else []
    meta = asset.metadata_json or {}
    storage = meta.get("storage") or {}
    identity = {"uri": asset.uri, "version": storage.get("version_id") or storage.get("object_version")}
    if storage.get("status") == "ready" and isinstance(storage.get("sha256"), str) and len(storage["sha256"]) == 64:
        identity = {"sha256": storage["sha256"], "size_bytes": storage.get("size_bytes")}
    fingerprint = digest({"source": source_hash(asset), "analysis": meta.get("analysis"),
                          "semantic_revision_id": semantic.id if semantic else None, "tags": meta.get("user_tags") or []})
    return {"asset_id": asset.id, "fingerprint": fingerprint,
            "annotation_revision_id": semantic.id if semantic else None,
            "output_allowed": not usage or usage.allow_output, "ready": ready(asset), "source_group": digest(identity)}, annotations


def _windows(asset, annotations):
    kind = asset.kind.split("/", 1)[0]
    analysis, duration, orientation = media_facts(asset)
    if kind == "image":
        base = [(0, None, {})]
    elif kind == "video" and duration:
        base = [(max(0, ceil(row.start * 1000)), min(floor(duration * 1000), floor(row.end * 1000)), row.model_dump())
                for row in analysis.shots] if analysis and analysis.shots else [(0, floor(duration * 1000), {})]
    else:
        return []
    if len(base) > 5000:
        raise EditingError("matching_input_limit", "素材区间超过5000个，请显式缩小选择", 422)
    indexed = {}
    for start, end, metadata in base:
        if end is not None and end <= start:
            continue
        indexed[(start, end)] = metadata
        for annotation in annotations:
            if end is None or annotation["start_ms"] is None:
                continue
            first, last = max(start, annotation["start_ms"]), min(end, annotation["end_ms"])
            if last > first:
                indexed[(first, last)] = metadata
            if len(indexed) > 5000:
                raise EditingError("matching_input_limit", "素材区间超过5000个，请显式缩小选择", 422)
    result = []
    for (start, end), metadata in sorted(indexed.items(), key=lambda row: (row[0][0], row[0][1] or 0)):
        fitting = [row for row in annotations if row["start_ms"] is None or end is not None
                   and row["start_ms"] <= start and row["end_ms"] >= end]
        fitting.sort(key=lambda row: (row["end_ms"] - row["start_ms"] if row["start_ms"] is not None else float("inf"), row["id"]))
        annotation = fitting[0] if fitting else None
        visual = {}
        tags = list((asset.metadata_json or {}).get("user_tags") or [])
        for evidence in reversed(fitting):
            visual.update({key: value for key, value in evidence["visual"].items() if value.strip()})
            tags.extend(evidence["tags"])
        result.append({"asset_id": asset.id, "name": asset.name, "kind": kind, "start_ms": start, "end_ms": end,
            "orientation": orientation, "metadata": metadata, "measured": analysis is not None,
            "visual": visual, "tags": list(dict.fromkeys(tags)),
            "annotation_span_id": annotation["id"] if annotation else None})
    return result


def report_for(db, document, report_id):
    row = db.scalar(select(models.ShotMatchReport).where(models.ShotMatchReport.id == report_id,
        models.ShotMatchReport.document_id == document.id, models.ShotMatchReport.tenant_id == document.tenant_id))
    if row is None:
        raise EditingError("match_report_missing", "未找到本分镜的候选报告", 404)
    return row


def _inputs_current(db, document, inputs):
    rows = list(db.scalars(select(models.Asset).where(models.Asset.tenant_id == document.tenant_id,
        models.Asset.project_id == document.project_id, models.Asset.id.in_([row["asset_id"] for row in inputs]))
        .execution_options(populate_existing=True)))
    current = {row.id: input_info(db, row)[0] for row in rows}
    return all(current.get(row["asset_id"]) == row for row in inputs)


def report_read(db, document, report, *, detail=True):
    stale = document.head_revision_id != report.story_revision_id or not _inputs_current(db, document, report.inputs_json)
    result = report.result_json if detail else {key: report.result_json[key] for key in ("summary", "asset_count")}
    return {"id": report.id, "document_id": document.id, "story_revision_id": report.story_revision_id,
            "stale_inputs": stale, "created_at": report.created_at, "result": result}


def create_report(db, tenant, document_id, request, key, actor):
    document = document_for(db, tenant, document_id, lock=True)
    payload = request.model_dump(mode="json"); payload["asset_ids"] = sorted(payload["asset_ids"])
    fingerprint = digest(payload)
    previous = db.scalar(select(models.ShotMatchReport).where(models.ShotMatchReport.document_id == document.id,
        models.ShotMatchReport.request_key == key))
    if previous:
        return report_read(db, document, _same(previous, fingerprint))
    if document.head_revision_id != request.story_revision_id:
        raise EditingError("matching_story_changed", "分镜已有新版本，请载入当前版本后匹配")
    revision = revision_for(db, document, request.story_revision_id)
    content = StoryContent.model_validate(revision.content_json).model_dump(mode="json")
    if set(request.overrides) - {row["id"] for row in content["shots"]}:
        raise EditingError("matching_shot_missing", "需求覆盖含其他分镜的镜头", 422)
    assets = list(db.scalars(select(models.Asset).where(models.Asset.tenant_id == tenant,
        models.Asset.project_id == document.project_id, models.Asset.id.in_(request.asset_ids)).order_by(models.Asset.id)))
    if len(assets) != len(request.asset_ids):
        raise EditingError("matching_asset_missing", "所选素材不存在或不属于当前项目", 404)
    inputs, windows, excluded = [], [], []
    for asset in assets:
        info, annotations = input_info(db, asset); inputs.append(info)
        if not info["ready"] or not info["output_allowed"]:
            excluded.append({"asset_id": asset.id, "name": asset.name, "reason": "upload_incomplete" if not info["ready"] else "reference_only"})
            continue
        source_windows = _windows(asset, annotations)
        if not source_windows:
            excluded.append({"asset_id": asset.id, "name": asset.name, "reason": "media_facts_unavailable"})
        for window in source_windows:
            window["input"] = info
        windows.extend(source_windows)
        if len(windows) > 5000:
            raise EditingError("matching_input_limit", "素材区间超过5000个，请显式缩小选择", 422)
    report_id = models.new_id(); items = []; recommended_assets = set(); last_asset = None
    for shot in content["shots"]:
        override = request.overrides.get(shot["id"], RequirementOverride())
        requirement = storyboard_requirement(shot, content["facts"], override)
        snapshot = requirement.snapshot(); requirement_hash = digest(snapshot)
        candidates, counts = [], {}
        for window in windows:
            assessment = assess(requirement, kind=window["kind"],
                duration=(window["end_ms"] - window["start_ms"]) / 1000 if window["end_ms"] is not None else None,
                orientation=window["orientation"], metadata=window["metadata"], visual=window["visual"],
                tags=window["tags"], measured=window["measured"])
            for code in assessment["rejected"]:
                counts[code] = counts.get(code, 0) + 1
            if assessment["rejected"]:
                continue
            end = window["start_ms"] + shot["duration_ms"] if window["kind"] == "video" else None
            candidate_id = digest({"report_id": report_id, "shot": shot["id"], "asset": window["asset_id"],
                                   "start": window["start_ms"], "end": end, "input": window["input"]["fingerprint"]})
            candidates.append({"id": candidate_id, "asset_id": window["asset_id"], "name": window["name"], "kind": window["kind"],
                "start_ms": window["start_ms"], "end_ms": end, "available_end_ms": window["end_ms"],
                "requirement_hash": requirement_hash, "input_fingerprint": window["input"]["fingerprint"],
                "source_group": window["input"]["source_group"],
                "annotation_revision_id": window["input"]["annotation_revision_id"], "annotation_span_id": window["annotation_span_id"],
                "semantic_provenance": "user_confirmed" if window["annotation_span_id"] else "unavailable",
                "observed_visual": window["visual"], "observed_tags": window["tags"],
                "duration_provenance": "hosted_media" if window["measured"] else "declared" if window["kind"] == "video" else "image_hold",
                "verdict": assessment["verdict"], "reasons": assessment["reasons"], "review": assessment["review"],
                "diversity": {"already_recommended_source": window["input"]["source_group"] in recommended_assets,
                              "adjacent_recommended_source": window["input"]["source_group"] == last_asset}})
        # Prefer supported evidence, then diversity; this is a proposal order,
        # never an automatic adoption and never a claimed similarity score.
        candidates.sort(key=lambda row: (row["verdict"] != "supported", len(row["review"]),
            row["diversity"]["adjacent_recommended_source"], row["diversity"]["already_recommended_source"],
            row["asset_id"], row["start_ms"], row["id"]))
        unique = {}
        for candidate in candidates:
            unique.setdefault(candidate["id"], candidate)
        candidates = list(unique.values())[:request.candidates_per_shot]
        if candidates:
            last_asset = candidates[0]["source_group"]; recommended_assets.add(last_asset)
        items.append({"shot_id": shot["id"], "requirement": snapshot, "requirement_hash": requirement_hash,
            "candidates": candidates, "excluded_counts": counts,
            "gap": "no_eligible_material" if not candidates else "visual_review_required" if all(row["review"] for row in candidates) else "awaiting_adoption",
            "reshoot": {"duration_ms": shot["duration_ms"], "intent": shot["intent"], "visual": shot["visual"],
                        "constraints": shot["constraints"], "facts": [row["text"] for row in content["facts"] if row["confirmed"]]}})
    result = {"schema_version": "shot-match-report/1", "configuration": payload, "items": items, "excluded_assets": excluded, "asset_count": len(assets),
        "summary": {"shots": len(items), "with_candidates": sum(bool(row["candidates"]) for row in items),
                    "without_candidates": sum(not row["candidates"] for row in items)},
        "notice": "候选排序不代表采用；用户标注不是模型识别，未验证条件需人工核对。"}
    _bounded(result)
    row = models.ShotMatchReport(id=report_id, tenant_id=tenant, document_id=document.id, story_revision_id=revision.id,
        request_key=key, request_hash=fingerprint, inputs_json=inputs, result_json=result, actor_id=actor)
    db.add(row); db.flush()
    return report_read(db, document, row)


def source_head(db, document):
    head = db.get(models.StoryboardSourceHead, document.id, populate_existing=True)
    return db.get(models.StoryboardSourceRevision, head.revision_id) if head else None


def source_read(db, document, row=None):
    head = source_head(db, document); row = row or head
    if row is None:
        return {"document_id": document.id, "revision_id": None, "head_revision_id": None, "number": 0, "content": None,
                "story_revision_id": document.head_revision_id, "story_current": True, "inputs_current": True}
    current = True
    for choice in row.content_json["choices"]:
        binding = choice.get("binding")
        if not binding:
            continue
        asset = db.scalar(select(models.Asset).where(models.Asset.id == binding["asset_id"],
            models.Asset.tenant_id == document.tenant_id, models.Asset.project_id == document.project_id)
            .execution_options(populate_existing=True))
        info = input_info(db, asset)[0] if asset else None
        if not info or not info["ready"] or not info["output_allowed"] or info["fingerprint"] != binding["input_fingerprint"]:
            current = False
    return {"document_id": document.id, "revision_id": row.id, "head_revision_id": head.id if head else None,
            "story_revision_id": row.story_revision_id, "story_current": document.head_revision_id == row.story_revision_id,
            "number": row.number, "content": row.content_json, "created_at": row.created_at, "inputs_current": current}


def save_sources(db, tenant, document_id, request, key, actor):
    document = document_for(db, tenant, document_id, lock=True)
    fingerprint = digest(request.model_dump(mode="json"))
    previous = db.scalar(select(models.StoryboardSourceRevision).where(models.StoryboardSourceRevision.document_id == document.id,
        models.StoryboardSourceRevision.request_key == key))
    if previous:
        return source_read(db, document, _same(previous, fingerprint))
    head = source_head(db, document)
    if (head.id if head else None) != request.base_revision_id:
        raise EditingError("sourcing_revision_conflict", "素材采用记录已有更新，请载入当前版本核对")
    report = report_for(db, document, request.report_id)
    if document.head_revision_id != report.story_revision_id:
        raise EditingError("matching_story_changed", "候选来自旧分镜版本，请为当前版本重新匹配")
    # Serialize concurrent label/source changes before accepting frozen inputs.
    asset_ids = sorted({row["asset_id"] for row in report.inputs_json} | {
        row["binding"]["asset_id"] for row in (head.content_json["choices"] if head else []) if row.get("binding")})
    assets = list(db.scalars(select(models.Asset).where(models.Asset.tenant_id == tenant,
        models.Asset.project_id == document.project_id, models.Asset.id.in_(asset_ids)).order_by(models.Asset.id)
        .with_for_update().execution_options(populate_existing=True)))
    by_asset = {row.id: row for row in assets}
    if not _inputs_current(db, document, report.inputs_json):
        raise EditingError("matching_inputs_changed", "素材、内容标注或参考用途已变化，请明确重新匹配")
    items = {row["shot_id"]: row for row in report.result_json["items"]}
    if {row.shot_id for row in request.choices} != set(items):
        raise EditingError("sourcing_choices_incomplete", "请为当前分镜的每个镜头保留一个明确决定", 422)
    old = {row["shot_id"]: row for row in head.content_json["choices"]} if head else {}
    unlocks = set(request.unlock_shot_ids)
    old_locks = {key for key, row in old.items() if row["locked"]}
    if unlocks - old_locks:
        raise EditingError("sourcing_unlock_invalid", "只能明确解锁已有的素材选择", 422)
    if old_locks - set(items) - unlocks:
        raise EditingError("sourcing_locked", "新分镜移除了锁定素材的镜头；须明确解锁后才能移除")
    choices = []
    for choice in request.choices:
        item = items[choice.shot_id]; before = old.get(choice.shot_id); binding = None
        if choice.mode == "candidate":
            candidate = next((row for row in item["candidates"] if row["id"] == choice.candidate_id), None)
            if candidate is None:
                raise EditingError("sourcing_candidate_missing", "候选不属于此镜头和报告", 422)
            if candidate["review"] and (not choice.reviewed or not choice.note.strip()):
                raise EditingError("sourcing_review_required", "请核对未验证条件，并填写采用依据", 422)
            binding = {key: deepcopy(candidate[key]) for key in ("id", "asset_id", "name", "kind", "start_ms", "end_ms",
                "input_fingerprint", "annotation_revision_id", "annotation_span_id", "semantic_provenance", "duration_provenance", "source_group")}
            binding.update(report_id=report.id, reviewed=choice.reviewed, note=choice.note, actor_id=actor)
        elif choice.mode == "keep":
            if not before or not before.get("binding") or before["requirement_hash"] != item["requirement_hash"]:
                raise EditingError("sourcing_keep_incompatible", "原素材不再对应当前需求，请明确解锁并重新选择")
            binding = deepcopy(before["binding"])
            asset = by_asset.get(binding["asset_id"])
            if not asset or input_info(db, asset)[0]["fingerprint"] != binding["input_fingerprint"] or not input_info(db, asset)[0]["output_allowed"]:
                raise EditingError("sourcing_keep_changed", "原素材或标注已变化，不能沿用旧依据")
        mode = "material" if binding else choice.mode
        if choice.shot_id in old_locks and choice.shot_id not in unlocks:
            if not choice.locked or not binding or binding != before["binding"] or item["requirement_hash"] != before["requirement_hash"]:
                raise EditingError("sourcing_locked", "锁定的素材和依据不能被替换；请先明确解锁")
        if choice.shot_id in unlocks and choice.locked:
            raise EditingError("sourcing_unlock_invalid", "解锁后的素材不能仍标记为锁定", 422)
        choices.append({"shot_id": choice.shot_id, "requirement_hash": item["requirement_hash"],
                        "mode": mode, "locked": choice.locked, "binding": binding, "note": choice.note})
    used = []; overlaps = []
    for choice in choices:
        binding = choice["binding"]
        if not binding:
            continue
        for other_id, other in used:
            if other["source_group"] == binding["source_group"] and (binding["kind"] == "image" or other["kind"] == "image" or
                    binding["start_ms"] < other["end_ms"] and other["start_ms"] < binding["end_ms"]):
                overlaps.append([other_id, choice["shot_id"]])
        used.append((choice["shot_id"], binding))
    if overlaps and not request.allow_reuse:
        raise EditingError("sourcing_reuse_confirmation", "有镜头重复使用同一图片或重叠区间；请改选素材或明确允许重复", 422)
    content = {"schema_version": "storyboard-sourcing/1", "choices": choices, "allow_reuse": request.allow_reuse,
               "reused_intervals": overlaps, "all_material": all(row["mode"] == "material" for row in choices),
               "notice": "素材采用记录已保存；还需编译时间线及渲染审核，不能当作已交付成片。"}
    _bounded(content)
    row = models.StoryboardSourceRevision(tenant_id=tenant, document_id=document.id, story_revision_id=report.story_revision_id,
        report_id=report.id, parent_revision_id=head.id if head else None, number=head.number + 1 if head else 1,
        request_key=key, request_hash=fingerprint, content_json=content, actor_id=actor)
    db.add(row); db.flush()
    if head:
        changed = db.execute(update(models.StoryboardSourceHead).where(models.StoryboardSourceHead.document_id == document.id,
            models.StoryboardSourceHead.revision_id == head.id).values(revision_id=row.id))
        if changed.rowcount != 1:
            raise EditingError("sourcing_revision_conflict", "素材采用记录已有更新")
    else:
        db.add(models.StoryboardSourceHead(document_id=document.id, revision_id=row.id))
    db.flush()
    return source_read(db, document, row)


def reshoot_manifest(db, document):
    head = source_head(db, document)
    if not head or head.story_revision_id != document.head_revision_id:
        raise EditingError("sourcing_not_current", "请先为当前分镜保存素材采用或补拍决定")
    if not source_read(db, document, head)["inputs_current"]:
        raise EditingError("sourcing_inputs_changed", "已采用的素材或标注已变化，请重新匹配并核对清单")
    report = report_for(db, document, head.report_id)
    by_shot = {row["shot_id"]: row for row in report.result_json["items"]}
    items = [{"shot_id": row["shot_id"], "decision": row["mode"], "note": row["note"],
              **by_shot[row["shot_id"]]["reshoot"]} for row in head.content_json["choices"] if row["mode"] != "material"]
    lines = ["补拍 / 待补素材清单", f"分镜版本：{head.story_revision_id}", f"素材决定版本：{head.id}"]
    labels = {"subject": "主体", "action": "动作", "scene": "场景", "composition": "构图", "camera": "运镜", "lighting": "光影", "style": "风格"}
    for index, item in enumerate(items, 1):
        lines.extend(["", f"{index}. {item['shot_id']} · {item['duration_ms'] / 1000:g}秒", "意图：" + item["intent"]])
        lines.extend(labels[key] + "：" + value for key, value in item["visual"].items() if value)
        lines.extend("必须保留：" + value for value in item["constraints"] + item["facts"])
        if item["note"]:
            lines.append("备注：" + item["note"])
    return {"schema_version": "storyboard-reshoot/1", "document_id": document.id, "story_revision_id": head.story_revision_id,
            "source_revision_id": head.id, "items": items, "text": "\n".join(lines), "provider_requests": 0}
