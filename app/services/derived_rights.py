"""Dynamic rights of adopted AI media and the renders that depend on it.

Checks are metadata-only. They gate new work and delivery, never polling or
accounting for an operation that was already sent to a remote provider.
"""
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import unquote, urlparse

from sqlalchemy import select

from .. import models
from .editing_errors import EditingError


OPERATIONS = {"video_generation", "face_replace", "person_replace"}
MAX_DEPENDENCIES = 1000


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _row(db, model, id):
    return db.scalar(select(model).where(model.id == id).execution_options(populate_existing=True))


def _oss_identity(uri):
    """Match the regional HTTPS form accepted by ICE to its OSS object ID."""
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or ""
        if parsed.scheme == "oss":
            bucket = host
        elif parsed.scheme == "https" and host.endswith(".aliyuncs.com") and ".oss-" in host:
            bucket = host.split(".oss-", 1)[0]
        else:
            return uri
        return f"oss://{bucket}/{unquote(parsed.path.lstrip('/'))}"
    except (TypeError, ValueError):
        return uri


def _active(row, *, tenant, project):
    now = datetime.now(timezone.utc)
    expires = row.expires_at if row else None
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return bool(row and row.tenant_id == tenant and row.project_id in {None, project}
                and not row.revoked_at and (expires is None or expires > now))


def assert_identity_rights(db, *, tenant_id, project_id, identity, operation):
    """Recheck the exact identity reference adopted by a completed production."""
    if not isinstance(identity, dict) or operation not in {"face_replace", "person_replace"}:
        raise EditingError("rights_identity_invalid", "派生素材的人物授权记录不完整", 403)
    profile = _row(db, models.AvatarProfile, identity.get("profile_id"))
    if (not _active(profile, tenant=tenant_id, project=project_id)
            or profile.status in {"revoked", "suspended", "deleted", "consent_pending"}):
        raise EditingError("rights_profile_inactive", "派生素材所依赖的人物档案已失效", 403)
    if (profile.consent_id != identity.get("consent_id")
            or profile.reference_asset_id != identity.get("reference_asset_id")):
        raise EditingError("rights_identity_changed", "人物授权或参考图与生成时不一致", 409)
    scopes = {operation, "avatar", "face"}
    if not scopes.intersection(profile.scopes or []):
        raise EditingError("rights_profile_scope", "人物档案不再允许此派生操作", 403)
    consent = _row(db, models.Consent, identity.get("consent_id"))
    if not _active(consent, tenant=tenant_id, project=project_id) or not consent.granted:
        raise EditingError("rights_consent_inactive", "派生素材所依赖的人物授权已撤回或到期", 403)
    consent_scopes = set(consent.scope.replace(",", " ").lower().split())
    if not scopes.intersection(consent_scopes):
        raise EditingError("rights_consent_scope", "授权范围不再包含此派生操作", 403)


