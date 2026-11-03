"""One private review contract shared by web and native work detail views."""
from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .deps import RequestIdentity, authenticate_request, require_tenant_id
from .identity import PrivateValidationRoute
from .. import models
from ..database import get_db
from ..review_schemas import ReviewSubmit
from ..services.artifact_delivery import _artifact
from ..services.artifact_reviews import review_read, review_report, submit_review

router = APIRouter(tags=["reviews"], route_class=PrivateValidationRoute)


@router.get("/artifacts/{artifact_id}/review")
def current_review(artifact_id: str, tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                   identity: RequestIdentity = Depends(authenticate_request)):
    return review_report(db, tenant_id=tenant_id, artifact_id=artifact_id, actor_id=identity.principal_id)


@router.post("/artifacts/{artifact_id}/reviews", status_code=201)
def review_output(artifact_id: str, payload: ReviewSubmit,
                  key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
                  tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db),
                  identity: RequestIdentity = Depends(authenticate_request)):
    args = {"tenant_id": tenant_id, "artifact_id": artifact_id, "actor_id": identity.principal_id,
            "key": key, "request": payload}
    try:
        row = submit_review(db, **args)
        db.commit()
    except IntegrityError:
        db.rollback()
        # Identical concurrent receipts replay; a competing decision returns CAS conflict.
        row = submit_review(db, **args)
        db.commit()
    return {"review": review_read(row), "current": review_report(db, tenant_id=tenant_id,
        artifact_id=artifact_id, actor_id=identity.principal_id)}


@router.get("/artifacts/{artifact_id}/review-requests/{key}")
def review_receipt(artifact_id: str, key: str, tenant_id: str = Depends(require_tenant_id),
                   db: Session = Depends(get_db), identity: RequestIdentity = Depends(authenticate_request)):
    _artifact(db, tenant_id, artifact_id)
    row = db.scalar(select(models.ArtifactReview).where(models.ArtifactReview.artifact_id == artifact_id,
                                                       models.ArtifactReview.idempotency_key == key))
    return {"found": row is not None, "review": review_read(row) if row else None,
            "current": review_report(db, tenant_id=tenant_id, artifact_id=artifact_id, actor_id=identity.principal_id)}


@router.get("/artifacts/{artifact_id}/reviews")
def review_history(artifact_id: str, skip: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                   tenant_id: str = Depends(require_tenant_id), db: Session = Depends(get_db)):
    _artifact(db, tenant_id, artifact_id)
    rows = db.scalars(select(models.ArtifactReview).where(models.ArtifactReview.artifact_id == artifact_id)
                      .order_by(models.ArtifactReview.number.desc()).offset(skip).limit(limit))
    return [review_read(row) for row in rows]
