"""Bind current quality evidence to human approval without inspecting media.

This gate never grants release or commercial rights. It adds prerequisites to
the existing exact-output approval and preserves its legacy hash when no new
quality evidence or scenario exists.
"""
from sqlalchemy import select

from .. import models
from ..scenario_schemas import ScenarioQualityReview
from .editing import digest
from .media_quality import artifact_quality_findings
from .scenario_profiles import resolve_scenario_binding, scenario_binding
from .scenario_quality import assess_scenario_quality


def latest_quality_review(db, row):
    return db.scalar(select(models.Artifact).where(models.Artifact.tenant_id == row.tenant_id,
        models.Artifact.project_id == row.project_id, models.Artifact.kind == "scenario.quality_review",
        models.Artifact.metadata_json["artifact_id"].as_string() == row.id)
        .order_by(models.Artifact.created_at.desc(), models.Artifact.id.desc()).limit(1))


def quality_evidence(row, payload, *, latest_review=None):
    """A pure metadata gate, also used by the bounded works-list projection."""
    binding = {}
    result = {"ready": True, "code": None, "reason": None, "binding": binding}

    def blocked(code, reason):
        result.update(ready=False, code=code, reason=reason)
        return result

    metadata = row.metadata_json or {}
    try:
        findings = artifact_quality_findings(metadata)
        metrics = metadata.get("metrics") or {}
        reports = {key: metrics[key] for key in ("render_quality_assessment", "renderer_quality_findings") if key in metrics and metrics[key] is not None}
        if reports:
            binding["reports_hash"] = digest(reports)
        if findings:
            binding["findings_hash"] = digest(findings)
        output_sha = (metadata.get("quality_control") or {}).get("output_sha256")
        media = metrics.get("media")
        if media is None:
            media = {}
        if not isinstance(media, dict):
            raise ValueError("invalid output media evidence")
        for other_sha in (metadata.get("output_sha256"), media.get("sha256")):
            if other_sha is not None and (not isinstance(other_sha, str) or not isinstance(output_sha, str) or other_sha.lower() != output_sha.lower()):
                raise ValueError("conflicting output hashes")
        frozen_ai = ((payload.get("ai_production") or {}).get("snapshot") or {}).get("scenario")
        frozen_timeline = ((((payload.get("manifest") or {}).get("source") or {}).get("timeline") or {}).get("metadata") or {}).get("scenario")
        frozen = frozen_ai or frozen_timeline
        if (frozen_ai is not None and not frozen_ai) or (frozen_timeline is not None and not frozen_timeline):
            raise ValueError("invalid scenario binding")
        if frozen:
            profile = resolve_scenario_binding(frozen)
            expected = scenario_binding(profile)
            # Scene-bound production must use an exact revision, not a mutable
            # id/version declaration that could silently change on recovery.
            if frozen != expected or (frozen_ai and frozen_timeline and frozen_ai != frozen_timeline):
                raise ValueError("unfrozen scenario")
            binding["scenario"] = expected
        else:
            expected = None
        if latest_review is not None:
            binding["review_id"] = latest_review.id
            value = latest_review.metadata_json
            review = ScenarioQualityReview.model_validate(value["review"])
            binding["review_hash"] = digest(review.model_dump(mode="json"))
            if value.get("artifact_id") != row.id:
                raise ValueError("different quality target")
            if not isinstance(output_sha, str) or review.output_sha256 != output_sha.lower():
                return blocked("artifact_scenario_review_stale", "场景评审对应了另一份输出，请核对当前成片并重新评审。")
            if expected and review.scenario.model_dump(mode="json") != expected:
                return blocked("artifact_scenario_review_stale", "场景评审与该成片的执行版本不一致，请重新评审。")
            if any(item.dimension == "audio" and item.status == "not_applicable" for item in review.dimensions):
                audio_facts = [source["has_audio"] for source in ((metadata.get("quality_control") or {}), media) if "has_audio" in source]
                if not audio_facts or any(value is not False for value in audio_facts):
                    return blocked("artifact_scenario_review_stale", "当前音轨测量不支持声音不适用的旧评审，请重新核对。")
            assessment = assess_scenario_quality(review, technical_findings=findings)
            binding["assessment_hash"] = digest(assessment)
            if assessment["verdict"] != "passed":
                return blocked("artifact_scenario_review_required", "当前场景质量评审尚未通过，请先处理失败或未审阅项。")
        elif expected:
            return blocked("artifact_scenario_review_required", "此成片已绑定场景，请先完成当前输出的分项质量评审。")
        if findings:
            return blocked("artifact_quality_findings_pending", "当前成片仍有质量问题或待复核项，请先处理对应报告。")
    except (ValueError, TypeError, KeyError, AttributeError):
        # Keep an invalid-evidence marker in the approval hash; do not let a
        # malformed report fall back to the no-evidence legacy path.
        binding["invalid_evidence"] = True
        return blocked("artifact_quality_evidence_invalid", "当前质量报告或场景证据无法核对，暂不能批准或领取。")
    return result