def assert_asset_rights(db, asset, *, tenant_id, project_id, _path=(), _checked=None,
                        _dependencies=None, _output_required=True):
    """Validate a derived chain against authoritative Jobs and frozen inputs.

    Return an input signature suitable for freezing in a new render Job.
    Asset metadata is checked against its originating Job, so removing a
    profile ID from that metadata cannot erase a known identity dependency.
    """
    from .preparation_quotes import source_hash

    checked = _checked if _checked is not None else {}
    dependencies = _dependencies if _dependencies is not None else {"artifacts": set(), "consents": set(), "profiles": set()}
    asset = _row(db, models.Asset, asset.id)
    if asset is None or asset.tenant_id != tenant_id or asset.project_id != project_id:
        raise EditingError("rights_source_unavailable", "派生素材的输入已不可用或不属于当前项目", 403)
    if asset.id in _path or len(_path) >= 32 or len(checked) >= MAX_DEPENDENCIES:
        raise EditingError("rights_dependency_invalid", "派生素材依赖循环或超过可核验范围", 409)
    usage = db.scalar(select(models.ReferenceUsage).where(models.ReferenceUsage.asset_id == asset.id)
                      .execution_options(populate_existing=True))
    if usage and (usage.tenant_id != tenant_id or usage.project_id != project_id or _output_required and not usage.allow_output):
        raise EditingError("rights_source_output_forbidden", "该素材仅供参考，当前不允许用于输出", 403)
    if asset.id in checked:
        return checked[asset.id]
    metadata = asset.metadata_json or {}
    provenance = metadata.get("provenance") or {}
    if not isinstance(provenance, dict):
        raise EditingError("rights_provenance_invalid", "派生素材来源记录不完整", 409)
    signature = {"source_hash": source_hash(asset)}
    if provenance.get("identity") or provenance.get("operation") in OPERATIONS or metadata.get("generated_media"):
        origin = _row(db, models.Job, provenance.get("job_id"))
        production = (origin.payload or {}).get("ai_production") if origin else None
        if (not origin or origin.tenant_id != tenant_id or origin.project_id != project_id
                or origin.status != "succeeded" or origin.policy_blocked
                or not isinstance(production, dict) or production.get("adopted_asset_id") != asset.id):
            raise EditingError("rights_origin_invalid", "无法核对派生素材的原任务和采用记录", 403)
        snapshot, request = production.get("snapshot") or {}, production.get("request") or {}
        identity, inputs = snapshot.get("identity"), snapshot.get("sources")
        operation = request.get("operation")
        if (operation not in OPERATIONS or provenance.get("operation") != operation
                or provenance.get("identity") != identity or not isinstance(inputs, dict)
                or provenance.get("input_sources") != inputs):
            raise EditingError("rights_signature_changed", "派生素材的授权或输入签名与原任务不一致", 409)
        artifact = _row(db, models.Artifact, provenance.get("artifact_id"))
        if (not artifact or artifact.tenant_id != tenant_id or artifact.project_id != project_id
                or artifact.job_id != origin.id or artifact.uri != asset.uri or artifact.legacy_duplicate_of
                or artifact.delivery_status in {"revoked", "deleted", "archived_duplicate"}):
            raise EditingError("rights_origin_output_changed", "派生素材与原输出不一致", 409)
        dependencies["artifacts"].add(artifact.id)
        if identity:
            assert_identity_rights(db, tenant_id=tenant_id, project_id=project_id, identity=identity, operation=operation)
            dependencies["profiles"].add(identity["profile_id"])
            dependencies["consents"].add(identity["consent_id"])
            if identity.get("reference_asset_id") not in inputs:
                raise EditingError("rights_reference_unbound", "人物参考图未绑定到生成输入签名", 403)
        elif operation != "video_generation":
            raise EditingError("rights_identity_invalid", "人物替换缺少授权依赖", 403)
        input_signatures = {}
        for id, expected in sorted(inputs.items()):
            source = _row(db, models.Asset, id)
            if (source is None or source.tenant_id != tenant_id or source.project_id != project_id
                    or source_hash(source) != expected):
                raise EditingError("rights_input_changed", "派生素材的原输入已变化或不可用", 409)
            input_signatures[id] = assert_asset_rights(db, source, tenant_id=tenant_id, project_id=project_id,
                _path=(*_path, asset.id), _checked=checked, _dependencies=dependencies)
        signature["derivation"] = _hash({"job_id": origin.id, "artifact_id": artifact.id,
            "operation": operation, "identity": identity, "input_sources": inputs, "dependencies": input_signatures})
    else:
        # A duplicate registration is still the same media object. Resolve
        # known adopted origins even when this particular ID has no AI tags.
        aliases = list(db.scalars(select(models.Asset).where(models.Asset.id != asset.id,
            models.Asset.uri.in_([asset.uri, _oss_identity(asset.uri)]),
            models.Asset.metadata_json["provenance"]["operation"].as_string().in_(OPERATIONS))
            .order_by(models.Asset.id).limit(MAX_DEPENDENCIES + 1).execution_options(populate_existing=True)))
        if len(aliases) > MAX_DEPENDENCIES:
            raise EditingError("rights_dependency_invalid", "同对象的来源依赖超过可核验范围", 409)
        if aliases:
            signature["object_derivations"] = {alias.id: assert_asset_rights(db, alias, tenant_id=tenant_id,
                project_id=project_id, _path=(*_path, asset.id), _checked=checked, _dependencies=dependencies,
                _output_required=_output_required) for alias in aliases}
    checked[asset.id] = signature
    return signature


