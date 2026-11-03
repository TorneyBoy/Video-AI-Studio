"""Allow-listed JSON events, server request IDs and commit-aware Job traces."""
from __future__ import annotations
import asyncio
from contextvars import ContextVar
from datetime import datetime,timezone
import hashlib
import json
import logging
import re
import sys
from threading import Lock
from time import perf_counter
from uuid import UUID,uuid4

from sqlalchemy import event,inspect,select
from sqlalchemy.orm import Session

request_id=ContextVar('observation_request_id',default=None)
logger=logging.getLogger('video_ai.observation')
SAFE_CODES=frozenset('budget_exceeded budget_period_exceeded budget_task_exceeded provider_timeout provider_unavailable provider_request_failed provider_response_invalid submission_outcome_unknown consent_inactive rights_expired source_changed render_failed analysis_failed plan_failed workflow_lease_expired storage_gateway_unreachable storage_gateway_request_failed artifact_quality_pending artifact_retention_expired package_approval_changed package_expired package_poll_timeout package_cancelled package_quota_exceeded package_disabled package_source_hash_mismatch package_source_size_mismatch package_worker_unavailable'.split())
EVENTS=frozenset('http_request job_state worker_cycle worker_heartbeat_error operational_alert operational_alert_recovered operator_check'.split())
IDS=frozenset('job_id tenant_id project_id workflow_id instance_id'.split())
INTS=frozenset('status attempt generation duration_ms count'.split())
TOKENS=frozenset('method route family state previous_state failure_code failure_tag code severity outcome worker_tag'.split())


def fingerprint(value):return hashlib.sha256(str(value).encode()).hexdigest()[:24]


def opaque_id(value):
    if value is None:return None
    try:return str(UUID(str(value)))
    except ValueError:return 'opaque:'+fingerprint(value)


def failure_code(value):return value if value in SAFE_CODES else 'other' if value else None


def job_family(kind):
    from .dispatch import family_for
    from types import SimpleNamespace
    if kind.startswith('batch.'):return 'batch'
    if kind in {'delivery.package','asset.analyze','auto_edit.plan','video.generate'} or kind.startswith(('render.','voice.','avatar.','enhancement.')):return family_for(SimpleNamespace(type=kind))
    return 'other'


def emit(name,**fields):
    if name not in EVENTS:return
    result={'schema':'observation/1','at':datetime.now(timezone.utc).isoformat(),'event':name}
    for key,value in fields.items():
        if value is None:continue
        if key in IDS:result[key]=opaque_id(value)
        elif key in {'request_id','origin_request_id'}:
            if isinstance(value,str) and re.fullmatch('[0-9a-f]{32}',value):result[key]=value
        elif key in INTS and isinstance(value,int) and not isinstance(value,bool):result[key]=max(0,min(value,10**12))
        elif key in TOKENS and isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_./{}:-]{1,180}',value):result[key]=value
    try:logger.log(logging.ERROR if result.get('severity')=='critical' else logging.INFO,json.dumps(result,ensure_ascii=True,separators=(',',':')))
    except Exception:pass  # Optional logging handlers cannot fail a committed transaction.


class _DynamicJSONHandler(logging.Handler):
    def emit(self,record):
        try:sys.stdout.write(record.getMessage()+'\n');sys.stdout.flush()
        except (OSError,ValueError):pass  # Logging cannot change a committed business result.


def configure_logging():
    if not any(isinstance(handler,_DynamicJSONHandler) for handler in logger.handlers):logger.addHandler(_DynamicJSONHandler())
    logger.setLevel(logging.INFO);logger.propagate=False


class HTTPMeasurements:
    buckets=(.05,.1,.25,.5,1.,2.,5.,10.,30.,60.,120.)
    def __init__(self):self.lock=Lock();self.rows={}
    def observe(self,route,method,status,seconds):
        with self.lock:
            key=(route,method,status)
            if len(self.rows)>=2048 and key not in self.rows:key=('other','OTHER',status)
            value=self.rows.setdefault(key,{'count':0,'sum':0.,'buckets':[0]*len(self.buckets)})
            value['count']+=1;value['sum']+=seconds
            for index,bound in enumerate(self.buckets):value['buckets'][index]+=int(seconds<=bound)
    def prometheus(self):
        with self.lock:rows=[(key,{'count':value['count'],'sum':value['sum'],'buckets':list(value['buckets'])}) for key,value in self.rows.items()]
        lines=['# HELP video_ai_http_requests_total HTTP responses in this serving process; registered routes only.',
            '# TYPE video_ai_http_requests_total counter','# HELP video_ai_http_request_seconds HTTP response duration in seconds in this serving process.',
            '# TYPE video_ai_http_request_seconds histogram']
        for (route,method,status),value in sorted(rows):
            labels=f'route="{route}",method="{method}",status="{status}"'
            lines.append(f'video_ai_http_requests_total{{{labels}}} {value["count"]}')
            for bound,count in zip(self.buckets,value['buckets']):lines.append(f'video_ai_http_request_seconds_bucket{{{labels},le="{bound:g}"}} {count}')
            lines.extend((f'video_ai_http_request_seconds_bucket{{{labels},le="+Inf"}} {value["count"]}',f'video_ai_http_request_seconds_sum{{{labels}}} {value["sum"]:.6f}',f'video_ai_http_request_seconds_count{{{labels}}} {value["count"]}'))
        return '\n'.join(lines)+'\n'


