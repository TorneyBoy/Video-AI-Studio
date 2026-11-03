"""Job and job-step state transitions.

Keeping transition rules in one service means HTTP handlers and local workers
apply the same monotonic state machine and timestamps.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .. import models

JOB_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
JOB_STATES = frozenset({"queued", "running", *JOB_TERMINAL_STATES})
STEP_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "skipped"})
STEP_STATES = frozenset({"queued", "running", *STEP_TERMINAL_STATES})


class InvalidJobTransition(ValueError):
    pass


def _transition(current: str, target: str, *, states: frozenset[str], terminal: frozenset[str]) -> None:
    if target not in states:
        raise InvalidJobTransition(f"Unknown state: {target}")
    if current == target:
        return
    if current in terminal:
        raise InvalidJobTransition(f"Cannot transition terminal state {current} to {target}")
    if current == "queued" and target not in {"running", "cancelled", "failed"}:
        raise InvalidJobTransition(f"Invalid transition {current} -> {target}")
    if current == "running" and target not in terminal:
        raise InvalidJobTransition(f"Invalid transition {current} -> {target}")


def transition_job(db: Session, job: models.Job, target: str, *, error: str | None = None) -> models.Job:
    _transition(job.status, target, states=JOB_STATES, terminal=JOB_TERMINAL_STATES)
    now = datetime.now(timezone.utc)
    job.status = target
    if target == "running" and job.started_at is None:
        job.started_at = now
    if target in JOB_TERMINAL_STATES:
        job.completed_at = now
    if error is not None:
        job.error = error
    if target == "succeeded":
        job.error = None
        job.retryable = False
        job.failure_code = None
        job.policy_blocked = False
        job.next_retry_at = None
    db.flush()
    return job


def schedule_retry(
    db: Session,
    job: models.Job,
    *,
    error: str,
    failure_code: str | None = None,
    delay_seconds: float = 1.0,
    policy_blocked: bool = False,
) -> models.Job:
    """Put a running job back in the queue without losing failure evidence.

    Retry is deliberately explicit rather than being an implicit transition
    from every exception.  The persisted attempt budget and due time make the
    operation safe across process restarts and multiple polling workers.
    """
    if job.status != "running":
        raise InvalidJobTransition(f"Cannot schedule retry from {job.status}")
    if job.attempt_count >= job.max_attempts:
        raise InvalidJobTransition("Retry budget exhausted")
    now = datetime.now(timezone.utc)
    job.status = "queued"
    job.next_retry_at = now.replace(microsecond=0) + timedelta(seconds=max(0.0, delay_seconds))
    job.retryable = True
    job.policy_blocked = policy_blocked
    job.failure_code = failure_code
    job.error = error
    job.completed_at = None
    db.flush()
    return job


def request_job_retry(
    db: Session,
    job: models.Job,
    *,
    delay_seconds: float = 0.0,
    force: bool = False,
) -> models.Job:
    """Requeue a terminal failed Job for an explicit operator retry.

    Worker retries enter through :func:`schedule_retry` while a job is
    running.  This separate operation handles a terminal failure and is
    intentionally conservative: policy failures cannot be forced, and a
    consumed attempt budget cannot be extended by an API caller.  The payload
    is preserved verbatim so an async provider job ID remains available and a
    retry polls the existing remote task instead of submitting billable work
    a second time.
    """

    if job.type.startswith("batch."):
        raise InvalidJobTransition("Batch status is derived from its items; retry the original child task")
    if job.status == "queued" and job.retryable and job.next_retry_at is not None:
        # A repeated client request is idempotent while the retry is queued.
        return job
    if job.status != "failed":
        raise InvalidJobTransition(f"Cannot retry job from {job.status}")
    if job.policy_blocked:
        raise InvalidJobTransition("Policy-blocked jobs cannot be retried")
    if not job.retryable and not force:
        raise InvalidJobTransition("Job failure is not retryable")
    if job.attempt_count >= job.max_attempts:
        raise InvalidJobTransition("Retry budget exhausted")
    now = datetime.now(timezone.utc)
    next_retry_at = now.replace(microsecond=0) + timedelta(seconds=max(0.0, delay_seconds))
    # Use a compare-and-set update so two operator requests cannot both turn
    # the same terminal failure into independent attempts.  A second request
    # that observes the already queued state is handled by the idempotent
    # branch above; a stale loaded instance is refreshed before reporting a
    # conflict to its caller.
    result = db.execute(
        update(models.Job)
        .where(models.Job.id == job.id, models.Job.status == "failed")
        .values(
            status="queued",
            next_retry_at=next_retry_at,
            completed_at=None,
            # Keep the structured provider failure and code visible until the
            # next attempt produces a replacement audit.
            retryable=True,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.refresh(job)
        if job.status == "queued" and job.retryable and job.next_retry_at is not None:
            return job
        raise InvalidJobTransition("Job changed before retry could be scheduled")
    db.refresh(job)
    # A failed provider step is a previous attempt, not a permanent terminal
    # state.  Reset it before the worker claims the parent Job; otherwise the
    # next run would fail at ``queued -> running`` with an invalid transition.
    for step in db.scalars(
        select(models.JobStep).where(
            models.JobStep.job_id == job.id,
            models.JobStep.status.in_(("failed", "cancelled", "skipped")),
        )
    ):
        reset_step_for_retry(db, step)
    db.flush()
    return job


def claim_job(db: Session, job: models.Job) -> bool:
    """Atomically claim a queued job so two workers cannot process it twice."""
    from .recovery_guard import database_active
    if not database_active(db):return False
    if job.status != "queued":
        return False
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(models.Job)
        .where(
            models.Job.id == job.id,
            models.Job.status == "queued",
            or_(models.Job.next_retry_at.is_(None), models.Job.next_retry_at <= now),
        )
        .values(status="running", started_at=now, attempt_count=models.Job.attempt_count + 1,
                execution_generation=models.Job.execution_generation + 1, next_retry_at=None)
        # SQLite stores timezone-aware datetimes as naive values.  Disabling
        # SQLAlchemy's Python-side evaluator avoids comparing those mixed
        # representations during a retry claim; refresh below gives callers a
        # consistent persisted instance.
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    job.status = "running"
    job.started_at = now
    # The UPDATE is intentionally atomic for concurrent workers.  Refresh the
    # instance before callers compute retry budgets or write audit payloads.
    db.refresh(job)
    job.next_retry_at = None
    db.flush()
    return True


def transition_step(
    db: Session,
    step: models.JobStep,
    target: str,
    *,
    progress: float | None = None,
    error: str | None = None,
) -> models.JobStep:
    _transition(step.status, target, states=STEP_STATES, terminal=STEP_TERMINAL_STATES)
    if progress is not None:
        if not 0.0 <= progress <= 1.0:
            raise InvalidJobTransition("Step progress must be between 0 and 1")
        if progress + 1e-9 < step.progress:
            raise InvalidJobTransition("Step progress cannot move backwards")
    now = datetime.now(timezone.utc)
    step.status = target
    if progress is not None:
        step.progress = progress
    elif target == "succeeded":
        step.progress = 1.0
    if target == "running" and step.started_at is None:
        step.started_at = now
    if target in STEP_TERMINAL_STATES:
        step.completed_at = now
    if error is not None:
        step.error = error
    if target == "succeeded":
        step.error = None
    db.flush()
    return step


def reset_step_for_retry(db: Session, step: models.JobStep, *, error: str | None = None) -> models.JobStep:
    """Requeue a failed step for an explicitly scheduled job retry."""
    if step.status not in STEP_TERMINAL_STATES:
        raise InvalidJobTransition(f"Cannot reset step from {step.status}")
    step.status = "queued"
    step.progress = 0.0
    step.started_at = None
    step.completed_at = None
    step.error = error
    db.flush()
    return step


def create_step(db: Session, job: models.Job, *, name: str, sequence: int = 0, status: str = "queued", progress: float = 0.0, payload: dict | None = None) -> models.JobStep:
    if status not in STEP_STATES:
        raise InvalidJobTransition(f"Unknown state: {status}")
    step = models.JobStep(
        tenant_id=job.tenant_id,
        job_id=job.id,
        name=name,
        sequence=sequence,
        status=status,
        progress=progress,
        payload=payload or {},
    )
    if status == "running":
        step.started_at = datetime.now(timezone.utc)
    if status in STEP_TERMINAL_STATES:
        step.completed_at = datetime.now(timezone.utc)
        if status == "succeeded":
            step.progress = 1.0
    db.add(step)
    return step

