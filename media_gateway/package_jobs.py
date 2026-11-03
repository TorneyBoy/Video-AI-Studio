"""Bounded real ZIP construction over managed OSS objects, no model execution."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import secrets
from typing import Literal
from uuid import uuid4
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,model_validator
from sqlalchemy import Boolean,DateTime,Integer,JSON,String,func,select,text,update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped,mapped_column
from app.services.storage_attestation import sign_attestation
from .database import Base
from .oss import ObjectStoreError

ID=r'^[A-Za-z0-9_-]{1,64}$'
HASH=r'^[0-9a-f]{64}$'
NAME=r'^[0-9]{3}-[A-Za-z0-9_-]{1,64}\.(mp4|srt|vtt)$'


class PackageQuota(Base):
    __tablename__='gateway_package_quota'
    id: Mapped[str]=mapped_column(String(20),primary_key=True)
    used: Mapped[int]=mapped_column(Integer,default=0,nullable=False)


class PackageOperation(Base):
    __tablename__='gateway_package_operations'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    tenant_id: Mapped[str]=mapped_column(String(64),nullable=False)
    manifest_hash: Mapped[str]=mapped_column(String(64),nullable=False)
    request_hash: Mapped[str | None]=mapped_column(String(64))
    specification: Mapped[dict | None]=mapped_column(JSON)
    status: Mapped[str]=mapped_column(String(20),default='queued',nullable=False)
    cancel_requested: Mapped[bool]=mapped_column(Boolean,default=False,nullable=False)
    attempts: Mapped[int]=mapped_column(Integer,default=0,nullable=False)
    lease_token: Mapped[str | None]=mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None]=mapped_column(JSON)
    error_code: Mapped[str | None]=mapped_column(String(100))
    retryable: Mapped[bool]=mapped_column(Boolean,default=False,nullable=False)


class PackageFile(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    kind: Literal['video','text']
    filename: str=Field(pattern=NAME)
    sha256: str=Field(pattern=HASH)
    size_bytes: int=Field(ge=0,le=1024**3)
    artifact_id: str | None=Field(default=None,pattern=ID)
    object_uri: str | None=Field(default=None,max_length=2048)
    content: str | None=Field(default=None,max_length=524288)

    @model_validator(mode='after')
    def typed(self):
        if self.kind=='video' and (not self.artifact_id or not self.object_uri or self.content is not None or self.size_bytes<1 or not self.filename.endswith('.mp4')):raise ValueError('invalid video entry')
        if self.kind=='text' and (self.content is None or self.object_uri is not None or self.artifact_id is not None or self.filename.endswith('.mp4')):raise ValueError('invalid text entry')
        return self


class PackageRead(BaseModel):
    model_config=ConfigDict(extra='forbid')
    package_id: str=Field(pattern=ID)
    tenant_id: str=Field(pattern=ID)
    manifest_hash: str=Field(pattern=HASH)


class PackageBuild(PackageRead):
    manifest: str=Field(min_length=1,max_length=524288)
    files: list[PackageFile]=Field(min_length=1,max_length=300)


class PackageBodyLimit:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('path')!='/v1/packages/build':return await self.app(scope,receive,send)
        total=0
        async def bounded():
            nonlocal total
            message=await receive();total+=len(message.get('body',b''))
            if total>2*1024*1024:raise HTTPException(413,'Package metadata exceeds 2MiB')
            return message
        return await self.app(scope,bounded,send)


def utc(value):return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _check_files(payload,settings,store):
    if len({file.filename for file in payload.files})!=len(payload.files):raise ObjectStoreError('package_filename_duplicate','Package entries must have unique names',422)
    videos=[file for file in payload.files if file.kind=='video'];size=sum(file.size_bytes for file in videos)
    if not 1<=len(videos)<=100 or size>min(settings.package_max_bytes,settings.max_upload_bytes):raise ObjectStoreError('package_size_limit','Selected videos exceed the package limit',413)
    count=len(payload.manifest.encode('utf-8'))
    for file in payload.files:
        if file.kind=='video':
            key=store.key_from_uri(file.object_uri,payload.tenant_id)
            if key!=f'tenants/{payload.tenant_id}/artifacts/{file.artifact_id}/output':raise ObjectStoreError('package_source_unmanaged','Package source is not an archived artifact',422)
        else:
            raw=file.content.encode('utf-8');count+=len(raw)
            if len(raw)!=file.size_bytes or hashlib.sha256(raw).hexdigest()!=file.sha256:raise ObjectStoreError('package_text_invalid','Package text does not match its checksum',422)
    if count>524288 or hashlib.sha256(payload.manifest.encode('utf-8')).hexdigest()!=payload.manifest_hash:raise ObjectStoreError('package_manifest_invalid','Package manifest or text exceeds its contract',422)
    try:
        manifest=json.loads(payload.manifest);included=[row for row in manifest['items'] if row['included']]
        if manifest['schema_version']!='batch-delivery/1' or len(included)!=len(videos):raise ValueError('manifest count')
        expected={(row['filename'],row['artifact_id'],row['output_sha256'],row['size_bytes']) for row in included}
        observed={(file.filename,file.artifact_id,file.sha256,file.size_bytes) for file in videos}
        if expected!=observed:raise ValueError('manifest file set')
        text_expected={(entry['filename'],entry['sha256'],entry['size_bytes']) for row in included for entry in row.get('sidecars',[])}
        if text_expected!={(file.filename,file.sha256,file.size_bytes) for file in payload.files if file.kind=='text'}:raise ValueError('manifest text set')
    except (ValueError,KeyError,TypeError) as error:raise ObjectStoreError('package_manifest_invalid','Package file list does not match its manifest',422) from error


def register_package_routes(app,*,settings,sessions,store,authorize):
    app.add_middleware(PackageBodyLimit)
    def owned(db,payload,lock=False):
        query=select(PackageOperation).where(PackageOperation.id==payload.package_id)
        if lock:query=query.with_for_update().execution_options(populate_existing=True)
        row=db.scalar(query)
        if row is None:raise ObjectStoreError('package_not_found','Package request not found',404)
        if row.tenant_id!=payload.tenant_id or row.manifest_hash!=payload.manifest_hash:raise ObjectStoreError('package_identity_conflict','Package request identity does not match',409)
        return row
    def reply(row):
        now=datetime.now(timezone.utc);value={'package_id':row.id,'manifest_hash':row.manifest_hash,'status':'failed' if row.status=='cancelled' else row.status,
            'error_code':row.error_code,'retryable':row.retryable and row.attempts<3,'resumable':row.status=='running' and bool(row.lease_expires_at and utc(row.lease_expires_at)<=now)}
        if row.status=='succeeded':
            result=row.result or {};expiry=now+timedelta(minutes=5);nonce=secrets.token_hex(16)
            signature=sign_attestation(settings.attestation_secret.get_secret_value(),asset_id=row.id,object_uri=result['object_uri'],size_bytes=result['size_bytes'],sha256=result['sha256'],expires_at=expiry,nonce=nonce)
            value.update(result,attestation={'expires_at':expiry.isoformat(),'nonce':nonce,'signature':signature})
        return value
    @app.post('/v1/packages/status',dependencies=[Depends(authorize)])
    def status(payload: PackageRead):
        with sessions() as db:return reply(owned(db,payload))
    @app.post('/v1/packages/cancel',dependencies=[Depends(authorize)])
    def cancel(payload: PackageRead):
        with sessions() as db:
            try:row=owned(db,payload,True)
            except ObjectStoreError as error:
                if error.code!='package_not_found':raise
                row=PackageOperation(id=payload.package_id,tenant_id=payload.tenant_id,manifest_hash=payload.manifest_hash,status='cancelled',cancel_requested=True,error_code='package_cancelled');db.add(row)
                try:db.flush()
                except IntegrityError:db.rollback();row=owned(db,payload,True)
            row.cancel_requested=True
            if row.status!='running' or not row.lease_expires_at or utc(row.lease_expires_at)<=datetime.now(timezone.utc):
                row.status='cancelled';row.lease_token=None;row.error_code='package_cancelled';row.retryable=False
            db.commit();return reply(row)
    @app.post('/v1/packages/build',dependencies=[Depends(authorize)])
    def build(payload: PackageBuild):
        _check_files(payload,settings,store);body=payload.model_dump(mode='json');hash=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with sessions() as db:
            row=db.get(PackageOperation,payload.package_id)
            if row is None:
                row=PackageOperation(id=payload.package_id,tenant_id=payload.tenant_id,manifest_hash=payload.manifest_hash,request_hash=hash,specification=body)
                db.add(row)
                try:
                    db.flush();db.execute(text("INSERT INTO gateway_package_quota (id, used) VALUES ('packages',0) ON CONFLICT (id) DO NOTHING"))
                    if not settings.package_enabled:row.status='failed';row.error_code='package_disabled'
                    elif db.execute(update(PackageQuota).where(PackageQuota.id=='packages',PackageQuota.used<settings.package_max_operations).values(used=PackageQuota.used+1)).rowcount!=1:
                        row.status='failed';row.error_code='package_quota_exceeded'
                    db.commit()
                except IntegrityError:db.rollback()
            row=owned(db,payload,True)
            if row.cancel_requested or row.status=='cancelled':return reply(row)
            if row.request_hash!=hash:raise ObjectStoreError('package_identity_conflict','Original package has a different file set',409)
            if row.status=='succeeded' or row.status=='failed' and (not row.retryable or row.attempts>=3):return reply(row)
            if not settings.package_enabled:return {'package_id':row.id,'manifest_hash':row.manifest_hash,'status':'queued'}
            # The quota row serializes concurrent claims across distinct IDs.
            db.execute(text("UPDATE gateway_package_quota SET used=used WHERE id='packages'"))
            now=datetime.now(timezone.utc)
            running=db.scalar(select(func.count()).select_from(PackageOperation).where(PackageOperation.status=='running',PackageOperation.lease_expires_at>now))
            if running:db.commit();return reply(row)
            token=uuid4().hex;row.status='running';row.lease_token=token;row.lease_expires_at=now+timedelta(minutes=10);row.attempts+=1;row.error_code=None;row.retryable=False;db.commit()
        def fence():
            with sessions() as db:
                current=db.get(PackageOperation,payload.package_id)
                if not current or current.lease_token!=token or current.cancel_requested or not current.lease_expires_at or utc(current.lease_expires_at)<=datetime.now(timezone.utc):
                    raise ObjectStoreError('package_cancelled','Package execution no longer owns the lease',409)
        result=None;code=None;retryable=False
        try:
            result=store.package(tenant_id=payload.tenant_id,package_id=payload.package_id,manifest=payload.manifest,
                files=[file.model_dump(mode='json') for file in payload.files],max_bytes=min(settings.package_max_bytes,settings.max_upload_bytes),temp_root=settings.package_temp_root,fence=fence)
        except Exception as error:
            code=error.code if isinstance(error,ObjectStoreError) else 'package_storage_unavailable'
            retryable=code not in {'package_cancelled','package_source_hash_mismatch','package_source_size_mismatch','package_size_limit','package_manifest_invalid','bucket_versioning_incompatible'}
        with sessions() as db:
            row=owned(db,payload,True)
            if row.lease_token!=token:return reply(row)
            row.lease_token=None;row.lease_expires_at=None
            if row.cancel_requested:row.status='cancelled';row.error_code='package_cancelled';row.retryable=False
            elif result is not None:row.status='succeeded';row.result=result
            else:row.status='failed';row.error_code=code;row.retryable=retryable
            db.commit();return reply(row)