http_measurements=HTTPMeasurements()


class RequestObservation:
    """Pure ASGI: no bodies, credentials, query strings, concrete paths or IPs."""
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        identifier=uuid4().hex;token=request_id.set(identifier);start=perf_counter();status=500;started=False;code=None
        scope.setdefault('state',{})['observation_request_id']=identifier
        async def output(message):
            nonlocal status,started
            if message['type']=='http.response.start':
                started=True;status=message['status'];headers=[(key,value) for key,value in message.get('headers',[]) if key.lower()!=b'x-request-id']
                message={**message,'headers':[*headers,(b'x-request-id',identifier.encode())]}
            await send(message)
        try:await self.app(scope,receive,output)
        except asyncio.CancelledError:status=499;code='request_cancelled';raise
        except Exception:
            code='unhandled_error';status=500
            if started:raise RuntimeError('response_stream_failed') from None
            body=json.dumps({'detail':{'code':'internal_error','request_id':identifier,'message':'请求暂未完成，请先核对原提交；支持编号：'+identifier}},ensure_ascii=False).encode('utf-8')
            await output({'type':'http.response.start','status':500,'headers':[(b'content-type',b'application/json'),(b'cache-control',b'no-store')]})
            await output({'type':'http.response.body','body':body})
        finally:
            route=getattr(scope.get('route'),'path','unmatched')
            if not isinstance(route,str) or not re.fullmatch(r'[A-Za-z0-9_./{}:-]{1,180}',route):route='unmatched'
            method=scope.get('method','OTHER');method=method if method in {'GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'} else 'OTHER'
            seconds=max(0.,perf_counter()-start);http_measurements.observe(route,method,status,seconds)
            emit('http_request',request_id=identifier,method=method,route=route,status=status,duration_ms=int(seconds*1000),code=code)
            request_id.reset(token)


_installed=False


def install_job_observation():
    global _installed
    if _installed:return
    _installed=True
    event.listen(Session,'after_flush',_flushed)
    event.listen(Session,'after_commit',_committed)
    event.listen(Session,'after_rollback',_rolled_back)
    event.listen(Session,'after_soft_rollback',lambda db,transaction:_discard(db,transaction))


def _flushed(db,context):
    from .. import models
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sq_insert
    jobs=[job for job in set(db.new).union(db.dirty) if isinstance(job,models.Job) and job not in db.deleted
        and (job in db.new or any(inspect(job).attrs[key].history.has_changes() for key in ('status','attempt_count','execution_generation','failure_code')))]
    if not jobs:return
    connection=db.connection();current=request_id.get();insert=pg_insert if connection.dialect.name=='postgresql' else sq_insert
    origins=dict(connection.execute(select(models.JobTrace.job_id,models.JobTrace.request_id).where(models.JobTrace.job_id.in_([job.id for job in jobs]))).all())
    parents={str((job.payload or {}).get('parent_job_id')) for job in jobs if job in db.new and (job.payload or {}).get('parent_job_id')}
    ancestors={row.job_id:row for row in connection.execute(select(models.JobTrace.job_id,models.JobTrace.request_id,models.Job.tenant_id,models.Job.project_id).join(models.Job,models.Job.id==models.JobTrace.job_id).where(models.JobTrace.job_id.in_(parents)))} if parents else {}
    transaction=db.get_nested_transaction() or db.get_transaction();pending=db.info.setdefault('observation_pending',{}).setdefault(transaction,{})
    for job in jobs:
        origin=origins.get(job.id)
        if job in db.new and not origin:
            parent=ancestors.get(str((job.payload or {}).get('parent_job_id')))
            origin=current or (parent.request_id if parent and (parent.tenant_id,parent.project_id)==(job.tenant_id,job.project_id) else None)
            if origin:connection.execute(insert(models.JobTrace).values(job_id=job.id,request_id=origin).on_conflict_do_nothing(index_elements=['job_id']))
        history=inspect(job).attrs.status.history
        previous=pending.get(job.id,{}).get('previous_state') if job.id in pending else history.deleted[0] if history.deleted and history.deleted[0] in {'queued','running','succeeded','failed','cancelled'} else None
        pending[job.id]={'job_id':job.id,'tenant_id':job.tenant_id,'project_id':job.project_id,'request_id':current,'origin_request_id':origin,
            'state':job.status if job.status in {'queued','running','succeeded','failed','cancelled'} else 'other',
            'previous_state':previous,
            'family':job_family(job.type),'attempt':job.attempt_count or 0,'generation':job.execution_generation or 0,
            'failure_code':failure_code(job.failure_code),'failure_tag':fingerprint(job.failure_code) if job.failure_code else None}


def _committed(db):
    transaction=db.get_nested_transaction() or db.get_transaction();pending=db.info.get('observation_pending',{});rows=pending.pop(transaction,{})
    if transaction is not None and transaction.nested:
        parent=pending.setdefault(transaction.parent,{})
        for id,row in rows.items():
            if id in parent:row['previous_state']=parent[id]['previous_state']
            parent[id]=row
    else:
        for row in rows.values():emit('job_state',**row)
        db.info.pop('observation_pending',None)


def _discard(db,transaction):
    pending=db.info.get('observation_pending',{});pending.pop(transaction,None)
    if not pending:db.info.pop('observation_pending',None)


def _rolled_back(db):_discard(db,db.get_nested_transaction() or db.get_transaction())
