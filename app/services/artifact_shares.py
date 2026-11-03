"""Scoped, expiring shares. Tokens are derived on demand and only hashes persist."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import Settings
from ..delivery_schemas import ShareCreate
from .artifact_delivery import _artifact, artifact_access
from .artifact_reviews import can_review, review_report
from .delivery_packages import delivery_file, utc
from .editing_errors import EditingError
from .editing import digest
from .storage_gateway import StorageGatewayError


def share_configuration(settings: Settings) -> dict:
    origin = urlsplit(settings.auth_public_origin or "")
    valid = (origin.scheme in {"http","https"} and bool(origin.hostname) and not (origin.username or origin.password or origin.path or origin.query or origin.fragment)
             and (settings.app_environment != "production" or origin.scheme == "https"))
    enabled = bool(settings.artifact_share_secret and valid)
    return {"available":enabled,"max_seconds":604800,"media_url_max_seconds":300,
            "reason":"可创建绑定当前批准的受控分享" if enabled else "分享服务尚未配置，仍可在个人作品页审阅和领取"}


def _key(settings: Settings) -> bytes:
    if not share_configuration(settings)["available"]:
        raise EditingError("share_unconfigured","分享服务尚未配置",503)
    return settings.artifact_share_secret.get_secret_value().encode()


def _key_version(settings: Settings) -> str:
    return hashlib.sha256(_key(settings)).hexdigest()


def _token(row: models.ArtifactShare, settings: Settings) -> str:
    signature = hmac.new(_key(settings),("artifact-share/1:"+row.id).encode(),hashlib.sha256).digest()
    return "sg1."+row.id+"."+base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _unavailable():
    return StorageGatewayError("share_unavailable","当前分享不可访问，可能已过期、撤销或重新审核",status_code=404)


def _owner(db: Session,tenant_id: str,artifact_id: str,actor_id: str | None):
    row = _artifact(db,tenant_id,artifact_id)
    if not can_review(db,tenant_id,actor_id):
        raise EditingError("share_permission_denied","当前仅空间所有者可创建或撤销分享",403)
    return row


def _active_report(db: Session,row: models.ArtifactShare,settings: Settings) -> dict:
    if row.revoked_at is not None or utc(row.expires_at) <= datetime.now(timezone.utc):
        raise _unavailable()
    if row.key_version != _key_version(settings):
        raise _unavailable()
    try:
        report = review_report(db,tenant_id=row.tenant_id,artifact_id=row.artifact_id)
        if (not report["can_download"] or report["binding_hash"] != row.binding_hash or report["latest"]["id"] != row.review_id):
            raise _unavailable()
        return report
    except (EditingError,StorageGatewayError) as exc:
        raise _unavailable() from exc


def share_read(db: Session,row: models.ArtifactShare,settings: Settings,*,include_link: bool=False) -> dict:
    if row.revoked_at is not None: status = "revoked"
    elif utc(row.expires_at) <= datetime.now(timezone.utc): status = "expired"
    elif not share_configuration(settings)["available"]: status = "unavailable"
    elif row.key_version != _key_version(settings): status = "key_changed"
    else:
        try: _active_report(db,row,settings); status = "active"
        except StorageGatewayError: status = "invalidated"
    result = {"id":row.id,"artifact_id":row.artifact_id,"label":row.label,"audience":row.audience,
        "allow_download":row.allow_download,"include_subtitles":row.include_subtitles,"expires_at":utc(row.expires_at),
        "created_at":utc(row.created_at),"revoked_at":utc(row.revoked_at) if row.revoked_at else None,"status":status,
        "binding_hash":row.binding_hash,"review_id":row.review_id,"url":None,
        "access_note":"撤销会阻止新授权；已签发的媒体地址最长仍可使用5分钟，且不超过分享期限。"}
    if include_link and status == "active":
        result["url"] = settings.auth_public_origin+"/share#"+_token(row,settings)
    return result


def create_share(db: Session,*,tenant_id: str,artifact_id: str,actor_id: str | None,key: str,
                 request: ShareCreate,settings: Settings) -> models.ArtifactShare:
    _owner(db,tenant_id,artifact_id,actor_id)
    _artifact(db,tenant_id,artifact_id,lock=True)
    actor = actor_id or "development_user"; request_hash = digest({"actor_id":actor,"request":request.model_dump(mode="json")})
    existing = db.scalar(select(models.ArtifactShare).where(models.ArtifactShare.artifact_id==artifact_id,models.ArtifactShare.idempotency_key==key))
    if existing:
        if existing.request_hash != request_hash:
            raise EditingError("share_request_conflict","同一分享操作标识已经用于不同设置")
        return existing
    version = _key_version(settings); report = review_report(db,tenant_id=tenant_id,artifact_id=artifact_id)
    if not report["can_download"]:
        raise EditingError("share_approval_required","请先审阅并批准这份成片，再创建分享")
    if report["binding_hash"] != request.binding_hash or report["latest"]["id"] != request.review_id:
        raise EditingError("share_approval_changed","批准记录已变化，请载入当前状态后重新设置分享")
    archive = db.get(models.ArtifactStorage,artifact_id)
    expires = min(datetime.now(timezone.utc)+timedelta(seconds=request.expires_in_seconds),utc(archive.expires_at))
    row = models.ArtifactShare(id=models.new_id(),tenant_id=tenant_id,artifact_id=artifact_id,actor_id=actor,
        review_id=request.review_id,binding_hash=request.binding_hash,key_version=version,token_hash="",
        label=request.label,audience=request.audience,allow_download=request.allow_download,include_subtitles=request.include_subtitles,
        expires_at=expires,idempotency_key=key,request_hash=request_hash)
    row.token_hash = hashlib.sha256(_token(row,settings).encode()).hexdigest()
    db.add(row); db.flush(); return row


def owner_shares(db: Session,*,tenant_id: str,artifact_id: str,actor_id: str | None,skip: int,limit: int):
    _owner(db,tenant_id,artifact_id,actor_id)
    return db.scalars(select(models.ArtifactShare).where(models.ArtifactShare.artifact_id==artifact_id)
        .order_by(models.ArtifactShare.created_at.desc(),models.ArtifactShare.id.desc()).offset(skip).limit(limit))


def revoke_share(db: Session,*,tenant_id: str,artifact_id: str,actor_id: str | None,share_id: str):
    _owner(db,tenant_id,artifact_id,actor_id); _artifact(db,tenant_id,artifact_id,lock=True)
    row = db.scalar(select(models.ArtifactShare).where(models.ArtifactShare.id==share_id,models.ArtifactShare.artifact_id==artifact_id)
                    .with_for_update().execution_options(populate_existing=True))
    if row is None: raise EditingError("share_not_found","未找到分享记录",404)
    if row.revoked_at is None: row.revoked_at = datetime.now(timezone.utc)
    db.flush(); return row


def lookup_share(db: Session,token: str,settings: Settings) -> models.ArtifactShare:
    if not share_configuration(settings)["available"]: raise _unavailable()
    row = db.scalar(select(models.ArtifactShare).where(models.ArtifactShare.token_hash==hashlib.sha256(token.encode()).hexdigest()))
    if row is None or not hmac.compare_digest(_token(row,settings),token): raise _unavailable()
    _active_report(db,row,settings)
    return row


def authorize_audience(row: models.ArtifactShare,authenticated_tenant_id: str | None):
    if row.audience == "workspace" and not authenticated_tenant_id:
        raise StorageGatewayError("share_login_required","此分享仅限同一工作空间，请先登录",status_code=401)
    if row.audience == "workspace" and authenticated_tenant_id != row.tenant_id:
        raise _unavailable()


def public_summary(db: Session,row: models.ArtifactShare,settings: Settings) -> dict:
    report = _active_report(db,row,settings)
    return {"label":row.label,"expires_at":utc(row.expires_at),"allow_download":row.allow_download,
            "include_subtitles":row.include_subtitles,"edit_revision":report["revision_number"],"duration":report["duration"],
            "measurements":report["measurements"],"review_status":"approved",
            "access_note":"授权可被撤销；已领取的文件无法收回。媒体地址最长5分钟有效，过期可在分享期限内刷新。"}


def shared_access(db: Session,row: models.ArtifactShare,*,settings: Settings,gateway,purpose: str) -> dict:
    _active_report(db,row,settings)
    if purpose == "download" and not row.allow_download:
        raise StorageGatewayError("share_download_forbidden","此分享仅允许播放",status_code=403)
    share_id = row.id; tenant = row.tenant_id; artifact = row.artifact_id; expiry = utc(row.expires_at)
    value = artifact_access(db,tenant_id=tenant,artifact_id=artifact,purpose=purpose,gateway=gateway,expires_at_limit=expiry)
    db.expire_all(); current = db.get(models.ArtifactShare,share_id)
    if current is None: raise _unavailable()
    _active_report(db,current,settings)
    if value["expires_at"] > utc(current.expires_at): raise _unavailable()
    # Public response never exposes internal artifact IDs or object locations.
    return {"url":value["url"],"expires_at":value["expires_at"],"purpose":purpose}


def shared_file(db: Session,row: models.ArtifactShare,*,settings: Settings,kind: str) -> dict:
    _active_report(db,row,settings)
    if not row.allow_download or (kind in {"srt","vtt"} and not row.include_subtitles):
        raise StorageGatewayError("share_file_forbidden","此分享未开放该文件",status_code=403)
    result = delivery_file(db,tenant_id=row.tenant_id,artifact_id=row.artifact_id,kind=kind,visibility="public",expected_binding=row.binding_hash)
    share_id=row.id
    db.expire_all()
    current=db.get(models.ArtifactShare,share_id)
    if current is None: raise _unavailable()
    _active_report(db,current,settings)
    # Export files contain only approved text/output information. Keep private
    # artifact identity and binding internals out of the public envelope too.
    return {**{key:result[key] for key in ("content","sha256","encoding","kind","warnings")},
            "filename":"video-delivery.json" if kind=="manifest" else "video-subtitles."+kind}
