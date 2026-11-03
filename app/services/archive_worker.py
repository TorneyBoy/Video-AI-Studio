"""Recoverable metadata-only archive queue; the media gateway handles bytes."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..config import Settings
from .artifact_delivery import archive_artifact
from .storage_gateway import StorageGateway, StorageGatewayError


MAX_ATTEMPTS = 5


def process_archive_tasks(db: Session, *, settings: Settings, limit: int = 10,
                          gateway: StorageGateway | None = None) -> list[dict]:
    from .recovery_guard import database_active
    if not database_active(db):return []
    if not settings.storage_gateway_url or not settings.storage_gateway_api_key:
        return []
    gateway = gateway or StorageGateway(settings)
    # Reconcile committed results rather than consuming a shared notification
    # marker. A crash after render completion cannot lose archive scheduling.
    candidates = list(db.scalars(select(models.Artifact)
        .join(models.Job, models.Job.id == models.Artifact.job_id)
        .outerjoin(models.ArtifactArchiveTask, models.ArtifactArchiveTask.artifact_id == models.Artifact.id)
        .where(models.ArtifactArchiveTask.artifact_id.is_(None), models.Job.status == "succeeded",
               models.Job.policy_blocked.is_(False), models.Job.type.like("render.%"),
               models.Artifact.legacy_duplicate_of.is_(None),
               models.Artifact.delivery_status.not_in(["revoked", "deleted", "archived_duplicate"]))
        .order_by(models.Artifact.created_at).limit(max(1, limit))))
    for row in candidates:
        qc = (row.metadata_json or {}).get("quality_control")
        if not isinstance(qc, dict) or qc.get("status") != "passed":
            continue
        try:
            with db.begin_nested():
                db.add(models.ArtifactArchiveTask(artifact_id=row.id, tenant_id=row.tenant_id))
                db.flush()
        except IntegrityError:
            # Another reconciler already created this artifact's only task.
            pass
    db.commit()
    now = datetime.now(timezone.utc)
    due = or_(models.ArtifactArchiveTask.status == "pending",
              (models.ArtifactArchiveTask.status == "running") & (models.ArtifactArchiveTask.lease_expires_at <= now))
    ids = list(db.scalars(select(models.ArtifactArchiveTask.artifact_id).where(
        due, models.ArtifactArchiveTask.attempts < MAX_ATTEMPTS,
        or_(models.ArtifactArchiveTask.next_attempt_at.is_(None), models.ArtifactArchiveTask.next_attempt_at <= now))
        .order_by(models.ArtifactArchiveTask.created_at).limit(max(1, limit))))
    db.commit()
    results = []
    for artifact_id in ids:
        token = uuid4().hex
        claimed = db.execute(update(models.ArtifactArchiveTask).where(
            models.ArtifactArchiveTask.artifact_id == artifact_id, due,
            models.ArtifactArchiveTask.attempts < MAX_ATTEMPTS,
            or_(models.ArtifactArchiveTask.next_attempt_at.is_(None), models.ArtifactArchiveTask.next_attempt_at <= now))
            .values(status="running", lease_token=token, lease_expires_at=now + timedelta(minutes=5),
                    attempts=models.ArtifactArchiveTask.attempts + 1))
        db.commit()
        if claimed.rowcount != 1:
            continue
        task = db.get(models.ArtifactArchiveTask, artifact_id)
        tenant_id, attempts = task.tenant_id, task.attempts
        db.commit()
        error = None
        try:
            archive_artifact(db, tenant_id=tenant_id, artifact_id=artifact_id, settings=settings, gateway=gateway)
        except Exception as exc:
            db.rollback()
            # Never store upstream exception messages or signed URLs.
            error = exc.code if isinstance(exc, StorageGatewayError) else "storage_archive_unavailable"
        permanent = error in {"artifact_not_found", "artifact_delivery_forbidden", "artifact_changed",
                              "storage_archive_verification_failed"}
        status = "succeeded" if error is None else "failed" if permanent or attempts >= MAX_ATTEMPTS else "pending"
        db.execute(update(models.ArtifactArchiveTask).where(
            models.ArtifactArchiveTask.artifact_id == artifact_id, models.ArtifactArchiveTask.lease_token == token)
            .values(status=status, lease_token=None, lease_expires_at=None, last_error=error,
                    next_attempt_at=now + timedelta(seconds=min(300, 10 * 2 ** attempts)) if status == "pending" else None))
        db.commit()
        results.append({"artifact_id": artifact_id, "archive_status": status, "error": error})
    # A crash on the last attempt leaves an expired running claim. Make its
    # exhausted state explicit rather than leaving it apparently in progress.
    db.execute(update(models.ArtifactArchiveTask).where(
        models.ArtifactArchiveTask.status == "running", models.ArtifactArchiveTask.attempts >= MAX_ATTEMPTS,
        models.ArtifactArchiveTask.lease_expires_at <= datetime.now(timezone.utc))
        .values(status="failed", lease_token=None, lease_expires_at=None, last_error="archive_attempts_exhausted"))
    db.commit()
    return results
