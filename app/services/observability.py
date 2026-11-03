"""Read-only operational aggregates for jobs.

The service deliberately returns aggregate values only.  It never exposes tenant
identifiers or job payloads, which makes the global metrics endpoint safe to
scrape while the richer API remains tenant-scoped.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from .. import models


def _counts(db: Session, stmt: Select[Any]) -> dict[str, int]:
    return {str(key): int(total) for key, total in db.execute(stmt).all() if key is not None}


def job_stats(db: Session, *, tenant_id: str | None = None, project_id: str | None = None) -> dict[str, Any]:
    """Return aggregate Job counters, optionally scoped to one tenant/project."""
    predicates = []
    if tenant_id is not None:
        predicates.append(models.Job.tenant_id == tenant_id)
    if project_id is not None:
        predicates.append(models.Job.project_id == project_id)

    base = select(models.Job).where(*predicates)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    by_status = _counts(
        db,
        select(models.Job.status, func.count())
        .where(*predicates)
        .group_by(models.Job.status),
    )
    by_type = _counts(
        db,
        select(models.Job.type, func.count())
        .where(*predicates)
        .group_by(models.Job.type),
    )
    by_failure_code = _counts(
        db,
        select(models.Job.failure_code, func.count())
        .where(*predicates, models.Job.failure_code.is_not(None))
        .group_by(models.Job.failure_code),
    )
    retryable = int(
        db.scalar(select(func.count()).select_from(models.Job).where(*predicates, models.Job.retryable.is_(True))) or 0
    )
    policy_blocked = int(
        db.scalar(select(func.count()).select_from(models.Job).where(*predicates, models.Job.policy_blocked.is_(True))) or 0
    )
    queued_retry = int(
        db.scalar(
            select(func.count()).select_from(models.Job).where(
                *predicates,
                models.Job.status == "queued",
                models.Job.next_retry_at.is_not(None),
            )
        )
        or 0
    )
    return {
        "total": total,
        "by_status": by_status,
        "by_type": by_type,
        "by_failure_code": by_failure_code,
        "retryable": retryable,
        "policy_blocked": policy_blocked,
        "queued_retry": queued_retry,
        "generated_at": datetime.now(timezone.utc),
    }


def prometheus_job_metrics(db: Session) -> str:
    """Render low-cardinality aggregate Job counters for a scrape endpoint."""
    stats = job_stats(db)
    lines = [
        "# HELP video_ai_jobs Number of jobs by current state.",
        "# TYPE video_ai_jobs gauge",
    ]
    statuses = {}
    for status,count in stats['by_status'].items():
        label = status if status in {'queued','running','succeeded','failed','cancelled'} else 'other'
        statuses[label] = statuses.get(label,0)+count
    for status, count in sorted(statuses.items()):
        lines.append(f'video_ai_jobs{{status="{_label(status)}"}} {count}')
    lines.extend(
        [
            "# HELP video_ai_jobs_retryable Number of jobs marked retryable.",
            "# TYPE video_ai_jobs_retryable gauge",
            f"video_ai_jobs_retryable {stats['retryable']}",
            "# HELP video_ai_jobs_policy_blocked Number of jobs blocked by a policy check.",
            "# TYPE video_ai_jobs_policy_blocked gauge",
            f"video_ai_jobs_policy_blocked {stats['policy_blocked']}",
            "# HELP video_ai_jobs_queued_retry Number of queued jobs waiting for a retry deadline.",
            "# TYPE video_ai_jobs_queued_retry gauge",
            f"video_ai_jobs_queued_retry {stats['queued_retry']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    """Escape a value for a Prometheus label without creating extra dependencies."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
