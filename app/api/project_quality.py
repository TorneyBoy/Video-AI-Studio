"""Project-scoped human quality evidence; never a commercial/release approval."""
from hashlib import sha256
import re
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai_production import _write
from .deps import RequestIdentity, authenticate_request, require_tenant_id
from .identity import PrivateValidationRoute
from .storyboards import request_key
from .. import models
from ..database import get_db
from ..scenario_schemas import Digest, DimensionReview, Identifier, ScenarioBinding, ScenarioQualityReview
from ..services.ai_production import project_for
from ..services.editing import digest
from ..services.editing_errors import EditingError
from ..services.media_quality import artifact_quality_findings
from ..services.scenario_profiles import list_scenario_profiles, resolve_scenario_binding, scenario_binding
from ..services.scenario_quality import assess_scenario_quality


router = APIRouter(prefix="/projects/{project_id}/quality", tags=["project-quality"], route_class=PrivateValidationRoute)


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    artifact_id: str = Field(min_length=1, max_length=100)
    output_sha256: Digest
    scenario: ScenarioBinding
    rubric_version: Identifier
    dimensions: tuple[DimensionReview, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def frozen_rules(self):
        if not self.scenario.content_hash or not self.scenario.prompt_version:
            raise ValueError("review requires the exact scenario and prompt revision")
        if len({dimension.dimension for dimension in self.dimensions}) != len(self.dimensions):
            raise ValueError("each quality dimension may only be reviewed once")
        return self


def _object(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EditingError("quality_evidence_invalid", "已有质量证据格式无法核对，请先检查执行记录", 409)
    return value


def _artifact(db, tenant, project, id, *, lock=False):
    project_for(db, tenant, project)
    query = select(models.Artifact).where(models.Artifact.id == id, models.Artifact.tenant_id == tenant,
        models.Artifact.project_id == project, models.Artifact.kind != "scenario.quality_review")
    row = db.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if row is None:
        raise EditingError("quality_artifact_missing", "未找到本项目的媒体结果", 404)
    metadata = _object(row.metadata_json)
    metrics = _object(metadata.get("metrics"))
    values = [value for value in (metadata.get("output_sha256"), _object(metrics.get("media")).get("sha256"),
                                  _object(metadata.get("quality_control")).get("output_sha256")) if value is not None]
    if not values or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value) for value in values) or len({value.lower() for value in values}) != 1:
        raise EditingError("quality_media_unverified", "结果缺少可核对的实际文件哈希，请先完成媒体核验", 409)
    return row, values[0].lower()


def _read(row, current_sha=None):
    metadata = row.metadata_json
    return {"id": row.id, "artifact_id": metadata["artifact_id"], "review": metadata["review"],
        "assessment": metadata["assessment"], "stale": current_sha is not None and metadata["review"]["output_sha256"] != current_sha,
        "created_at": row.created_at}


def _reviewer(identity):
    return "reviewer-" + sha256(str(identity.principal_id or identity.user_id or "development_user").encode()).hexdigest()[:24]


def _request_id(tenant, project_id, key):
    return str(uuid5(NAMESPACE_URL, f"scenario-quality:{tenant}:{project_id}:{key}"))


def _frozen_scenario(db, row):
    if not row.job_id:
        return None
    job = db.scalar(select(models.Job).where(models.Job.id == row.job_id, models.Job.tenant_id == row.tenant_id,
        models.Job.project_id == row.project_id))
    payload = _object(job.payload) if job else {}
    snapshot = _object(_object(payload.get("ai_production")).get("snapshot"))
    timeline = _object(_object(_object(payload.get("manifest")).get("source")).get("timeline"))
    frozen_ai = snapshot.get("scenario")
    frozen_timeline = _object(timeline.get("metadata")).get("scenario")
    if frozen_ai and frozen_timeline and frozen_ai != frozen_timeline:
        raise EditingError("quality_scenario_mismatch", "任务中的场景版本不一致，请先核对执行记录", 409)
    frozen = frozen_ai or frozen_timeline
    if frozen:
        try:
            if frozen != scenario_binding(resolve_scenario_binding(frozen)):
                raise ValueError("scenario is not an exact frozen revision")
        except ValueError as exc:
            raise EditingError("quality_scenario_mismatch", "任务中的场景版本无法核对，请先检查执行记录", 409) from exc
    return frozen


def _measured_audio(metadata):
    sources = [_object(metadata.get("quality_control")), _object(_object(metadata.get("metrics")).get("media"))]
    values = [source["has_audio"] for source in sources if "has_audio" in source]
    if not values or any(type(value) is not bool for value in values) or len(set(values)) != 1:
        return None
    return values[0]


