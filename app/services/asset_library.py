"""User metadata and resumable upload discovery; never accepts media bytes."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models, schemas
from .storage_gateway import StorageGatewayError


def upload_intent_read(row: models.StorageUploadIntent) -> schemas.UploadIntentRead:
    return schemas.UploadIntentRead(asset_id=row.asset_id, project_id=row.project_id,
        idempotency_key=row.idempotency_key, specification=row.request_json,
        status=row.status, last_error_code=row.last_error_code,
        expires_at=row.expires_at, created_at=row.created_at)


def update_asset_labels(db: Session, *, tenant_id: str, asset_id: str,
                        changes: schemas.AssetLabelsUpdate) -> models.Asset:
    row = db.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.tenant_id == tenant_id)
                    .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise StorageGatewayError("asset_not_found", "未找到素材", status_code=404)
    metadata = dict(row.metadata_json or {})
    revision = int(metadata.get("user_labels_revision") or 0)
    if changes.base_revision != revision:
        raise StorageGatewayError("asset_labels_conflict", "素材标签已在其他页面更新，请刷新后再修改", status_code=409)
    normalized = {}
    for label in changes.tags:
        label = label.strip()
        if not label or max(len(label), len(label.casefold())) > 64 or any(ord(c) < 32 for c in label):
            raise StorageGatewayError("asset_label_invalid", "标签需为1至64个字符", status_code=422)
        normalized.setdefault(label.casefold(), label)
    if changes.name is not None:
        name = changes.name.strip()
        if not name:
            raise StorageGatewayError("asset_name_invalid", "素材名称不能为空", status_code=422)
        row.name = name
    # Client metadata has a narrow namespace; source, analysis, integrity,
    # licensing and readiness cannot be set by this action.
    metadata.update(user_tags=list(normalized.values()), user_note=changes.note,
                    user_labels_revision=revision + 1)
    row.metadata_json = metadata
    db.execute(delete(models.AssetLabel).where(models.AssetLabel.asset_id == row.id,
                                               models.AssetLabel.tenant_id == tenant_id))
    db.add_all(models.AssetLabel(tenant_id=tenant_id, asset_id=row.id, tag_key=key, label=value)
               for key, value in normalized.items())
    db.commit()
    return row
