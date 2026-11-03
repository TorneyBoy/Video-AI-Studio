"""Durable workflow leases and append-only execution events.

The service is intentionally runtime-neutral: the local poller and an optional
Temporal adapter use the same database contract.  ``Job`` remains the public
API object while ``WorkflowRun`` carries worker ownership and recovery state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from .. import models

WORKFLOW_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
WORKFLOW_STATES = frozenset({"queued", "running", *WORKFLOW_TERMINAL_STATES})
WORKFLOW_STEP_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "skipped"})


class WorkflowError(RuntimeError):
    """Base error for an invalid workflow operation."""


class WorkflowLeaseLost(WorkflowError):
    """Raised when a worker no longer owns an execution lease."""


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).replace(microsecond=0)


def _assert_owner(run: models.WorkflowRun, worker_id: str | None) -> None:
    if worker_id and run.worker_id != worker_id:
        raise WorkflowLeaseLost("workflow lease belongs to another worker")


def append_event(
    db: Session,
    run: models.WorkflowRun,
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    worker_id: str | None = None,
) -> models.WorkflowEvent:
    """Append one ordered event in the same transaction as the state change."""
    db.execute(select(models.WorkflowRun.id).where(models.WorkflowRun.id == run.id).with_for_update())
    last = db.scalar(
        select(func.max(models.WorkflowEvent.sequence)).where(models.WorkflowEvent.workflow_run_id == run.id)
    )
    event = models.WorkflowEvent(
        tenant_id=run.tenant_id,
        workflow_run_id=run.id,
        sequence=int(last or 0) + 1,
        event_type=event_type,
        worker_id=worker_id,
        payload=payload or {},
    )
    db.add(event)
    db.flush()
    return event


def create_workflow(
    db: Session,
    job: models.Job,
    *,
    workflow_type: str | None = None,
    input_json: dict[str, Any] | None = None,
    max_attempts: int | None = None,
    steps: Iterable[dict[str, Any]] = (),
    runtime: str = "local",
) -> models.WorkflowRun:
    """Create or return the durable workflow associated with a Job."""
    existing = db.scalar(select(models.WorkflowRun).where(models.WorkflowRun.job_id == job.id))
    if existing is not None:
        return existing
    attempts = max_attempts if max_attempts is not None else job.max_attempts
    if attempts < 1:
        raise ValueError("max_attempts must be greater than zero")
    run = models.WorkflowRun(
        tenant_id=job.tenant_id,
        job_id=job.id,
        workflow_type=workflow_type or job.type,
        runtime=runtime,
        max_attempts=attempts,
        input_json=input_json or dict(job.payload or {}),
    )
    db.add(run)
    db.flush()
    for item in steps:
        db.add(
            models.WorkflowStep(
                tenant_id=job.tenant_id,
                workflow_run_id=run.id,
                name=str(item["name"]),
                sequence=int(item.get("sequence", 0)),
                max_attempts=int(item.get("max_attempts", attempts)),
                input_json=dict(item.get("input_json", item.get("payload", {})) or {}),
            )
        )
    append_event(db, run, "workflow.created", payload={"workflow_type": run.workflow_type, "runtime": runtime})
    return run


def claim_workflow(
    db: Session,
    run: models.WorkflowRun,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> bool:
    """Atomically claim a queued or expired workflow execution."""
    from .recovery_guard import database_active
    if not database_active(db):return False
    if not worker_id.strip():
        raise ValueError("worker_id is required")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be greater than zero")
    current = _now(now)
    was_expired = run.status == "running"
    due = or_(models.WorkflowRun.next_retry_at.is_(None), models.WorkflowRun.next_retry_at <= current)
    eligible = or_(and_(models.WorkflowRun.status == "queued", due), and_(models.WorkflowRun.status == "running", models.WorkflowRun.lease_expires_at <= current))
    result = db.execute(
        update(models.WorkflowRun)
        .where(
            models.WorkflowRun.id == run.id,
            eligible,
            models.WorkflowRun.attempt_count < models.WorkflowRun.max_attempts,
        )
        .values(
            status="running",
            worker_id=worker_id,
            lease_expires_at=current + timedelta(seconds=lease_seconds),
            heartbeat_at=current,
            next_retry_at=None,
            started_at=func.coalesce(models.WorkflowRun.started_at, current),
            attempt_count=models.WorkflowRun.attempt_count + 1,
        ).execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    db.refresh(run)
    append_event(db, run, "workflow.reclaimed" if was_expired else "workflow.claimed", worker_id=worker_id)
    return True


def heartbeat_workflow(
    db: Session,
    run: models.WorkflowRun,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> models.WorkflowRun:
    """Extend an active lease, rejecting stale or foreign workers."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be greater than zero")
    current = _now(now)
    result = db.execute(
        update(models.WorkflowRun)
        .where(
            models.WorkflowRun.id == run.id,
            models.WorkflowRun.status == "running",
            models.WorkflowRun.worker_id == worker_id,
            models.WorkflowRun.lease_expires_at > current,
        )
        .values(heartbeat_at=current, lease_expires_at=current + timedelta(seconds=lease_seconds))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise WorkflowLeaseLost("workflow lease is expired or owned by another worker")
    db.refresh(run)
    append_event(db, run, "workflow.heartbeat", worker_id=worker_id, payload={"lease_seconds": lease_seconds})
    return run


def complete_workflow(
    db: Session,
    run: models.WorkflowRun,
    status: str,
    *,
    worker_id: str | None = None,
    result_json: dict[str, Any] | None = None,
    error: str | None = None,
    failure_code: str | None = None,
) -> models.WorkflowRun:
    if status not in WORKFLOW_TERMINAL_STATES:
        raise WorkflowError(f"invalid terminal workflow status: {status}")
    if run.status in WORKFLOW_TERMINAL_STATES:
        raise WorkflowError(f"workflow is already {run.status}")
    _assert_owner(run, worker_id)
    now = _now()
    run.status = status
    run.completed_at = now
    run.lease_expires_at = None
    run.heartbeat_at = now
    run.worker_id = None
    run.result_json = result_json or {}
    run.error = error
    run.failure_code = failure_code
    append_event(db, run, f"workflow.{status}", worker_id=worker_id, payload={"failure_code": failure_code} if failure_code else {})
    db.flush()
    return run


def retry_workflow(
    db: Session,
    run: models.WorkflowRun,
    *,
    worker_id: str | None = None,
    error: str,
    failure_code: str | None = None,
    delay_seconds: float = 1.0,
) -> models.WorkflowRun:
    """Return a failed attempt to the queue while preserving the error event."""
    if run.status != "running":
        raise WorkflowError(f"cannot retry workflow from {run.status}")
    _assert_owner(run, worker_id)
    if run.attempt_count >= run.max_attempts:
        return complete_workflow(db, run, "failed", worker_id=worker_id, error=error, failure_code=failure_code)
    now = _now()
    run.status = "queued"
    run.next_retry_at = now + timedelta(seconds=max(delay_seconds, 0.0))
    run.worker_id = None
    run.lease_expires_at = None
    run.heartbeat_at = now
    run.error = error
    run.failure_code = failure_code
    append_event(db, run, "workflow.retry_scheduled", worker_id=worker_id, payload={"failure_code": failure_code, "delay_seconds": delay_seconds})
    db.flush()
    return run


def request_workflow_retry(
    db: Session,
    run: models.WorkflowRun,
    *,
    delay_seconds: float = 0.0,
) -> models.WorkflowRun:
    """Requeue a terminal failed workflow for its parent Job retry.

    The provider audit fields remain unchanged.  This is important for
    operator diagnostics and for preserving ``provider_job_id`` in the parent
    Job payload when a hosted provider accepted work before the failure.
    Repeated requests while queued are idempotent.
    """

    if run.status == "queued" and run.next_retry_at is not None:
        return run
    if run.status != "failed":
        raise WorkflowError(f"cannot retry workflow from {run.status}")
    if run.attempt_count >= run.max_attempts:
        raise WorkflowError("workflow retry budget exhausted")
    now = _now()
    next_retry_at = now + timedelta(seconds=max(delay_seconds, 0.0))
    # Compare-and-set the terminal state so concurrent operator requests do
    # not append duplicate retry events or move the retry deadline twice.
    result = db.execute(
        update(models.WorkflowRun)
        .where(models.WorkflowRun.id == run.id, models.WorkflowRun.status == "failed")
        .values(
            status="queued",
            next_retry_at=next_retry_at,
            worker_id=None,
            lease_expires_at=None,
            heartbeat_at=now,
            completed_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.refresh(run)
        if run.status == "queued" and run.next_retry_at is not None:
            return run
        raise WorkflowError("workflow changed before retry could be scheduled")
    db.refresh(run)
    # A terminal activity state belongs to the previous attempt.  Requeue it
    # together with the parent workflow so the worker can claim it cleanly;
    # leaving it as ``failed`` would make recovery appear queued at the run
    # level while the activity remains permanently terminal.
    for step in db.scalars(
        select(models.WorkflowStep).where(
            models.WorkflowStep.workflow_run_id == run.id,
            models.WorkflowStep.status.in_(("failed", "cancelled", "skipped")),
        )
    ):
        step.status = "queued"
        step.progress = 0.0
        step.worker_id = None
        step.lease_expires_at = None
        step.heartbeat_at = None
        step.started_at = None
        step.completed_at = None
        step.error = None
        step.result_json = {}
    append_event(
        db,
        run,
        "workflow.operator_retry_scheduled",
        payload={"delay_seconds": max(delay_seconds, 0.0)},
    )
    db.flush()
    return run


def cancel_workflow(db: Session, run: models.WorkflowRun, *, reason: str | None = None) -> models.WorkflowRun:
    if run.status in WORKFLOW_TERMINAL_STATES:
        raise WorkflowError(f"workflow is already {run.status}")
    run.status = "cancelled"
    run.completed_at = _now()
    run.worker_id = None
    run.lease_expires_at = None
    run.error = reason
    append_event(db, run, "workflow.cancelled", payload={"reason": reason} if reason else {})
    db.flush()
    return run


def recover_expired_workflows(db: Session, *, now: datetime | None = None, limit: int = 100) -> list[models.WorkflowRun]:
    """Requeue expired leases, or fail them when their retry budget is spent."""
    from .recovery_guard import database_active
    if not database_active(db):return []
    current = _now(now)
    runs = list(
        db.scalars(
            select(models.WorkflowRun)
            .where(models.WorkflowRun.status == "running", models.WorkflowRun.lease_expires_at <= current)
            .order_by(models.WorkflowRun.lease_expires_at)
            .limit(limit)
        )
    )
    from .completion import finish_job

    recovered = []
    for run in runs:
        # Same lock order as completion/cancellation. A stale scan must not
        # recover a run that a callback finished while the scan was waiting.
        job = db.scalar(select(models.Job).where(models.Job.id == run.job_id)
                        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if job is None:
            continue
        db.refresh(run, with_for_update=True)
        expiry = run.lease_expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if run.status != "running" or expiry is None or expiry > current:
            continue
        if job.status in WORKFLOW_TERMINAL_STATES:
            complete_workflow(db, run, job.status, error=job.error, failure_code=job.failure_code)
            recovered.append(run)
            continue
        if run.attempt_count >= run.max_attempts:
            finish_job(db, job, "failed", error="workflow lease expired; retry budget exhausted", failure_code="lease_expired")
        else:
            run.status = "queued"
            run.next_retry_at = current
            run.worker_id = None
            run.lease_expires_at = None
            run.error = "workflow lease expired; queued for recovery"
            run.failure_code = "lease_expired"
            job.status = "queued"
            job.execution_generation += 1
            job.next_retry_at = current
            job.retryable = True
            job.failure_code = "lease_expired"
            job.error = run.error
            for step in db.scalars(select(models.JobStep).where(models.JobStep.job_id == job.id)):
                if step.status != "succeeded":
                    step.status = "queued"
                    step.progress = 0.0
                    step.started_at = None
                    step.completed_at = None
            for step in db.scalars(select(models.WorkflowStep).where(models.WorkflowStep.workflow_run_id == run.id)):
                if step.status != "succeeded":
                    step.status = "queued"
                    step.worker_id = None
                    step.lease_expires_at = None
                    step.progress = 0.0
            append_event(db, run, "workflow.lease_expired", payload={"retryable": True})
        recovered.append(run)
    db.flush()
    return recovered

