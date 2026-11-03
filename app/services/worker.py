"""Local worker for queued media jobs and durable workflow runs.

The same Job/WorkflowRun contract can be driven by the optional Temporal
adapter, while this poller keeps local deployments executable without an
external orchestration service.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from timeline_engine import RenderManifest, TemplateDefinition, Timeline

from .. import models
from ..config import get_settings
from .jobs import create_step, reset_step_for_retry, schedule_retry, transition_step
from .dispatch import DispatchDeferred,claims,claim_next,defer_execution,family_for,FAMILIES
from .completion import (ExecutionSuperseded, accept_worker_result, execution_ticket,
                         finish_job, lock_execution, prepare_provider_call, record_provider_failure, render_revision_binding)
from .budgets import BudgetDataError, BudgetExceededError, record_usage_evidence, settle_provider_cost
from .auto_edit import AutoEditPlanner, compile_auto_edit_timeline, shot_features_from_assets
from .media_analysis import MediaAnalysisProvider
from .analysis_contract import require_planning_analysis, validate_hosted_analysis
from .preparation_quotes import plan_assets, validate_analysis_source
from .editing import EditingError, assert_render_sources, initialize_document, validate_render_revision
from .media_quality import validate_render_quality
from .archive_worker import process_archive_tasks
from .providers import ProviderExecutionError, ProviderRequest, ProviderRouter, default_provider_router
from .renderer import RenderProfile, build_ffmpeg_command, probe_render_output
from .workflows import (
    WorkflowError,
    WorkflowLeaseLost,
    claim_workflow,
    create_workflow,
    heartbeat_workflow,
    append_event,
    retry_workflow,
)
from .remote_uri import RemoteURIError, validate_remote_uri


def _remote_asset_uri(value: str, *, capability: str, field: str) -> str:
    """Validate that an external provider can fetch an input asset."""
    try:
        return validate_remote_uri(value, field=field)
    except RemoteURIError as exc:
        raise ProviderExecutionError(
            str(exc),
            code="remote_asset_uri_required",
            capability=capability,
            retryable=False,
            details={"field": field},
        ) from exc


def _claim_job_workflow(
    db: Session,
    job: models.Job,
    *,
    retry_deadline: datetime | None = None,
) -> models.WorkflowRun | None:
    """Claim the durable run that owns a Job before doing media work.

    Job remains the public queue primitive, while WorkflowRun owns worker
    leases and recovery.  Keeping both claims in the same transaction means a
    worker that loses a workflow race also rolls back its Job claim.
    """
    settings = get_settings()
    worker_id = settings.workflow_worker_id.strip()
    if not worker_id:
        raise WorkflowError("WORKFLOW_WORKER_ID must not be empty")
    run = db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job.id))
    if run is None:
        run = create_workflow(
            db,
            job,
            steps=[{"name": "job.execute", "sequence": 0, "input_json": dict(job.payload or {})}],
        )
    # A Temporal (or another external runtime) run is owned by that runtime;
    # the database poller must leave it queued for the corresponding adapter.
    if run.runtime != "local":
        return None
    # Job is the legacy scheduler's source of truth for backoff.  Operators
    # and older schedulers may move its due time directly, so mirror an
    # earlier Job deadline before applying the durable workflow gate.
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    job_retry_at = _as_utc(retry_deadline or job.next_retry_at) if (retry_deadline or job.next_retry_at) else None
    run_retry_at = _as_utc(run.next_retry_at) if run.next_retry_at else None
    if run.status == "queued" and job_retry_at and (run_retry_at is None or job_retry_at < run_retry_at):
        # ``claim_job`` clears Job.next_retry_at atomically.  Keep the
        # original due timestamp captured by the caller when mirroring it to
        # WorkflowRun, including SQLite's naive datetime representation.
        run.next_retry_at = job_retry_at
        db.flush()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lease_is_active = bool(run.lease_expires_at and _as_utc(run.lease_expires_at) > now)
    if run.status == "running" and run.worker_id == worker_id and lease_is_active:
        # A process can see its own run after a retry or a nested poll.  A
        # heartbeat keeps the ownership valid without incrementing attempts.
        try:
            heartbeat_workflow(
                db,
                run,
                worker_id=worker_id,
                lease_seconds=settings.workflow_lease_seconds,
            )
            return run
        except WorkflowLeaseLost:
            return None
    if run.status != "queued":
        return None
    if not claim_workflow(
        db,
        run,
        worker_id=worker_id,
        lease_seconds=settings.workflow_lease_seconds,
    ):
        return None
    return run


def _complete_job_workflow(
    db: Session,
    run: models.WorkflowRun,
    status: str,
    *,
    result_json: dict | None = None,
    error: str | None = None,
    failure_code: str | None = None,
) -> None:
    job = db.get(models.Job, run.job_id)
    finish_job(db, job, status, result_json=result_json, error=error, failure_code=failure_code)



def _workflow_step(db: Session, run: models.WorkflowRun) -> models.WorkflowStep:
    """Return the activity row owned by the built-in single-activity worker."""
    step = db.scalar(
        select(models.WorkflowStep)
        .where(models.WorkflowStep.workflow_run_id == run.id)
        .order_by(models.WorkflowStep.sequence, models.WorkflowStep.created_at)
    )
    if step is None:
        step = models.WorkflowStep(
            tenant_id=run.tenant_id,
            workflow_run_id=run.id,
            name="job.execute",
            sequence=0,
            max_attempts=run.max_attempts,
            input_json=dict(run.input_json or {}),
        )
        db.add(step)
        db.flush()
    return step


def _start_workflow_step(db: Session, run: models.WorkflowRun) -> models.WorkflowStep:
    settings = get_settings()
    step = _workflow_step(db, run)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    step.status = "running"
    step.worker_id = settings.workflow_worker_id
    step.lease_expires_at = now + timedelta(seconds=settings.workflow_lease_seconds)
    step.heartbeat_at = now
    step.attempt_count += 1
    step.started_at = step.started_at or now
    append_event(db, run, "workflow.step.claimed", worker_id=settings.workflow_worker_id, payload={"step_id": step.id, "step": step.name})
    db.flush()
    return step


def _finish_workflow_step(
    db: Session,
    run: models.WorkflowRun,
    *,
    status: str,
    result_json: dict | None = None,
    error: str | None = None,
    retry: bool = False,
) -> None:
    settings = get_settings()
    step = _workflow_step(db, run)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if retry:
        step.status = "queued"
        step.progress = 0.0
        step.completed_at = None
    else:
        step.status = status
        step.progress = 1.0 if status == "succeeded" else step.progress
        step.completed_at = now
    step.worker_id = None
    step.lease_expires_at = None
    step.heartbeat_at = now
    step.result_json = result_json or {}
    step.error = error
    append_event(
        db,
        run,
        "workflow.step.retry_scheduled" if retry else f"workflow.step.{status}",
        worker_id=settings.workflow_worker_id,
        payload={"step_id": step.id, "step": step.name},
    )
    db.flush()


def _retry_job_workflow(
    db: Session,
    run: models.WorkflowRun,
    *,
    error: str,
    failure_code: str | None,
    delay_seconds: float,
) -> None:
    settings = get_settings()
    retry_workflow(
        db,
        run,
        worker_id=settings.workflow_worker_id,
        error=error,
        failure_code=failure_code,
        delay_seconds=delay_seconds,
    )


def _get_or_create_step(db: Session, job: models.Job, *, name: str, sequence: int) -> models.JobStep:
    step = db.scalar(select(models.JobStep).where(models.JobStep.job_id == job.id, models.JobStep.name == name))
    if step is None:
        step = create_step(db, job, name=name, sequence=sequence)
        db.flush()
    return step


def _api_only_media_execution() -> bool:
    """Return whether media analysis/render must remain outside this process.

    ``PROVIDER_MODE`` is the single compute policy switch.  Keeping this
    decision in the worker makes it impossible for a production worker to
    silently fall back to ffprobe/ffmpeg when an external model provider is
    unavailable.
    """

    return get_settings().provider_mode == "api_only"


def _external_analysis_request(job: models.Job, asset: models.Asset, options: dict) -> ProviderRequest:
    provider_job_id = (job.payload or {}).get("provider_job_id")
    capability = (job.payload or {}).get("capability_snapshot") or {}
    frozen = (job.payload or {}).get("analysis_input") or {}
    asset_uri = _remote_asset_uri(frozen.get("uri", asset.uri), capability="media_analysis", field="asset_uri")
    return ProviderRequest(
        request_id=job.id,
        capability="media_analysis",
        provider=capability.get("provider"),
        model_id=capability.get("model_id"),
        model_version=capability.get("model_version"),
        inputs={
            "asset_id": asset.id,
            "asset_uri": asset_uri,
            "uri": asset_uri,
            "kind": frozen.get("kind", asset.kind),
            "sha256": frozen.get("sha256") if frozen else (asset.metadata_json or {}).get("storage", {}).get("sha256"),
        },
        parameters={
            **dict(options or {}),
            **({"provider_job_id": str(provider_job_id)} if provider_job_id else {}),
        },
        tenant_id=job.tenant_id,
        commercial_use=True,
    )


def _external_analysis_payload(result) -> dict:
    """Extract normalized analysis returned by an external API.

    The control plane never downloads provider output.  Analysis providers
    therefore must return the compact JSON needed by planning inline in the
    result envelope (``provenance.analysis`` or ``metrics.analysis``).
    """

    for container in (getattr(result, "provenance", None), getattr(result, "metrics", None)):
        if isinstance(container, dict):
            value = container.get("analysis") or container.get("result")
            if isinstance(value, dict):
                return value
    raise ProviderExecutionError(
        "external media analysis must return an inline analysis object",
        code="provider_analysis_missing",
        capability="media_analysis",
        retryable=False,
    )


def _external_render_request(job: models.Job, manifest: RenderManifest, quality: str) -> ProviderRequest:
    manifest_json = manifest.model_dump(mode="json")
    payload = job.payload or {}
    provider_job_id = payload.get("provider_job_id")
    capability = payload.get("capability_snapshot") or {}
    asset_uris = [
        _remote_asset_uri(asset.uri, capability="video_editing", field=f"asset_uris[{index}]")
        for index, asset in enumerate(manifest.assets)
        if asset.uri
    ]
    return ProviderRequest(
        request_id=job.id,
        capability="video_editing",
        provider=capability.get("provider"),
        model_id=capability.get("model_id"),
        model_version=capability.get("model_version"),
        inputs={
            "manifest": manifest_json,
            "timeline": manifest.source.get("timeline"),
            "asset_uris": asset_uris,
        },
        parameters={
            "quality": quality,
            "manifest_id": manifest.manifest_id,
            "content_hash": manifest.content_hash,
            **({"provider_job_id": str(provider_job_id)} if provider_job_id else {}),
        },
        tenant_id=job.tenant_id,
        commercial_use=True,
    )


def _provider_model_metadata(result) -> dict:
    model = result.model
    return {
        "provider": model.provider,
        "model": model.as_dict(),
        "metrics": dict(result.metrics),
        "provenance": dict(result.provenance),
        "usage": dict(result.usage),
        "cost": dict(result.cost),
        "status": result.status,
        "warnings": list(result.warnings),
    }


def _settle_provider_budget(db: Session, job: models.Job, result, *, source: str = "provider_result") -> None:
    """Persist one final provider charge and stop the job when it is over budget."""

    operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job.id))
    if operation is not None:
        operation.state = "result_received"
        operation.result_json = {"artifact_uri": result.artifact_uri, "artifact_type": result.artifact_type}
        if result.provenance.get("provider_job_id"):
            operation.external_job_id = str(result.provenance["provider_job_id"])
    decision = settle_provider_cost(
        db,
        job=job,
        cost=result.cost,
        usage=result.usage,
        provider=result.model.provider,
        model_id=result.model.model_id,
        model_version=result.model.version,
        source=source,
    )
    if not decision.allowed:
        raise BudgetExceededError(decision)


def _schedule_external_retry(
    db: Session,
    job: models.Job,
    workflow: models.WorkflowRun,
    step: models.JobStep,
    *,
    error: str,
    failure_code: str,
    delay_seconds: float,
) -> None:
    """Put an external media call back on the durable queue."""

    transition_step(db, step, "failed", error=error)
    reset_step_for_retry(db, step, error=error)
    schedule_retry(db, job, error=error, failure_code=failure_code, delay_seconds=delay_seconds)
    _finish_workflow_step(db, workflow, status="failed", error=error, retry=True)
    _retry_job_workflow(db, workflow, error=error, failure_code=failure_code, delay_seconds=delay_seconds)


def _capture_failed_result_charge(db: Session, job: models.Job, result) -> None:
    # Domain writes were rolled back, but an observed vendor charge is still
    # evidence. Invalid monetary data is held for reconciliation, not zeroed.
    operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job.id))
    if operation is not None:
        operation.state = "result_received"
        if result.provenance.get("provider_job_id"):
            operation.external_job_id = str(result.provenance["provider_job_id"])
    try:
        settle_provider_cost(db, job=job, cost=result.cost, usage=result.usage,
                             provider=result.model.provider, model_id=result.model.model_id,
                             model_version=result.model.version,
                             source="worker_failure_result")
    except BudgetDataError:
        record_usage_evidence(db, job=job, cost=result.cost, usage=result.usage,
                              provider=result.model.provider, source="worker_failure_result", disposition="invalid_amount")


def process_queued_analysis_jobs(
    db: Session,
    *,
    limit: int = 10,
    provider: MediaAnalysisProvider | None = None,
    router: ProviderRouter | None = None,
    _claims=None,
) -> list[dict[str, str]]:
    """Process queued ``asset.analyze`` jobs.

    A provider injected by a test or a development caller uses the local
    analyzer.  The default production path is API-only and dispatches the
    request to a configured ``media_analysis`` HTTP Provider.
    """

    analyzer = provider
    external_router = None
    if analyzer is None and _api_only_media_execution():
        external_router = router or default_provider_router()
    elif analyzer is None:
        analyzer = MediaAnalysisProvider()
    results: list[dict[str, str]] = []
    admitted = _claims if _claims is not None else claims(db,families=('analysis',),limit=limit,workflow_claim=_claim_job_workflow,settings=get_settings())
    for job,workflow in admitted:
        ticket = execution_ticket(job, workflow)
        if not accept_worker_result(db, job, ticket):
            db.commit()
            continue
        _start_workflow_step(db, workflow)
        analysis_request = None
        external_result = None
        analysis_step = _get_or_create_step(db, job, name="analyze_media", sequence=0)
        transition_step(db, analysis_step, "running")
        db.commit()
        try:
            payload = job.payload or {}
            asset_id = str(payload.get("asset_id") or "")
            asset = db.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == job.tenant_id))
            if asset is None:
                raise ValueError(f"asset '{asset_id}' not found")
            options = payload.get("options") or {}
            analysis_request = None
            external_result = None
            if external_router is not None:
                analysis_request = _external_analysis_request(job, asset, options)
                analysis_request = prepare_provider_call(db, job, ticket, analysis_request, external_router)
                external_result = external_router.execute(analysis_request)
                if not accept_worker_result(db, job, ticket, external_result):
                    db.commit()
                    results.append({"job_id": job.id, "status": "superseded"})
                    continue
                _settle_provider_budget(db, job, external_result)
                analysis = _external_analysis_payload(external_result)
                if get_settings().app_environment == "production":
                    analysis = validate_hosted_analysis(analysis, source_uri=asset.uri)
            else:
                assert analyzer is not None
                analysis = analyzer.analyze(asset.uri, options=options)
                if not accept_worker_result(db, job, ticket):
                    db.commit()
                    results.append({"job_id": job.id, "status": "superseded"})
                    continue
            # Labels may have changed in either client while analysis was
            # running. Merge into the latest asset rather than its old copy.
            db.refresh(asset, with_for_update=True)
            validate_analysis_source(job, asset)
            if external_result is not None and get_settings().app_environment == "production":
                analysis = validate_hosted_analysis(analysis, source_uri=asset.uri)
            current = dict(asset.metadata_json or {})
            current["analysis"] = analysis
            # Keep the legacy auto-edit fallback useful while the canonical
            # metadata remains namespaced under ``analysis.media``.
            duration = (analysis.get("media") or {}).get("duration_seconds")
            if duration is not None:
                current.setdefault("duration", duration)
            asset.metadata_json = current
            artifact = models.Artifact(
                tenant_id=job.tenant_id,
                project_id=job.project_id,
                job_id=job.id,
                kind="analysis",
                uri=external_result.artifact_uri if external_result is not None else asset.uri,
                metadata_json={
                    "asset_id": asset.id,
                    "analysis": analysis,
                    **({"remote": True, **_provider_model_metadata(external_result)} if external_result is not None else {}),
                },
            )
            db.add(artifact)
            job.payload = {**job.payload, "analysis": analysis}
            transition_step(db, analysis_step, "succeeded")
            _finish_workflow_step(db, workflow, status="succeeded", result_json={"asset_id": asset.id})
            _complete_job_workflow(db, workflow, "succeeded", result_json={"asset_id": asset.id})
            db.commit()
            results.append({"job_id": job.id, "status": "succeeded", "asset_id": asset.id})
        except Exception as exc:
            if isinstance(exc,DispatchDeferred):
                db.rollback()
                try:
                    defer_execution(db,job,ticket)
                    results.append({'job_id':ticket.job_id,'status':'capacity_wait'})
                except ExecutionSuperseded:
                    db.rollback();results.append({'job_id':ticket.job_id,'status':'superseded'})
                continue  # noqa: BLE001 - persist structured worker failure
            external_result = external_result or getattr(exc, "rejected_result", None)
            if isinstance(exc, ExecutionSuperseded):
                db.rollback()
                results.append({"job_id": ticket.job_id, "status": "superseded"})
                continue
            db.rollback()
            if not accept_worker_result(db, job, ticket, external_result):
                db.commit()
                results.append({"job_id": ticket.job_id, "status": "superseded"})
                continue
            if external_result is not None:
                _capture_failed_result_charge(db, job, external_result)
            request = analysis_request
            audit = _provider_failure_audit(job, request, exc) if isinstance(exc, ProviderExecutionError) else None
            error = json.dumps(audit, ensure_ascii=False, sort_keys=True) if audit else str(exc)[:2000]
            if audit is not None:
                record_provider_failure(db, job, audit)
                job.payload = {**(job.payload or {}), "provider_failure": audit}
                job.failure_code = audit["code"]
                job.retryable = bool(audit["retryable"])
                job.policy_blocked = audit["code"].startswith(("budget_", "consent_", "rights_", "policy_"))
                if audit["retryable"] and job.attempt_count < job.max_attempts:
                    details = audit.get("details") or {}
                    provider_job_id = details.get("provider_job_id") if isinstance(details, dict) else None
                    if provider_job_id:
                        job.payload["provider_job_id"] = provider_job_id
                    _schedule_external_retry(
                        db,
                        job,
                        workflow,
                        analysis_step,
                        error=error,
                        failure_code=audit["code"],
                        delay_seconds=_retry_delay_seconds(job.attempt_count),
                    )
                    db.commit()
                    results.append({"job_id": job.id, "status": "retry_scheduled", "error": audit["code"]})
                    continue
            transition_step(db, analysis_step, "failed", error=error)
            _finish_workflow_step(db, workflow, status="failed", error=error)
            _complete_job_workflow(db, workflow, "failed", error=error, failure_code=audit["code"] if audit else "analysis_failed")
            db.commit()
            results.append({"job_id": job.id, "status": "failed", "error": job.error})
    return results


def process_queued_auto_edit_jobs(db: Session, *, limit: int = 10, _claims=None) -> list[dict[str, str]]:
    """Compile queued auto-edit plans into durable canonical Timelines.

    Planning is intentionally separated from rendering.  This worker consumes
    the explainable ``auto_edit.plan`` job produced by the API, resolves its
    tenant-scoped assets, persists a canonical Timeline and a timeline
    Artifact, and leaves the resulting timeline ready for preview/export via
    the normal render queue.  Existing timeline rows are reused on retry so a
    crash after the first commit cannot duplicate work.
    """
    results: list[dict[str, str]] = []
    admitted = _claims if _claims is not None else claims(db,families=('plan',),limit=limit,workflow_claim=_claim_job_workflow,settings=get_settings())
    for job,workflow in admitted:
        ticket = execution_ticket(job, workflow)
        if not accept_worker_result(db, job, ticket):
            db.commit()
            continue
        _start_workflow_step(db, workflow)
        step = _get_or_create_step(db, job, name="auto_edit.compile", sequence=0)
        transition_step(db, step, "running")
        db.commit()
        try:
            payload = dict(job.payload or {})
            template_id = str(payload.get("template_id") or "")
            template_row = db.scalar(
                select(models.Template).where(
                    models.Template.id == template_id,
                    models.Template.tenant_id == job.tenant_id,
                )
            )
            initial_revision = db.scalar(select(models.PlanRevision).join(models.EditDocument,
                models.EditDocument.id == models.PlanRevision.document_id).where(
                    models.EditDocument.origin_job_id == job.id, models.EditDocument.tenant_id == job.tenant_id,
                    models.PlanRevision.number == 1))
            if template_row is None and not payload.get("template_snapshot") and initial_revision is None:
                raise ValueError(f"template '{template_id}' not found")
            template = TemplateDefinition.model_validate(initial_revision.template_json if initial_revision else
                payload.get("template_snapshot") or template_row.content)
            asset_ids = [str(item) for item in payload.get("asset_ids", [])]
            if not asset_ids:
                # Older jobs only persisted the planner output. Recover the
                # source asset IDs from selected shot IDs for compatibility.
                asset_ids = sorted({str(item.get("asset_id", "")).split("#shot-", 1)[0] for item in payload.get("plan", {}).get("selected", []) if item.get("asset_id")})
            assets = list(
                db.scalars(
                    select(models.Asset).where(
                        models.Asset.tenant_id == job.tenant_id,
                        models.Asset.id.in_(asset_ids),
                    )
                )
            ) if asset_ids else []
            dependency_ids=[str(id) for id in payload.get('analysis_job_ids',[]) if id]
            dependency_statuses=list(db.scalars(select(models.Job.status).where(models.Job.tenant_id==job.tenant_id,models.Job.id.in_(dependency_ids)))) if dependency_ids else []
            if any(status in {'queued','running','processing'} for status in dependency_statuses):
                raise DispatchDeferred('analysis_wait')
            dependency_failed = any(status in {"failed", "cancelled"} for status in dependency_statuses)
            if dependency_failed:
                raise ValueError("one or more asset analysis jobs failed; retry analysis before auto-edit")
            if initial_revision is None:
                if payload.get("preparation_quote_id") and len(assets) != len(asset_ids):
                    raise ValueError("quoted planning source is missing")
                assets = plan_assets(db, job, assets)
            # Recompute from the latest committed analysis.  The API creates a
            # useful preview plan immediately, but the worker is the authority
            # that turns it into a durable Timeline.
            if initial_revision is None and get_settings().app_environment == "production":
                for asset in assets:
                    require_planning_analysis((asset.metadata_json or {}).get("analysis") or {},
                                              source_uri=asset.uri, kind=asset.kind)
            if initial_revision is None:
                fresh_plan = AutoEditPlanner().plan(template, shot_features_from_assets(assets))
                selected = [shot.__dict__ for shot in fresh_plan.selected]
                payload["plan"] = {"template_id": fresh_plan.template_id, "template_version": fresh_plan.template_version,
                                   "selected": selected, "warnings": list(fresh_plan.warnings),
                                   "missing_required_slots": list(fresh_plan.missing_required_slots)}
            else:
                document = db.get(models.EditDocument, initial_revision.document_id)
                frozen_timeline = assert_render_sources(db, document, initial_revision)
                payload["plan"] = dict(initial_revision.plan_json)
                payload["timeline_id"] = frozen_timeline.timeline_id
                selected = list(payload["plan"].get("selected") or [])
            payload["template_snapshot"] = template.model_dump(mode="json")
            lock_execution(db, job, ticket)
            timeline_id = str(payload.get("timeline_id") or f"tl_{job.id.replace('-', '')[:32]}")
            timeline_row = db.scalar(
                select(models.Timeline).where(
                    models.Timeline.id == timeline_id,
                    models.Timeline.tenant_id == job.tenant_id,
                )
            )
            if timeline_row is None:
                template_resolution = template.metadata.get("resolution") if isinstance(template.metadata, dict) else None
                timeline = compile_auto_edit_timeline(
                    template,
                    selected,
                    assets,
                    timeline_id=timeline_id,
                    resolution=template_resolution if isinstance(template_resolution, dict) else None,
                )
                timeline_row = models.Timeline(
                    id=timeline.id,
                    tenant_id=job.tenant_id,
                    project_id=job.project_id,
                    name=f"{template_row.name if template_row else template.id} · Auto edit",
                    fps=timeline.fps,
                    duration_ms=round(timeline.duration * 1000),
                    timeline_json=timeline.model_dump(mode="json"),
                )
                db.add(timeline_row)
                db.flush()
            job.payload = dict(payload)
            initial_revision = initialize_document(db, job=job, timeline_row=timeline_row, assets=assets, template=template)
            payload.update(edit_document_id=initial_revision.document_id, plan_revision_id=initial_revision.id,
                           timeline_revision_id=initial_revision.id)
            artifact = db.scalar(
                select(models.Artifact).where(
                    models.Artifact.job_id == job.id,
                    models.Artifact.kind == "timeline.plan",
                    models.Artifact.tenant_id == job.tenant_id,
                )
            )
            if artifact is None:
                artifact = models.Artifact(
                    tenant_id=job.tenant_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    kind="timeline.plan",
                    uri=f"timeline://{timeline_row.id}",
                    metadata_json={
                        "timeline_id": timeline_row.id,
                        "template_id": template.id,
                        "template_version": template.version,
                        "selected_count": len(selected),
                    },
                )
                db.add(artifact)
            job.payload = {
                **payload,
                "timeline_id": timeline_row.id,
                "timeline_uri": f"timeline://{timeline_row.id}",
                "compiled": True,
            }
            step.payload = {
                "timeline_id": timeline_row.id,
                "selected_count": len(selected),
                "warnings": list((payload.get("plan") or {}).get("warnings") or []),
            }
            transition_step(db, step, "succeeded")
            _finish_workflow_step(db, workflow, status="succeeded", result_json={"timeline_id": timeline_row.id})
            _complete_job_workflow(db, workflow, "succeeded", result_json={"timeline_id": timeline_row.id})
            db.commit()
            results.append({"job_id": job.id, "status": "succeeded", "timeline_id": timeline_row.id})
        except Exception as exc:
            if isinstance(exc,DispatchDeferred):
                db.rollback()
                try:
                    defer_execution(db,job,ticket,reason='analysis_wait')
                    results.append({'job_id':ticket.job_id,'status':'waiting_dependencies'})
                except ExecutionSuperseded:
                    db.rollback();results.append({'job_id':ticket.job_id,'status':'superseded'})
                continue  # noqa: BLE001 - persist deterministic worker failure
            if isinstance(exc, ExecutionSuperseded):
                db.rollback()
                results.append({"job_id": ticket.job_id, "status": "superseded"})
                continue
            db.rollback()
            if not accept_worker_result(db, job, ticket):
                db.commit()
                results.append({"job_id": ticket.job_id, "status": "superseded"})
                continue
            transition_step(db, step, "failed", error=str(exc)[:2000])
            _finish_workflow_step(db, workflow, status="failed", error=str(exc)[:2000])
            _complete_job_workflow(db, workflow, "failed", error=str(exc)[:2000], failure_code="auto_edit_failed")
            db.commit()
            results.append({"job_id": job.id, "status": "failed", "error": job.error or "auto_edit_failed"})
    return results


def process_queued_jobs(db: Session, *, limit: int = 10) -> list[dict[str, str]]:
    """One fair media dispatcher; each family retains the public poll limit."""
    from .recovery_guard import database_active
    if not database_active(db):return []
    from .batches import reconcile_batches
    from .batch_runs import reconcile_runs
    from .package_worker import process_queued_package_jobs,reconcile_package_operations
    processors={'analysis':process_queued_analysis_jobs,'plan':process_queued_auto_edit_jobs,
        'provider':process_queued_provider_jobs,'render':process_queued_render_jobs,'package':process_queued_package_jobs}
    results=[];seen=set();counts={family:0 for family in FAMILIES}
    for _ in range(max(0,limit)*len(FAMILIES)):
        eligible=[family for family in FAMILIES if counts[family]<limit and (family!='package' or get_settings().delivery_package_enabled)]
        value=claim_next(db,families=eligible,workflow_claim=_claim_job_workflow,excluded=seen,settings=get_settings())
        if value is None:break
        job,workflow=value;seen.add(job.id);family=family_for(job);counts[family]+=1
        results.extend(processors[family](db,limit=1,_claims=[(job,workflow)]))
    return results+process_archive_tasks(db,settings=get_settings(),limit=limit)+reconcile_package_operations(db,limit=limit)+reconcile_batches(db,limit=max(10,limit))+reconcile_runs(db,limit=max(10,limit))


def _provider_job_filter():
    """Types that are executed by the Provider Gateway worker."""

    return or_(
        models.Job.type.like("voice.%"),
        models.Job.type.like("avatar.%"),
        models.Job.type == "video.generate",
        models.Job.type.like("enhancement.%"),
    )


def _provider_failure_audit(
    job: models.Job,
    request: ProviderRequest | None,
    exc: Exception,
) -> dict:
    if isinstance(exc, ProviderExecutionError):
        code = exc.code
        provider = exc.provider
        capability = exc.capability
        retryable = exc.retryable
        details = exc.details
        message = str(exc)
    else:
        code = "provider_unexpected_error"
        provider = request.provider if request else None
        capability = request.capability if request else None
        retryable = True
        details = {}
        message = str(exc) or exc.__class__.__name__
    return {
        "schema_version": "provider-failure/1.0",
        "job_id": job.id,
        "request_id": request.request_id if request else job.id,
        "provider": provider,
        "capability": capability,
        "code": code,
        "retryable": retryable,
        "message": message[:2000],
        "details": details,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def _provider_request(job: models.Job, output_dir: Path, db: Session | None = None) -> ProviderRequest:
    raw = dict(job.payload.get("provider_request") or {})
    capability = raw.pop("capability", None)
    if not capability:
        raise ProviderExecutionError(
            "provider_request.capability is required",
            code="invalid_provider_request",
            retryable=False,
        )
    provider = raw.pop("provider", None)
    model_id = raw.pop("model_id", None)
    model_version = raw.pop("model_version", None)
    accepted = job.payload.get("capability_snapshot") or {}
    provider = accepted.get("provider", provider)
    model_id = accepted.get("model_id", model_id)
    model_version = accepted.get("model_version", model_version)
    commercial_use = bool(raw.pop("commercial_use", True))
    options = dict(raw.pop("options", {}) or {})
    # Fields that are policy inputs belong in the request inputs so the
    # adapter and audit trail can both observe them.  Options are model
    # tuning knobs and are kept separate from identity/rights inputs.
    region = options.pop("region", None)
    consent_id = raw.get("consent_id")
    consent_ids = (str(consent_id),) if consent_id else ()
    options["output_dir"] = str(output_dir)
    # Reuse the remote task ID after an async provider timeout. This turns a
    # durable retry into a poll and avoids duplicate billable submissions.
    pending = (job.payload.get("provider_failure") or {}).get("details") or {}
    if pending.get("provider_job_id"):
        options.setdefault("provider_job_id", str(pending["provider_job_id"]))
    # Resolve tenant-scoped assets once in the worker. CommandProvider can use
    # local paths in development, while HTTPProvider receives only the
    # remotely reachable URI and never receives a worker filesystem path.
    if db is not None:
        if _api_only_media_execution():
            for field in ("asset_uri", "source_uri", "audio_asset_uri"):
                if raw.get(field):
                    raw[field] = _remote_asset_uri(raw[field], capability=capability, field=field)
        for field in ("asset_id", "source_asset_id", "audio_asset_id"):
            asset_id = raw.get(field)
            if not asset_id:
                continue
            asset = db.scalar(select(models.Asset).where(models.Asset.id == str(asset_id), models.Asset.tenant_id == job.tenant_id))
            if asset is None:
                # The deterministic development adapter intentionally accepts
                # symbolic asset IDs.  A configured command adapter that
                # needs bytes will fail explicitly if its {input_path}
                # placeholder cannot be resolved.
                continue
            if field == "audio_asset_id":
                if _api_only_media_execution():
                    raw["audio_asset_uri"] = _remote_asset_uri(
                        asset.uri,
                        capability=capability,
                        field="audio_asset_uri",
                    )
                else:
                    raw["audio_input_path"] = asset.uri
            else:
                if _api_only_media_execution():
                    raw[f"{field.removesuffix('_id')}_uri"] = _remote_asset_uri(
                        asset.uri,
                        capability=capability,
                        field=f"{field.removesuffix('_id')}_uri",
                    )
                else:
                    raw["input_path"] = asset.uri
        reference_ids = raw.get("reference_asset_ids")
        if isinstance(reference_ids, list):
            reference_assets = list(
                db.scalars(
                    select(models.Asset).where(
                        models.Asset.tenant_id == job.tenant_id,
                        models.Asset.id.in_([str(item) for item in reference_ids]),
                    )
                )
            )
            by_id = {asset.id: asset for asset in reference_assets}
            if _api_only_media_execution():
                raw["reference_asset_uris"] = [
                    _remote_asset_uri(
                        by_id[str(asset_id)].uri,
                        capability=capability,
                        field=f"reference_asset_uris[{index}]",
                    )
                    for index, asset_id in enumerate(reference_ids)
                    if str(asset_id) in by_id
                ]
        extension = ".wav" if capability.startswith("voice_") else ".mp4"
        options.setdefault("output_path", str(output_dir / f"output{extension}"))
    return ProviderRequest(
        request_id=job.id,
        capability=capability,
        inputs=raw,
        parameters=options,
        tenant_id=job.tenant_id,
        consent_ids=consent_ids,
        region=region,
        provider=provider,
        model_id=model_id,
        model_version=model_version,
        commercial_use=commercial_use,
    )


def _validate_provider_policy(db: Session, job: models.Job, request: ProviderRequest) -> None:
    """Re-check mutable consent and rights immediately before dispatch.

    API handlers validate these inputs when a job is created, but consent can
    be revoked while a job is queued.  The worker therefore performs the
    final check and emits a non-retryable, auditable policy failure.
    """
    if request.capability == "enhancement" and not request.inputs.get("rights_confirmed"):
        raise ProviderExecutionError(
            "rights confirmation is required for enhancement",
            code="rights_not_confirmed",
            capability=request.capability,
            retryable=False,
        )
    if not request.consent_ids:
        return
    scope_by_capability = {
        "voice_tts": {"voice", "tts"},
        "voice_clone": {"voice", "voice_clone"},
        "voice_conversion": {"voice", "voice_conversion"},
        "motion_transfer": {"face", "avatar", "motion_transfer"},
        "face_transform": {"face", "avatar", "face_replace"},
        "lip_sync": {"face", "avatar", "lip_sync"},
    }
    required_scopes = scope_by_capability.get(request.capability, set())
    now = datetime.now(timezone.utc)
    for consent_id in request.consent_ids:
        consent = db.scalar(
            select(models.Consent).where(
                models.Consent.id == consent_id,
                models.Consent.tenant_id == job.tenant_id,
            )
        )
        if consent is None:
            raise ProviderExecutionError(
                "consent record was not found",
                code="consent_not_found",
                capability=request.capability,
                retryable=False,
                details={"consent_id": consent_id},
            )
        scopes = {part.strip().lower() for part in consent.scope.replace(",", " ").split() if part.strip()}
        if not consent.granted or consent.revoked_at is not None or (consent.expires_at and consent.expires_at <= now):
            raise ProviderExecutionError(
                "consent is not active",
                code="consent_inactive",
                capability=request.capability,
                retryable=False,
                details={"consent_id": consent_id},
            )
        if required_scopes and not (required_scopes & scopes):
            raise ProviderExecutionError(
                "consent scope does not cover provider capability",
                code="consent_scope_mismatch",
                capability=request.capability,
                retryable=False,
                details={"consent_id": consent_id, "required_scopes": sorted(required_scopes), "scopes": sorted(scopes)},
            )


def _is_policy_failure(audit: dict) -> bool:
    return audit.get("code", "").startswith(("budget_", "consent_", "rights_", "policy_"))


def _retry_delay_seconds(attempt_count: int) -> float:
    return float(min(60, 2 ** max(0, attempt_count - 1)))


def process_queued_provider_jobs(
    db: Session,
    *,
    limit: int = 10,
    router: ProviderRouter | None = None,
    _claims=None,
) -> list[dict[str, str]]:
    """Execute voice/avatar/generation/enhancement jobs through one gateway.

    Production uses configured HTTP Providers. Injecting a router is reserved
    for contract tests or an explicit development adapter, while preserving
    the same Job/Artifact semantics.
    """

    settings = get_settings()
    provider_router = router or default_provider_router()
    results: list[dict[str, str]] = []
    admitted = _claims if _claims is not None else claims(db,families=('provider',),limit=limit,workflow_claim=_claim_job_workflow,settings=get_settings())
    for job,workflow in admitted:
        ticket = execution_ticket(job, workflow)
        if not accept_worker_result(db, job, ticket):
            db.commit()
            continue
        _start_workflow_step(db, workflow)
        step = _get_or_create_step(db, job, name="provider.execute", sequence=0)
        transition_step(db, step, "running")
        db.commit()
        request: ProviderRequest | None = None
        result = None
        try:
            output_dir = Path(settings.storage_root) / job.tenant_id / (job.project_id or "unassigned") / "providers" / job.id
            request = _provider_request(job, output_dir, db)
            _validate_provider_policy(db, job, request)
            request = prepare_provider_call(db, job, ticket, request, provider_router)
            result = provider_router.execute(request)
            if not accept_worker_result(db, job, ticket, result):
                db.commit()
                results.append({"job_id": job.id, "status": "superseded"})
                continue
            _settle_provider_budget(db, job, result)
            artifact = models.Artifact(
                tenant_id=job.tenant_id,
                project_id=job.project_id,
                job_id=job.id,
                kind=result.artifact_type,
                uri=result.artifact_uri,
                metadata_json={
                    "request_id": request.request_id,
                    "capability": request.capability,
                    "status": result.status,
                    "provider": result.model.provider,
                    "model": {
                        "model_id": result.model.model_id,
                        "version": result.model.version,
                        "weights_sha256": result.model.weights_sha256,
                        "runtime_digest": result.model.runtime_digest,
                        "license_snapshot": result.model.license_snapshot,
                    },
                    "metrics": result.metrics,
                    "usage": result.usage,
                    "cost": result.cost,
                    "provenance": result.provenance,
                    "warnings": list(result.warnings),
                },
            )
            db.add(artifact)
            step.payload = {
                "request_id": request.request_id,
                "provider": result.model.provider,
                "capability": request.capability,
                "artifact_type": result.artifact_type,
                "artifact_uri": result.artifact_uri,
                "status": result.status,
                "usage": result.usage,
                "cost": result.cost,
                "warnings": list(result.warnings),
            }
            transition_step(db, step, "succeeded")
            job.payload = {
                **job.payload,
                "provider_result": {
                    "artifact_uri": result.artifact_uri,
                    "artifact_type": result.artifact_type,
                    "provider": result.model.provider,
                    "status": result.status,
                    "usage": result.usage,
                    "cost": result.cost,
                },
            }
            _finish_workflow_step(
                db,
                workflow,
                status="succeeded",
                result_json={"artifact_uri": result.artifact_uri, "artifact_type": result.artifact_type},
            )
            _complete_job_workflow(
                db,
                workflow,
                "succeeded",
                result_json={"artifact_uri": result.artifact_uri, "artifact_type": result.artifact_type},
            )
            db.commit()
            results.append({"job_id": job.id, "status": "succeeded", "output_uri": result.artifact_uri})
        except Exception as exc:
            if isinstance(exc,DispatchDeferred):
                db.rollback()
                try:
                    defer_execution(db,job,ticket)
                    results.append({'job_id':ticket.job_id,'status':'capacity_wait'})
                except ExecutionSuperseded:
                    db.rollback();results.append({'job_id':ticket.job_id,'status':'superseded'})
                continue  # noqa: BLE001 - audit all provider failures
            result = result or getattr(exc, "rejected_result", None)
            if isinstance(exc, ExecutionSuperseded):
                db.rollback()
                results.append({"job_id": ticket.job_id, "status": "superseded"})
                continue
            db.rollback()
            if not accept_worker_result(db, job, ticket, result):
                db.commit()
                results.append({"job_id": ticket.job_id, "status": "superseded"})
                continue
            if result is not None:
                _capture_failed_result_charge(db, job, result)
            audit = _provider_failure_audit(job, request, exc)
            record_provider_failure(db, job, audit)
            audit_error = json.dumps(audit, ensure_ascii=False, sort_keys=True)
            step.payload = {
                **(step.payload or {}),
                "audit": audit,
                "last_failure": audit,
                "retry_count": max(0, job.attempt_count - 1),
            }
            job.payload = {**job.payload, "provider_failure": audit}
            job.failure_code = audit["code"]
            job.retryable = bool(audit["retryable"])
            job.policy_blocked = _is_policy_failure(audit)
            if audit["retryable"] and job.attempt_count < job.max_attempts:
                # The step is requeued with the parent job.  Its payload keeps
                # the complete latest audit while avoiding duplicate steps.
                transition_step(db, step, "failed", error=audit_error)
                reset_step_for_retry(db, step, error=audit_error)
                retry_delay = float(
                    (audit.get("details") or {}).get("poll_after_seconds")
                    or _retry_delay_seconds(job.attempt_count)
                )
                schedule_retry(
                    db,
                    job,
                    error=audit_error,
                    failure_code=audit["code"],
                    delay_seconds=min(300.0, max(0.0, retry_delay)),
                )
                _finish_workflow_step(db, workflow, status="failed", error=audit_error, retry=True)
                _retry_job_workflow(
                    db,
                    workflow,
                    error=audit_error,
                    failure_code=audit["code"],
                    delay_seconds=min(300.0, max(0.0, retry_delay)),
                )
                db.commit()
                results.append({"job_id": job.id, "status": "retry_scheduled", "error": audit["code"]})
                continue
            transition_step(db, step, "failed", error=audit_error)
            _finish_workflow_step(db, workflow, status="failed", error=audit_error)
            _complete_job_workflow(
                db,
                workflow,
                "failed",
                error=audit_error,
                failure_code=audit["code"],
            )
            db.commit()
            results.append({"job_id": job.id, "status": "failed", "error": job.error or audit["code"]})
    return results


def process_queued_render_jobs(
    db: Session,
    *,
    limit: int = 10,
    router: ProviderRouter | None = None,
    _claims=None,
) -> list[dict[str, str]]:
    settings = get_settings()
    external_router = (router or default_provider_router()) if _api_only_media_execution() else None
    results: list[dict[str, str]] = []
    admitted = _claims if _claims is not None else claims(db,families=('render',),limit=limit,workflow_claim=_claim_job_workflow,settings=get_settings())
    for job,workflow in admitted:
        ticket = execution_ticket(job, workflow)
        if not accept_worker_result(db, job, ticket):
            db.commit()
            continue
        _start_workflow_step(db, workflow)
        request = None
        result = None
        render_step = _get_or_create_step(db, job, name="render_media", sequence=0)
        transition_step(db, render_step, "running")
        db.commit()
        try:
            manifest = RenderManifest.model_validate(job.payload["manifest"])
            if not manifest.verify_hash():
                raise ValueError("render manifest content_hash verification failed")
            try:
                validate_render_revision(db, job, manifest)
            except EditingError as exc:
                raise ProviderExecutionError(str(exc), code=exc.code, capability="video_editing", retryable=False) from exc
            timeline = Timeline.model_validate(manifest.source["timeline"])
            quality = str(manifest.options.get("quality", "draft"))
            if external_router is not None:
                request = _external_render_request(job, manifest, quality)
                request = prepare_provider_call(db, job, ticket, request, external_router)
                result = external_router.execute(request)
                if not accept_worker_result(db, job, ticket, result):
                    db.commit()
                    results.append({"job_id": job.id, "status": "superseded"})
                    continue
                _settle_provider_budget(db, job, result)
                hosted_qc = {}
                if settings.app_environment == "production":
                    hosted_qc = validate_render_quality(result.metrics.get("quality_control") or {},
                                                        timeline=timeline, output_uri=result.artifact_uri)
                artifact = models.Artifact(
                    tenant_id=job.tenant_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    kind=result.artifact_type,
                    uri=result.artifact_uri,
                    metadata_json={
                        "manifest_id": manifest.manifest_id,
                        "quality": quality,
                        "content_hash": manifest.content_hash,
                        "remote": True,
                        **({"quality_control": hosted_qc, "output_sha256": hosted_qc["output_sha256"]} if hosted_qc else {}),
                        **_provider_model_metadata(result),
                        **render_revision_binding(job),
                    },
                )
                db.add(artifact)
                render_step.payload = {
                    "request_id": request.request_id,
                    "provider": result.model.provider,
                    "capability": request.capability,
                    "artifact_type": result.artifact_type,
                    "artifact_uri": result.artifact_uri,
                    "status": result.status,
                    "remote": True,
                    "usage": result.usage,
                    "cost": result.cost,
                    "warnings": list(result.warnings),
                }
                transition_step(db, render_step, "succeeded")
                job.payload = {
                    **job.payload,
                    "output_uri": result.artifact_uri,
                    "artifact_manifest_id": manifest.manifest_id,
                    "provider_result": {
                        "artifact_uri": result.artifact_uri,
                        "artifact_type": result.artifact_type,
                        "provider": result.model.provider,
                        "status": result.status,
                        "usage": result.usage,
                        "cost": result.cost,
                    },
                }
                _finish_workflow_step(db, workflow, status="succeeded", result_json={"output_uri": result.artifact_uri})
                _complete_job_workflow(db, workflow, "succeeded", result_json={"output_uri": result.artifact_uri})
                db.commit()
                results.append({"job_id": job.id, "status": "succeeded", "output_uri": result.artifact_uri})
                continue
            output_dir = Path(settings.storage_root) / job.tenant_id / (job.project_id or "unassigned") / "renders"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{job.id}.mp4"
            # Never expose a partially written file as an Artifact.  A unique
            # sibling temp file also makes retries safe when a worker dies
            # after FFmpeg opens its output.
            temp_path = output_dir / f".{job.id}.rendering.mp4"
            temp_path.unlink(missing_ok=True)
            command = build_ffmpeg_command(timeline, temp_path, RenderProfile(name=quality))
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=settings.render_timeout_seconds,
                )
                qc = probe_render_output(temp_path, timeline)
                output_hash = hashlib.sha256(temp_path.read_bytes()).hexdigest()
                os.replace(temp_path, output_path)
            finally:
                temp_path.unlink(missing_ok=True)
            lock_execution(db, job, ticket)
            artifact = models.Artifact(
                tenant_id=job.tenant_id,
                project_id=job.project_id,
                job_id=job.id,
                kind="render",
                uri=str(output_path),
                metadata_json={
                    "manifest_id": manifest.manifest_id,
                    "quality": quality,
                    "content_hash": manifest.content_hash,
                    "output_sha256": output_hash,
                    "quality_control": qc,
                    **render_revision_binding(job),
                },
            )
            db.add(artifact)
            transition_step(db, render_step, "succeeded")
            job.payload = {**job.payload, "output_uri": str(output_path), "artifact_manifest_id": manifest.manifest_id}
            _finish_workflow_step(db, workflow, status="succeeded", result_json={"output_uri": str(output_path)})
            _complete_job_workflow(db, workflow, "succeeded", result_json={"output_uri": str(output_path)})
            db.commit()
            results.append({"job_id": job.id, "status": "succeeded", "output_uri": str(output_path)})
        except Exception as exc:
            if isinstance(exc,DispatchDeferred):
                db.rollback()
                try:
                    defer_execution(db,job,ticket)
                    results.append({'job_id':ticket.job_id,'status':'capacity_wait'})
                except ExecutionSuperseded:
                    db.rollback();results.append({'job_id':ticket.job_id,'status':'superseded'})
                continue  # noqa: BLE001 - worker must persist structured failure
            result = result or getattr(exc, "rejected_result", None)
            if isinstance(exc, ExecutionSuperseded):
                db.rollback()
                results.append({"job_id": ticket.job_id, "status": "superseded"})
                continue
            db.rollback()
            if not accept_worker_result(db, job, ticket, result):
                db.commit()
                results.append({"job_id": ticket.job_id, "status": "superseded"})
                continue
            if result is not None:
                _capture_failed_result_charge(db, job, result)
            audit = _provider_failure_audit(job, request, exc) if isinstance(exc, ProviderExecutionError) else None
            error = json.dumps(audit, ensure_ascii=False, sort_keys=True) if audit else str(exc)[:2000]
            if audit is not None:
                record_provider_failure(db, job, audit)
                job.payload = {**(job.payload or {}), "provider_failure": audit}
                job.failure_code = audit["code"]
                job.retryable = bool(audit["retryable"])
                job.policy_blocked = audit["code"].startswith(("budget_", "consent_", "rights_", "policy_"))
                if audit["retryable"] and job.attempt_count < job.max_attempts:
                    details = audit.get("details") or {}
                    provider_job_id = details.get("provider_job_id") if isinstance(details, dict) else None
                    if provider_job_id:
                        job.payload["provider_job_id"] = provider_job_id
                    _schedule_external_retry(
                        db,
                        job,
                        workflow,
                        render_step,
                        error=error,
                        failure_code=audit["code"],
                        delay_seconds=_retry_delay_seconds(job.attempt_count),
                    )
                    db.commit()
                    results.append({"job_id": job.id, "status": "retry_scheduled", "error": audit["code"]})
                    continue
            transition_step(db, render_step, "failed", error=error)
            _finish_workflow_step(db, workflow, status="failed", error=error)
            _complete_job_workflow(db, workflow, "failed", error=error, failure_code=audit["code"] if audit else "render_failed")
            db.commit()
            results.append({"job_id": job.id, "status": "failed", "error": job.error})
    return results
