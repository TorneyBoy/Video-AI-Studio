"""Common transaction boundary for workers, callbacks and cancellation.

Callers commit once after domain writes and finish_job. No network call may
run while these locks are held. Lock order: Job, Workflow, Tenant, Budget.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session,object_session

from .. import models
from .budgets import release_job_reservation, settle_provider_cost
from .jobs import JOB_TERMINAL_STATES, transition_job, transition_step
from .providers import ProviderExecutionError
from .render_features import require_render_support
from .editing_errors import EditingError
from .workflows import append_event, complete_workflow
from .recovery_guard import database_state,database_active


class ExecutionSuperseded(RuntimeError):
    """The caller has lost its execution claim and must not mutate results."""


@dataclass(frozen=True)
class ExecutionTicket:
    job_id: str
    generation: int
    workflow_id: str
    workflow_attempt: int
    worker_id: str | None
    database_instance: str | None = None


def execution_ticket(job: models.Job, workflow: models.WorkflowRun) -> ExecutionTicket:
    db=object_session(job);instance=database_state(db)['instance_id'] if db is not None else None
    return ExecutionTicket(job.id, job.execution_generation, workflow.id,
                           workflow.attempt_count, workflow.worker_id,instance)


def lock_job(db: Session, job: models.Job) -> models.WorkflowRun | None:
    # Explicit refresh defeats the identity-map cache after a long API call.
    db.refresh(job, with_for_update=True)
    return db.scalar(select(models.WorkflowRun).where(
        models.WorkflowRun.job_id == job.id,
    ).with_for_update().execution_options(populate_existing=True))


def lock_execution(db: Session, job: models.Job, ticket: ExecutionTicket) -> models.WorkflowRun:
    state=database_state(db)
    if state['state']!='active' or state['instance_id']!=ticket.database_instance:
        raise ExecutionSuperseded('database recovery fence rejected this execution')
    workflow = lock_job(db, job)
    if job.id != ticket.job_id or job.status != "running" or job.execution_generation != ticket.generation:
        raise ExecutionSuperseded("job execution has been superseded")
    if (workflow is None or workflow.id != ticket.workflow_id or workflow.status != "running"
            or workflow.attempt_count != ticket.workflow_attempt or workflow.worker_id != ticket.worker_id):
        raise ExecutionSuperseded("workflow execution has been superseded")
    expiry = workflow.lease_expires_at
    if expiry is None or (expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry) <= datetime.now(timezone.utc):
        raise ExecutionSuperseded("workflow execution lease has expired")
    return workflow


def accept_worker_result(db: Session, job: models.Job, ticket: ExecutionTicket, result=None) -> bool:
    state=database_state(db)
    if state['state']!='active' or state['instance_id']!=ticket.database_instance:return False
    try:
        lock_execution(db, job, ticket)
    except ExecutionSuperseded:
        # An actual provider charge must survive cancellation or a newer
        # polling claim. It never grants the stale worker delivery rights.
        if result is not None:
            operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job.id))
            remote_id = (result.provenance or {}).get("provider_job_id")
            if (operation is not None and operation.provider_key == result.model.provider
                    and (not remote_id or not operation.external_job_id or str(remote_id) == operation.external_job_id)):
                operation.state = "result_received"
            settle_provider_cost(db, job=job, cost=result.cost, usage=result.usage,
                                 provider=result.model.provider, model_id=result.model.model_id,
                                 model_version=result.model.version,
                                 source="late_worker_result")
        return False
    return True


def prepare_provider_call(db: Session, job: models.Job, ticket: ExecutionTicket, request, router):
    """Pin the provider and operation before dispatch, then release all locks."""
    lock_execution(db, job, ticket)
    operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job.id))
    if operation is not None and operation.state == "terminal_received":
        raise ProviderExecutionError("remote operation has ended; a new generation needs a new quote",
                                     code="provider_operation_ended", retryable=False)
    if operation and operation.provider_key:
        request = replace(request, provider=operation.provider_key)
    selected = router.choose(request)
    request = replace(request, provider=selected.name)
    if request.capability == "media_analysis" and not (request.parameters.get("provider_job_id") or (operation and operation.external_job_id)) and (operation is None or operation.state == "created"):
        from .preparation_quotes import validate_analysis_price
        validate_analysis_price(db, job)
    if request.capability == "video_editing" and request.inputs.get("timeline") and not (request.parameters.get("provider_job_id") or (operation and operation.external_job_id)):
        try:
            current = require_render_support(request.inputs["timeline"], require_price=bool((job.payload or {}).get("quote_id")), provider=selected.name, model_id=request.model_id, model_version=request.model_version)
            if ((job.payload or {}).get("quote_id") and (operation is None or operation.state == "created")
                    and current != (job.payload or {}).get("capability_snapshot")):
                raise EditingError("render_quote_changed", "服务价格已变化，本次尚未提交，请重新获取报价", 409)
        except EditingError as exc:
            raise ProviderExecutionError(str(exc), code=exc.code, capability="video_editing", provider=selected.name, retryable=False) from exc
    digest = hashlib.sha256(json.dumps({
        "capability": request.capability, "inputs": request.inputs,
        "parameters": {k: v for k, v in request.parameters.items() if k not in {"provider_job_id", "output_dir"}},
        "model_id": request.model_id, "model_version": request.model_version,
    }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    remote_id = (operation.external_job_id if operation else None) or request.parameters.get("provider_job_id")
    if not remote_id and (operation is None or operation.state in {"created", "rejected"}):
        from .budgets import validate_dispatch_budget
        validate_dispatch_budget(db,job)
    if operation is None:
        operation = models.ProviderOperation(tenant_id=job.tenant_id, job_id=job.id,
                                             request_id=request.request_id, state="created")
        db.add(operation)
    elif operation.request_hash and operation.request_hash != digest:
        raise ProviderExecutionError("provider operation inputs changed", code="operation_input_conflict", retryable=False)
    if operation.state in {"submitting", "unknown"} and not remote_id:
        raise ProviderExecutionError("remote submission needs reconciliation before retry",
                                     code="submission_outcome_unknown", retryable=False)
    if remote_id:
        request = replace(request, parameters={**request.parameters, "provider_job_id": str(remote_id)})
        operation.external_job_id = str(remote_id)
    from .dispatch import provider_gate
    provider_gate(db,job,selected,request.capability,continuation=bool(remote_id))
    operation.provider_key = selected.name
    operation.capability = request.capability
    operation.request_hash = digest
    operation.execution_generation = ticket.generation
    operation.state = "pending" if remote_id else "submitting"
    db.commit()
    return request


def record_provider_failure(db: Session, job: models.Job, audit: dict) -> None:
    operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job.id))
    if operation is None:
        return
    details = audit.get("details") or {}
    external_id = details.get("provider_job_id")
    if external_id:
        operation.external_job_id = str(external_id)
        operation.state = "pending"
        job.payload = {**job.payload, "provider_job_id": str(external_id)}
    elif operation.state == "submitting":
        # A transport exception does not prove the vendor rejected a billable
        # request. Preserve its identity instead of automatically resubmitting.
        rejected = details.get("submission_state") == "rejected" or details.get("http_status") in {400, 401, 403, 404, 413, 415, 422, 429}
        operation.state = "rejected" if rejected else "unknown"
        if not rejected:
            audit["retryable"] = False
            audit["details"] = {**details, "reconciliation_required": True}
    operation.result_json = {"failure_code": audit.get("code"), "retryable": audit.get("retryable", False)}


def render_revision_binding(job: models.Job) -> dict:
    """Server-owned version identity; provider metadata cannot replace it."""
    payload = job.payload or {}
    if not job.type.startswith("render.") or not payload.get("timeline_revision_id"):
        return {}
    return {key: payload[key] for key in ("edit_document_id", "plan_revision_id", "timeline_revision_id", "plan_hash", "timeline_hash", "batch_id", "batch_run_id", "batch_item_id", "parent_job_id")
            if key in payload} | {"input_manifest_hash": (payload.get("manifest") or {}).get("content_hash")}


def store_artifact(db: Session, job: models.Job, *, kind: str, uri: str,
                   metadata: dict | None = None, role: str = "primary", index: int = 0) -> models.Artifact:
    """The caller holds Job lock; the unique key is the final concurrency guard."""
    existing = db.scalar(select(models.Artifact).where(
        models.Artifact.job_id == job.id, models.Artifact.output_role == role,
        models.Artifact.output_index == index,
    ))
    if existing is not None:
        if existing.uri != uri or existing.kind != kind:
            raise ValueError("conflicting immutable output for this job")
        return existing
    artifact = models.Artifact(tenant_id=job.tenant_id, project_id=job.project_id,
                               job_id=job.id, kind=kind, uri=uri,
                               output_role=role, output_index=index, metadata_json={**(metadata or {}), **render_revision_binding(job)})
    db.add(artifact)
    db.flush()
    return artifact


def finish_job(db: Session, job: models.Job, status: str, *, result_json: dict | None = None,
               error: str | None = None, failure_code: str | None = None) -> None:
    """Finish Job, steps, workflow and outbox within the caller's locked txn."""
    if not database_active(db):raise ExecutionSuperseded('database recovery quarantine prevents completion')
    if job.type.startswith("batch."):
        raise ExecutionSuperseded("Batch status is derived from its items; act on the original child task")
    if job.status in JOB_TERMINAL_STATES:
        if job.status == status:
            return
        raise ExecutionSuperseded("terminal job cannot be overwritten")
    if status == "succeeded" and job.status == "queued":
        transition_job(db, job, "running")
    transition_job(db, job, status, error=error)
    if failure_code:
        job.failure_code = failure_code
    for step in db.scalars(select(models.JobStep).where(models.JobStep.job_id == job.id)):
        if step.status == "queued" and status == "succeeded":
            transition_step(db, step, "running")
        if step.status in {"queued", "running"}:
            transition_step(db, step, status, error=error)
    workflow = db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job.id))
    if workflow is not None and workflow.status not in JOB_TERMINAL_STATES:
        now = datetime.now(timezone.utc)
        for step in db.scalars(select(models.WorkflowStep).where(models.WorkflowStep.workflow_run_id == workflow.id)):
            if step.status in {"queued", "running"}:
                step.status = status
                step.progress = 1.0 if status == "succeeded" else step.progress
                step.completed_at = now
                step.worker_id = None
                step.lease_expires_at = None
                step.result_json = result_json or {}
                step.error = error
                append_event(db, workflow, f"workflow.step.{status}", payload={"step_id": step.id, "step": step.name})
        complete_workflow(db, workflow, status, result_json=result_json, error=error, failure_code=failure_code)
    operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job.id))
    if operation is not None and status == "succeeded":
        operation.state = "completed"
        operation.result_json = result_json or {}
    if status in {"failed", "cancelled"}:
        release_job_reservation(db, job)
    db.add(models.OutboxEvent(tenant_id=job.tenant_id, job_id=job.id,
                             event_type=f"job.{status}", execution_generation=job.execution_generation,
                             payload_json={"status": status, "failure_code": failure_code,
                                           "result": result_json or {}}))
    db.flush()
