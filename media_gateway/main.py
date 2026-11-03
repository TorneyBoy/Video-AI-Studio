"""Internal gateway deployed on the media host, separate from the control API.

Run: uvicorn media_gateway.main:create_app --factory --host 127.0.0.1 --port 8100
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import JSON, DateTime, String, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from app.services.storage_attestation import sign_attestation
from app.services.runtime_logging import RequestObservation,configure_logging
from .oss import ObjectStoreError, OSSStore
from .credentials import oss_credentials
from .database import Base
from .render_jobs import register_render_routes
from .analysis_jobs import register_analysis_routes
from .package_jobs import register_package_routes


class GatewaySettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    api_key: SecretStr = Field(min_length=32)
    attestation_secret: SecretStr = Field(min_length=32)
    database_url: str = "sqlite:///./data/gateway.db"
    oss_bucket: str
    oss_region: str
    oss_internal: bool = False
    credential_mode: str = "environment"
    ecs_role_name: str | None = None
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0, le=5 * 1024**3)
    ice_render_enabled: bool = False
    ice_render_max_operations: int = Field(default=6, ge=1, le=10000)
    ice_render_max_duration_seconds: float = Field(default=30, gt=0, le=3600)
    analysis_enabled: bool = False
    analysis_fc_account_id: str | None = None
    analysis_fc_function_name: str = "video-ai-shot-analysis"
    analysis_max_operations: int = Field(default=6, ge=1, le=10000)
    analysis_max_duration_seconds: float = Field(default=30, gt=0, le=10800)
    analysis_max_thumbnails: int = Field(default=30, ge=0, le=300)
    package_enabled: bool = False
    package_max_operations: int = Field(default=6,ge=1,le=10000)
    package_max_bytes: int = Field(default=100*1024*1024,ge=1,le=1024**3)
    package_temp_root: str = './data/package-work'

    @classmethod
    def from_env(cls):
        return cls(api_key=os.environ["STORAGE_GATEWAY_API_KEY"],
                   attestation_secret=os.environ["STORAGE_ATTESTATION_SECRET"],
                   database_url=os.getenv("GATEWAY_DATABASE_URL", "sqlite:///./data/gateway.db"),
                   oss_bucket=os.environ["OSS_BUCKET"], oss_region=os.environ["OSS_REGION"],
                   credential_mode=os.getenv("OSS_CREDENTIAL_MODE", "environment"),
                   ecs_role_name=os.getenv("ECS_RAM_ROLE_NAME") or None,
                   oss_internal=os.getenv("OSS_USE_INTERNAL_ENDPOINT", "false").lower() == "true",
                   max_upload_bytes=int(os.getenv("GATEWAY_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))),
                   ice_render_enabled=os.getenv("GATEWAY_ICE_RENDER_ENABLED", "false").lower() == "true",
                   ice_render_max_operations=int(os.getenv("GATEWAY_ICE_RENDER_MAX_OPERATIONS", "6")),
                   ice_render_max_duration_seconds=float(os.getenv("GATEWAY_ICE_RENDER_MAX_DURATION_SECONDS", "30")),
                   analysis_enabled=os.getenv("GATEWAY_ANALYSIS_ENABLED", "false").lower() == "true",
                   analysis_fc_account_id=os.getenv("GATEWAY_ANALYSIS_FC_ACCOUNT_ID") or None,
                   analysis_fc_function_name=os.getenv("GATEWAY_ANALYSIS_FC_FUNCTION_NAME", "video-ai-shot-analysis"),
                   analysis_max_operations=int(os.getenv("GATEWAY_ANALYSIS_MAX_OPERATIONS", "6")),
                   analysis_max_duration_seconds=float(os.getenv("GATEWAY_ANALYSIS_MAX_DURATION_SECONDS", "30")),
                   analysis_max_thumbnails=int(os.getenv("GATEWAY_ANALYSIS_MAX_THUMBNAILS", "30")),
                   package_enabled=os.getenv('GATEWAY_PACKAGE_ENABLED','false').lower()=='true',
                   package_max_operations=int(os.getenv('GATEWAY_PACKAGE_MAX_OPERATIONS','6')),
                   package_max_bytes=int(os.getenv('GATEWAY_PACKAGE_MAX_BYTES','104857600')),
                   package_temp_root=os.getenv('GATEWAY_PACKAGE_TEMP_ROOT','./data/package-work'))


class UploadIntent(Base):
    __tablename__ = "gateway_upload_intents"
    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    specification: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified: Mapped[dict | None] = mapped_column(JSON)


class ArchiveIntent(Base):
    __tablename__ = "gateway_archive_intents"
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    verified: Mapped[dict | None] = mapped_column(JSON)


ID = r"^[A-Za-z0-9_-]{1,64}$"


class UploadRequest(BaseModel):
    asset_id: str = Field(pattern=ID)
    tenant_id: str = Field(pattern=ID)
    project_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    content_type: str = Field(pattern=r"^(video|audio|image)/[A-Za-z0-9.+-]{1,100}$")
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    ttl_seconds: int = Field(default=3600, ge=60, le=3600)


class VerifyRequest(BaseModel):
    asset_id: str = Field(pattern=ID)
    tenant_id: str = Field(pattern=ID)
    object_uri: str = Field(max_length=4096)


class AccessRequest(BaseModel):
    artifact_id: str = Field(pattern=ID)
    tenant_id: str = Field(pattern=ID)
    object_uri: str = Field(max_length=4096)
    purpose: str = Field(pattern=r"^(preview|download)$")
    ttl_seconds: int = Field(default=300, ge=1, le=300)


class ArchiveRequest(BaseModel):
    artifact_id: str = Field(pattern=ID)
    tenant_id: str = Field(pattern=ID)
    source_uri: str = Field(max_length=4096)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def create_app(settings: GatewaySettings | None = None, store=None, *, ice_client=None, shot_client=None) -> FastAPI:
    settings = settings or GatewaySettings.from_env()
    if settings.database_url.startswith("sqlite:///./data/"):
        Path("data").mkdir(exist_ok=True)
    engine = create_engine(settings.database_url, **({"connect_args": {"check_same_thread": False}}
                                                    if settings.database_url.startswith("sqlite") else {}))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    store = store or OSSStore(bucket=settings.oss_bucket, region=settings.oss_region, internal=settings.oss_internal,
                              credentials_provider=oss_credentials(settings.credential_mode, role_name=settings.ecs_role_name))
    app = FastAPI(title="Video AI Studio media gateway", docs_url=None, redoc_url=None)
    configure_logging();app.add_middleware(RequestObservation)
    app.state.engine, app.state.sessions, app.state.store = engine, sessions, store

    def authorize(authorization: str = Header(default="")):
        expected = "Bearer " + settings.api_key.get_secret_value()
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise ObjectStoreError("gateway_unauthorized", "Gateway authorization required", 401)

    @app.exception_handler(ObjectStoreError)
    async def object_error(request: Request, exc: ObjectStoreError):
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": str(exc)})

    @app.exception_handler(Exception)
    async def provider_error(request: Request, exc: Exception):
        # SDK exceptions may contain signed requests or credential identifiers.
        return JSONResponse(status_code=502, content={"code": "object_store_unavailable", "message": "Object store could not confirm the operation"})

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "storage-gateway", "cloud_verified": False}

    @app.post("/v1/uploads", dependencies=[Depends(authorize)])
    def upload(payload: UploadRequest):
        if payload.size_bytes > settings.max_upload_bytes:
            raise ObjectStoreError("upload_too_large", "Upload exceeds the gateway limit", 413)
        specification = payload.model_dump(exclude={"ttl_seconds"})
        digest = hashlib.sha256(json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        expiry = datetime.now(timezone.utc) + timedelta(seconds=payload.ttl_seconds)
        key = f"tenants/{payload.tenant_id}/uploads/{payload.asset_id}/source"
        with sessions() as db:
            row = db.get(UploadIntent, payload.asset_id)
            if row is None:
                row = UploadIntent(asset_id=payload.asset_id, tenant_id=payload.tenant_id, request_hash=digest,
                                   object_key=key, specification=specification, expires_at=expiry)
                db.add(row)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    row = db.get(UploadIntent, payload.asset_id)
            if row is None or row.tenant_id != payload.tenant_id or row.request_hash != digest:
                raise ObjectStoreError("upload_intent_conflict", "Upload identity is bound to another request", 409)
        grant = store.grant(key=key, size_bytes=payload.size_bytes, content_type=payload.content_type, expires_at=expiry)
        actual_expiry = datetime.fromisoformat(grant["expires_at"])
        with sessions.begin() as db:
            row = db.get(UploadIntent, payload.asset_id, with_for_update=True)
            row.expires_at = max(_utc(row.expires_at), actual_expiry)
        return {"asset_id": payload.asset_id, "object_uri": store.object_uri(key), **grant}

    @app.post("/v1/uploads/{asset_id}/verify", dependencies=[Depends(authorize)])
    def verify(asset_id: str, payload: VerifyRequest):
        with sessions() as db:
            row = db.get(UploadIntent, asset_id)
            if not row or row.tenant_id != payload.tenant_id or payload.asset_id != asset_id:
                raise ObjectStoreError("upload_not_found", "Upload intent not found", 404)
            key, specification, expiry, observed = row.object_key, row.specification, _utc(row.expires_at), row.verified
        if payload.object_uri != store.object_uri(key):
            raise ObjectStoreError("object_identity_mismatch", "Object does not match its upload intent", 409)
        if expiry <= datetime.now(timezone.utc):
            raise ObjectStoreError("upload_expired", "Renew the upload intent before verifying it", 410)
        if observed is None:
            observed = store.observe(key=key, expected_size=specification["size_bytes"], expected_hash=specification["sha256"])
            with sessions.begin() as db:
                row = db.get(UploadIntent, asset_id, with_for_update=True)
                if row.verified is None:
                    row.verified = observed
                else:
                    observed = row.verified
        proof_expiry = min(expiry, datetime.now(timezone.utc) + timedelta(minutes=5))
        nonce = secrets.token_hex(16)
        signature = sign_attestation(settings.attestation_secret.get_secret_value(), asset_id=asset_id,
                                     object_uri=payload.object_uri, size_bytes=observed["size_bytes"],
                                     sha256=observed["sha256"], expires_at=proof_expiry, nonce=nonce)
        return {"asset_id": asset_id, "object_uri": payload.object_uri, **observed,
                "attestation": {"expires_at": proof_expiry.isoformat(), "nonce": nonce, "signature": signature}}

    @app.post("/v1/artifacts/access", dependencies=[Depends(authorize)])
    def access(payload: AccessRequest):
        key = store.key_from_uri(payload.object_uri, payload.tenant_id)
        return {"artifact_id": payload.artifact_id, "object_uri": payload.object_uri,
                **store.access(key=key, ttl_seconds=payload.ttl_seconds, purpose=payload.purpose)}

    @app.post("/v1/artifacts/archive", dependencies=[Depends(authorize)])
    def archive(payload: ArchiveRequest):
        # Initial integration accepts only the managed same-tenant OSS namespace.
        # Import of arbitrary provider HTTPS URLs needs a separate allowlisted adapter.
        source_key = store.key_from_uri(payload.source_uri, payload.tenant_id)
        key = f"tenants/{payload.tenant_id}/artifacts/{payload.artifact_id}/output"
        digest = hashlib.sha256(json.dumps(payload.model_dump(), sort_keys=True).encode()).hexdigest()
        with sessions() as db:
            row = db.get(ArchiveIntent, payload.artifact_id)
            if row is None:
                row = ArchiveIntent(artifact_id=payload.artifact_id, tenant_id=payload.tenant_id,
                                    request_hash=digest, object_key=key)
                db.add(row)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    row = db.get(ArchiveIntent, payload.artifact_id)
            if row is None or row.tenant_id != payload.tenant_id or row.request_hash != digest:
                raise ObjectStoreError("archive_identity_conflict", "Artifact identity is bound to a different source", 409)
            observed = row.verified
        if observed is None:
            observed = store.archive(source_key=source_key, target_key=key,
                                     max_bytes=settings.max_upload_bytes, expected_hash=payload.expected_sha256)
            with sessions.begin() as db:
                row = db.get(ArchiveIntent, payload.artifact_id, with_for_update=True)
                if row.verified is None:
                    row.verified = observed
                else:
                    observed = row.verified
        uri = store.object_uri(key)
        expiry, nonce = datetime.now(timezone.utc) + timedelta(minutes=5), secrets.token_hex(16)
        signature = sign_attestation(settings.attestation_secret.get_secret_value(), asset_id=payload.artifact_id,
                                     object_uri=uri, size_bytes=observed["size_bytes"], sha256=observed["sha256"],
                                     expires_at=expiry, nonce=nonce)
        return {"artifact_id": payload.artifact_id, "object_uri": uri, **observed,
                "attestation": {"expires_at": expiry.isoformat(), "nonce": nonce, "signature": signature}}

    register_render_routes(app, settings=settings, sessions=sessions, store=store, authorize=authorize, ice_client=ice_client)
    register_analysis_routes(app, settings=settings, sessions=sessions, store=store, authorize=authorize,
                             ice_client=ice_client, shot_client=shot_client)
    register_package_routes(app,settings=settings,sessions=sessions,store=store,authorize=authorize)
    return app