def assert_timeline_rights(db, *, tenant_id, project_id, timeline, expected=None, lock_policy=False, _locked=None):
    """Check used sources; preserve legacy timelines whose assets predate DB IDs."""
    if hasattr(timeline, "model_dump"):
        timeline = timeline.model_dump(mode="json")
    if (not isinstance(timeline, dict) or expected is not None and
            (not isinstance(expected, dict) or expected.get("schema_version") != "derived-rights/1"
             or not isinstance(expected.get("sources"), dict))):
        raise EditingError("rights_render_inputs_changed", "渲染授权依赖签名不完整", 409)
    used = {clip.get("asset_id") for track in timeline.get("tracks", []) for clip in track.get("clips", [])
            if clip.get("asset_id")}
    signatures, checked = {}, {}
    dependencies = {"artifacts": set(), "consents": set(), "profiles": set()}
    for ref in sorted(timeline.get("assets", []), key=lambda row: row["id"]):
        if ref.get("id") not in used:
            continue
        asset = _row(db, models.Asset, ref["id"])
        if asset is None:
            # An alias of a known generated object must retain its rights too.
            uri = ref.get("uri")
            asset = db.scalar(select(models.Asset).where(models.Asset.uri.in_([uri, _oss_identity(uri)]))
                .order_by((models.Asset.tenant_id == tenant_id).desc(),
                          (models.Asset.project_id == project_id).desc(), models.Asset.id).limit(1)
                .execution_options(populate_existing=True))
        if asset is None:
            if expected and ref["id"] in expected.get("sources", {}):
                raise EditingError("rights_source_unavailable", "渲染绑定的素材已不可用", 403)
            continue
        if _oss_identity(asset.uri) != _oss_identity(ref.get("uri")):
            raise EditingError("rights_source_changed", "渲染输入与当前素材对象不一致", 409)
        signatures[ref["id"]] = assert_asset_rights(db, asset, tenant_id=tenant_id, project_id=project_id,
                                                   _checked=checked, _dependencies=dependencies)
    result = {"schema_version": "derived-rights/1", "sources": signatures}
    if expected is not None and result != expected:
        raise EditingError("rights_render_inputs_changed", "渲染输入的授权依赖或对象签名已变化", 409)
    dependencies["assets"] = set(checked)
    if _locked is not None and dependencies != _locked:
        raise EditingError("rights_render_inputs_changed", "准入期间授权依赖已变化，请重新核对", 409)
    if lock_policy:
        # The caller holds the render Job lock. Lock the whole discovered set
        # in one global order, before its budget/dispatch locks. Consent comes
        # before Profile to match deletion's FK RESTRICT check.
        for model, field, ids in ((models.Asset, models.Asset.id, dependencies["assets"]),
                (models.ReferenceUsage, models.ReferenceUsage.asset_id, dependencies["assets"]),
                (models.Artifact, models.Artifact.id, dependencies["artifacts"]),
                (models.Consent, models.Consent.id, dependencies["consents"]),
                (models.AvatarProfile, models.AvatarProfile.id, dependencies["profiles"])):
            if ids:
                list(db.scalars(select(field).where(field.in_(sorted(ids))).order_by(field).with_for_update()))
        return assert_timeline_rights(db, tenant_id=tenant_id, project_id=project_id, timeline=timeline,
                                     expected=result, _locked=dependencies)
    return result


def assert_job_rights(db, job, *, lock_policy=False):
    payload = job.payload or {}
    manifest = payload.get("manifest")
    if manifest is None and "derived_rights" not in payload:
        return None  # Preserve historical artifacts with no render input record.
    source = manifest.get("source") if isinstance(manifest, dict) else None
    timeline = source.get("timeline") if isinstance(source, dict) else None
    if not isinstance(timeline, dict):
        raise EditingError("rights_render_inputs_missing", "无法核对成片的原输入和授权依赖", 409)
    return assert_timeline_rights(db, tenant_id=job.tenant_id, project_id=job.project_id,
                                 timeline=timeline, expected=payload.get("derived_rights"), lock_policy=lock_policy)


def asset_playback(db, asset, *, tenant_id, gateway, expected_source_hash=None):
    """Sign a source preview without allowing a derived-rights bypass.

    Ordinary references may still be viewed while excluded from output.
    A generated result inherits its input/output rights even for preview.
    """
    from .preparation_quotes import source_hash
    from .shot_matching import ready

    project_id = asset.project_id
    def signature(current):
        if (current is None or current.tenant_id != tenant_id or current.project_id != project_id
                or not ready(current) or not current.kind.startswith(("video", "image"))):
            raise EditingError("rights_source_unavailable", "素材已不可用或不属于当前项目", 403)
        fingerprint = source_hash(current)
        if expected_source_hash is not None and fingerprint != expected_source_hash:
            raise EditingError("rights_source_changed", "素材对象与参考记录不一致", 409)
        return assert_asset_rights(db, current, tenant_id=tenant_id, project_id=project_id, _output_required=False)
    asset = _row(db, models.Asset, asset.id)
    before = signature(asset)
    id, uri = asset.id, asset.uri
    db.commit()
    grant = gateway.artifact_access(artifact_id=id, tenant_id=tenant_id, object_uri=uri, purpose="preview", ttl_seconds=300)
    db.expire_all()
    current = _row(db, models.Asset, id)
    if signature(current) != before or current.uri != uri:
        raise EditingError("rights_source_changed", "签名期间素材或授权依赖已变化", 409)
    return grant
