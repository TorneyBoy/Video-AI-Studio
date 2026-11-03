"""One fixed-wave bulk review and delivery contract for web and phone."""
from fastapi import APIRouter,Depends,Header,Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session,defer
from .billing import reply
from .batches import valid_key
from .deps import RequestIdentity,authenticate_request,require_tenant_id
from .identity import PrivateValidationRoute
from .. import models
from ..batch_schemas import BatchReviewRequest,BatchDeliverySelection,BatchPackageCreate
from ..database import get_db
from ..services import batches,batch_runs,batch_delivery as service
from ..services.editing_errors import EditingError
from ..services.storage_gateway import StorageGateway,configured_storage_gateway

router=APIRouter(tags=['batch-delivery'],route_class=PrivateValidationRoute)


def context(db,tenant,batch_id,run_id,*,lock=False):
    batch=batches.batch_for(db,tenant,batch_id,lock=lock);return batch,batch_runs.run_for(db,batch,run_id)


@router.get('/batches/{batch_id}/runs/{run_id}/delivery')
def view(batch_id: str,run_id: str,state: str='all',tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch,run=context(db,tenant,batch_id,run_id);return reply(service.delivery_view(db,batch,run,state))


@router.post('/batches/{batch_id}/runs/{run_id}/review-prepare')
def prepare(batch_id: str,run_id: str,body: BatchDeliverySelection,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db),identity: RequestIdentity=Depends(authenticate_request)):
    batch,run=context(db,tenant,batch_id,run_id);return reply(service.prepare_reviews(db,batch,run,body.keys,identity.principal_id))


@router.post('/batches/{batch_id}/runs/{run_id}/reviews')
def review(batch_id: str,run_id: str,body: BatchReviewRequest,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),
           tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db),identity: RequestIdentity=Depends(authenticate_request)):
    valid_key(key);batch,run=context(db,tenant,batch_id,run_id,lock=True)
    try:result=service.apply_reviews(db,batch,run,identity.principal_id,key,body);db.commit()
    except IntegrityError as error:db.rollback();raise EditingError('batch_review_conflict','审核状态或原标识已变化，请核对原提交',409) from error
    return reply(result)


@router.get('/batches/{batch_id}/runs/{run_id}/review-requests/{key}')
def review_receipt(batch_id: str,run_id: str,key: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch,run=context(db,tenant,batch_id,run_id)
    row=db.scalar(select(models.BatchReviewAction).where(models.BatchReviewAction.tenant_id==tenant,models.BatchReviewAction.batch_id==batch.id,
        models.BatchReviewAction.run_id==run.id,models.BatchReviewAction.idempotency_key==key))
    return reply({'found':row is not None,'result':row.result_json if row else None})


@router.post('/batches/{batch_id}/runs/{run_id}/delivery-preview')
def preview(batch_id: str,run_id: str,body: BatchDeliverySelection,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch,run=context(db,tenant,batch_id,run_id);return reply(service.delivery_preview(db,batch,run,body)[0])


@router.post('/batches/{batch_id}/runs/{run_id}/packages')
def create(batch_id: str,run_id: str,body: BatchPackageCreate,key: str=Header(alias='Idempotency-Key',min_length=1,max_length=255),
           tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db),identity: RequestIdentity=Depends(authenticate_request)):
    valid_key(key);batch,run=context(db,tenant,batch_id,run_id,lock=True)
    try:result=service.create_package(db,batch,run,identity.principal_id,key,body);db.commit()
    except IntegrityError as error:db.rollback();raise EditingError('package_request_conflict','打包标识或文件集合已变化，请核对原提交',409) from error
    return reply(result)


@router.get('/batches/{batch_id}/runs/{run_id}/package-requests/{key}')
def package_receipt(batch_id: str,run_id: str,key: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch,run=context(db,tenant,batch_id,run_id);row=db.scalar(select(models.DeliveryPackageRequest).where(models.DeliveryPackageRequest.tenant_id==tenant,
        models.DeliveryPackageRequest.batch_id==batch.id,models.DeliveryPackageRequest.run_id==run.id,models.DeliveryPackageRequest.idempotency_key==key))
    return reply({'found':row is not None,'result':row.result_json if row else None})


@router.get('/batches/{batch_id}/runs/{run_id}/packages')
def listing(batch_id: str,run_id: str,skip: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100),tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch,run=context(db,tenant,batch_id,run_id)
    rows=list(db.scalars(select(models.DeliveryPackage).options(defer(models.DeliveryPackage.files_json)).where(models.DeliveryPackage.tenant_id==tenant,
        models.DeliveryPackage.batch_id==batch.id,models.DeliveryPackage.run_id==run.id).order_by(models.DeliveryPackage.created_at.desc(),models.DeliveryPackage.id.desc()).offset(skip).limit(limit+1)))
    return reply({'items':[service.package_read(db,row,validate=False) for row in rows[:limit]],'has_more':len(rows)>limit,'next_skip':skip+min(limit,len(rows))})


@router.get('/batches/{batch_id}/runs/{run_id}/packages/{package_id}')
def read(batch_id: str,run_id: str,package_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db)):
    batch,run=context(db,tenant,batch_id,run_id);return reply(service.package_read(db,service.package_for(db,batch,run,package_id)))


@router.post('/batches/{batch_id}/runs/{run_id}/packages/{package_id}/access')
def access(batch_id: str,run_id: str,package_id: str,tenant: str=Depends(require_tenant_id),db: Session=Depends(get_db),gateway: StorageGateway=Depends(configured_storage_gateway)):
    batch,run=context(db,tenant,batch_id,run_id);return reply(service.package_access(db,service.package_for(db,batch,run,package_id),gateway))