@router.get("/scenarios")
def scenarios(project_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    project_for(db, tenant, project_id)
    return {"profiles": list_scenario_profiles()}


@router.get("/artifacts/{artifact_id}")
def context(project_id: str, artifact_id: str, tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    row, output_sha = _artifact(db, tenant, project_id, artifact_id)
    frozen = _frozen_scenario(db, row)
    try:
        profile = resolve_scenario_binding(frozen) if frozen else None
        findings = artifact_quality_findings(row.metadata_json or {})
    except ValueError as exc:
        raise EditingError("quality_evidence_invalid", "场景或质量报告无法核对，请先检查执行记录", 409) from exc
    assets = list(db.scalars(select(models.Asset.id).where(models.Asset.tenant_id == tenant,
        models.Asset.project_id == project_id).order_by(models.Asset.id).limit(500)))
    reviews = list(db.scalars(select(models.Artifact).where(models.Artifact.tenant_id == tenant,
        models.Artifact.project_id == project_id, models.Artifact.kind == "scenario.quality_review",
        models.Artifact.metadata_json["artifact_id"].as_string() == row.id).order_by(models.Artifact.created_at.desc()).limit(100)))
    return {"artifact_id": row.id, "output_sha256": output_sha, "evidence_ids": [row.id, *assets],
        "scenario": scenario_binding(profile) if profile else None,
        "required_dimensions": list(profile.required_quality_dimensions) if profile else [],
        "has_audio": _measured_audio(row.metadata_json or {}), "technical_findings": findings,
        "reviews": [_read(review, output_sha) for review in reviews], "commercial_or_release_admission": False}


@router.post("/reviews", status_code=201)
def create_review(project_id: str, payload: ReviewCreate, key: str = Depends(request_key),
                  tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                  identity: RequestIdentity = Depends(authenticate_request)):
    actor = _reviewer(identity)
    request_hash = digest({"body": payload.model_dump(mode="json"), "actor": actor})
    id = _request_id(tenant, project_id, key)

    def write():
        row, output_sha = _artifact(db, tenant, project_id, payload.artifact_id, lock=True)
        previous = db.get(models.Artifact, id)
        if previous is not None:
            if previous.kind != "scenario.quality_review" or previous.metadata_json.get("request_hash") != request_hash:
                raise EditingError("quality_request_conflict", "原操作标识已用于另一份评审，请核对原结果", 409)
            return _read(previous, output_sha)
        if output_sha != payload.output_sha256:
            raise EditingError("quality_output_changed", "评审对象的实际文件已变化，请重新核对", 409)
        try:
            profile = resolve_scenario_binding(payload.scenario)
        except ValueError as exc:
            raise EditingError("quality_scenario_invalid", "场景版本已变化，请重新选择", 422) from exc
        measured_audio = _measured_audio(row.metadata_json or {})
        if any(dimension.dimension == "audio" and dimension.status == "not_applicable" for dimension in payload.dimensions) and measured_audio is not False:
            raise EditingError("quality_audio_unverified", "尚无可信的无音轨测量，不能将声音标为不适用", 422)
        frozen = _frozen_scenario(db, row)
        if frozen and frozen != scenario_binding(profile):
            raise EditingError("quality_scenario_mismatch", "评审场景与该成片实际执行版本不同", 409)
        evidence = {id for dimension in payload.dimensions for id in dimension.evidence_ids}
        valid = set(db.scalars(select(models.Asset.id).where(models.Asset.tenant_id == tenant,
            models.Asset.project_id == project_id, models.Asset.id.in_(evidence))))
        valid.update(db.scalars(select(models.Artifact.id).where(models.Artifact.tenant_id == tenant,
            models.Artifact.project_id == project_id, models.Artifact.id.in_(evidence))))
        if evidence - valid:
            raise EditingError("quality_evidence_missing", "质量证据不属于当前项目或已不存在", 422)
        review = ScenarioQualityReview(scenario=ScenarioBinding.model_validate(scenario_binding(profile)),
            output_sha256=output_sha, reviewer_id=actor, rubric_version=payload.rubric_version, dimensions=payload.dimensions)
        try:
            findings = artifact_quality_findings(row.metadata_json or {})
        except ValueError as exc:
            raise EditingError("quality_findings_invalid", "已有质量报告格式不完整，请先重新核验媒体", 409) from exc
        assessment = assess_scenario_quality(review, technical_findings=findings)
        result = models.Artifact(id=id, tenant_id=tenant, project_id=project_id, kind="scenario.quality_review",
            uri=f"quality-review://{id}", output_role="quality_evidence", metadata_json={"artifact_id": row.id,
                "request_hash": request_hash, "review": review.model_dump(mode="json"), "assessment": assessment})
        db.add(result)
        db.flush()
        return _read(result, output_sha)

    return _write(db, write)


@router.get("/reviews/requests/{key}")
def review_request(project_id: str, key: str = Path(min_length=1, max_length=255),
                   tenant: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                   identity: RequestIdentity = Depends(authenticate_request)):
    project_for(db, tenant, project_id)
    request_key(key)
    row = db.scalar(select(models.Artifact).where(models.Artifact.id == _request_id(tenant, project_id, key),
        models.Artifact.tenant_id == tenant, models.Artifact.project_id == project_id,
        models.Artifact.kind == "scenario.quality_review"))
    if row is None or (row.metadata_json.get("review") or {}).get("reviewer_id") != _reviewer(identity):
        return {"found": False, "result": None}
    try:
        _, current_sha = _artifact(db, tenant, project_id, row.metadata_json["artifact_id"])
    except EditingError:
        return {"found": True, "result": {**_read(row), "stale": True}}
    return {"found": True, "result": _read(row, current_sha)}
