"""Versioned personal presets, shared by both clients; no media execution."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models


VERSION = "m2-1"


def _preset(key: str, name: str, duration: int, support: int) -> dict:
    return {"key": key, "name": name, "content": {"schema_version": "1.0", "id": key, "version": VERSION,
        "fps": 30, "duration": duration,
        "slots": [{"name": "hook", "type": "video", "required": True, "description": "开场镜头",
                   "constraints": {"allowed_asset_types": ["video"], "max_items": 1}}] +
                 ([{"name": "body", "type": "video", "required": False, "description": "补充镜头",
                    "constraints": {"allowed_asset_types": ["video"], "max_items": support}}] if support else []),
        "metadata": {"resolution": {"width": 1080, "height": 1920}, "preset_version": VERSION,
                     "real_acceptance": "pending_m2"}}}


PRESETS = (_preset("vertical-reel", "竖屏短视频", 12, 2), _preset("product-demo", "商品展示", 15, 2),
           _preset("quick-cut", "轻量短剪", 6, 0))


def ensure_presets(db: Session, *, tenant_id: str, project_id: str) -> list[models.Template]:
    rows = []
    for preset in PRESETS:
        row_id = str(uuid5(NAMESPACE_URL, f"video-ai-studio:preset:{tenant_id}:{project_id}:{preset['key']}:{VERSION}"))
        row = db.get(models.Template, row_id)
        if row is None:
            try:
                with db.begin_nested():
                    row = models.Template(id=row_id, tenant_id=tenant_id, project_id=project_id,
                        name=preset["name"], version=VERSION, content=deepcopy(preset["content"]))
                    db.add(row); db.flush()
            except IntegrityError:
                row = db.scalar(select(models.Template).where(models.Template.id == row_id,
                    models.Template.tenant_id == tenant_id, models.Template.project_id == project_id))
                if row is None:
                    raise
        rows.append(row)
    return rows
