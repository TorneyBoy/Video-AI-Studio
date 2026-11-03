"""Personal delivery controls and a separate, least-data share redemption API."""
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .deps import RequestIdentity, authenticate_request, ensure_project, require_tenant_id
from .identity import PrivateValidationRoute
from .. import models
from ..config import get_settings
from ..database import get_db
from ..delivery_schemas import ShareCreate, SharedAccess, SharedFile, SharedRequest
from ..services.artifact_shares import (authorize_audience, create_share, lookup_share, owner_shares,
    public_summary, revoke_share, share_configuration, shared_access, shared_file, share_read, _owner)
from ..services.delivery_packages import compare_outputs, delivery_file, MAX_EXPORT_BYTES
from ..services.work_center import work_page
from ..services.editing_errors import EditingError
from ..services.identity import throttle
from ..services.storage_gateway import configured_storage_gateway, StorageGateway

router = APIRouter(tags=["delivery"],route_class=PrivateValidationRoute)


def _response(value):
    return JSONResponse(jsonable_encoder(value),headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"})


@router.get("/delivery/config")
def delivery_configuration(tenant_id: str = Depends(require_tenant_id)):
    return _response({"sharing":share_configuration(get_settings()),"max_file_bytes":MAX_EXPORT_BYTES})


@router.get("/projects/{project_id}/works")
def project_works(project_id: str,state: str="all",cursor: str | None=Query(None,max_length=512),limit: int=Query(20,ge=1,le=100),
                  tenant_id: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    ensure_project(db,project_id,tenant_id)
    return _response(work_page(db,tenant_id=tenant_id,project_id=project_id,state=state,cursor=cursor,limit=limit))


@router.get("/projects/{project_id}/works/compare")
def compare_work(project_id: str,left: str,right: str,skip: int=Query(0,ge=0),limit: int=Query(100,ge=1,le=100),
                 tenant_id: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    ensure_project(db,project_id,tenant_id)
    return _response(compare_outputs(db,tenant_id=tenant_id,project_id=project_id,left_id=left,right_id=right,skip=skip,limit=limit))


@router.get("/artifacts/{artifact_id}/delivery-file")
def export_delivery(artifact_id: str,kind: str=Query("manifest",pattern="^(manifest|srt|vtt)$"),
                    visibility: str=Query("internal",pattern="^(internal|public)$"),
                    expected_binding: str | None=Query(None,pattern=r"^[0-9a-f]{64}$"),
                    tenant_id: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    return _response(delivery_file(db,tenant_id=tenant_id,artifact_id=artifact_id,kind=kind,visibility=visibility,expected_binding=expected_binding))


@router.post("/artifacts/{artifact_id}/shares",status_code=201)
def new_share(artifact_id: str,payload: ShareCreate,key: str=Header(alias="Idempotency-Key",min_length=1,max_length=255),
              tenant_id: str=Depends(require_tenant_id),db: Session=Depends(get_db),identity: RequestIdentity=Depends(authenticate_request)):
    args={"tenant_id":tenant_id,"artifact_id":artifact_id,"actor_id":identity.principal_id,"key":key,"request":payload,"settings":get_settings()}
    try: row=create_share(db,**args);db.commit()
    except IntegrityError:
        db.rollback();row=create_share(db,**args);db.commit()
    response=_response(share_read(db,row,get_settings(),include_link=True));response.status_code=201;return response


@router.get("/artifacts/{artifact_id}/shares")
def list_shares(artifact_id: str,skip: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100),
                tenant_id: str=Depends(require_tenant_id),db: Session=Depends(get_db),identity: RequestIdentity=Depends(authenticate_request)):
    return _response([share_read(db,row,get_settings()) for row in owner_shares(db,tenant_id=tenant_id,artifact_id=artifact_id,actor_id=identity.principal_id,skip=skip,limit=limit)])


@router.get("/artifacts/{artifact_id}/share-requests/{key}")
def share_receipt(artifact_id: str,key: str,tenant_id: str=Depends(require_tenant_id),db: Session=Depends(get_db),
                  identity: RequestIdentity=Depends(authenticate_request)):
    _owner(db,tenant_id,artifact_id,identity.principal_id)
    row=db.scalar(select(models.ArtifactShare).where(models.ArtifactShare.artifact_id==artifact_id,models.ArtifactShare.idempotency_key==key))
    return _response({"found":row is not None,"share":share_read(db,row,get_settings(),include_link=True) if row else None})


@router.post("/artifacts/{artifact_id}/shares/{share_id}/link")
def read_share_link(artifact_id: str,share_id: str,tenant_id: str=Depends(require_tenant_id),db: Session=Depends(get_db),
                    identity: RequestIdentity=Depends(authenticate_request)):
    _owner(db,tenant_id,artifact_id,identity.principal_id)
    row=db.scalar(select(models.ArtifactShare).where(models.ArtifactShare.id==share_id,models.ArtifactShare.artifact_id==artifact_id))
    if row is None: raise EditingError("share_not_found","未找到分享记录",404)
    return _response(share_read(db,row,get_settings(),include_link=True))


@router.post("/artifacts/{artifact_id}/shares/{share_id}/revoke")
def cancel_share(artifact_id: str,share_id: str,tenant_id: str=Depends(require_tenant_id),db: Session=Depends(get_db),
                 identity: RequestIdentity=Depends(authenticate_request)):
    row=revoke_share(db,tenant_id=tenant_id,artifact_id=artifact_id,actor_id=identity.principal_id,share_id=share_id);db.commit()
    return _response(share_read(db,row,get_settings()))


def _public_grant(request: Request,db: Session,token: str):
    # The token is in a POST body, never a route/query value or log identifier.
    throttle(db,scope="shared-redemption",subject=request.client.host if request.client else "unknown",limit=600,seconds=60)
    row=lookup_share(db,token,get_settings());identity=None
    if row.audience=="workspace":
        identity=authenticate_request(request,db=db,x_tenant_id=request.headers.get("X-Tenant-ID"),
            x_auth_principal=request.headers.get("X-Auth-Principal"),x_auth_tenant_id=request.headers.get("X-Auth-Tenant-ID"),
            x_auth_issued_at=request.headers.get("X-Auth-Issued-At"),x_auth_signature=request.headers.get("X-Auth-Signature"))
    authorize_audience(row,identity.tenant_id if identity else None)
    return row


@router.post("/shared/inspect")
def inspect_shared(payload: SharedRequest,request: Request,db: Session=Depends(get_db)):
    row=_public_grant(request,db,payload.token)
    return _response(public_summary(db,row,get_settings()))


@router.post("/shared/access")
def access_shared(payload: SharedAccess,request: Request,db: Session=Depends(get_db),gateway: StorageGateway=Depends(configured_storage_gateway)):
    row=_public_grant(request,db,payload.token)
    return _response(shared_access(db,row,settings=get_settings(),gateway=gateway,purpose=payload.purpose))


@router.post("/shared/file")
def file_shared(payload: SharedFile,request: Request,db: Session=Depends(get_db)):
    row=_public_grant(request,db,payload.token)
    return _response(shared_file(db,row,settings=get_settings(),kind=payload.kind))
