"""Real deterministic ZIP bytes over offline OSS substitutes; not cloud acceptance."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import io
import json
from types import SimpleNamespace
from zipfile import ZipFile
from threading import Event
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from media_gateway.main import create_app,GatewaySettings
from media_gateway.oss import OSSStore
from media_gateway.package_jobs import PackageOperation,PackageQuota
from app.services.storage_attestation import verify_attestation

KEY='package-gateway-fixture-only-key-32-characters'
SECRET='package-gateway-fixture-proof-32-characters'
DATA=b'Explicit offline object fixture; this is not an acceptance video.'*200
SHA=hashlib.sha256(DATA).hexdigest()


class Objects:
    def __init__(self):self.objects={'tenants/tenant-a/artifacts/artifact-a/output':DATA};self.puts=[];self.gets=[];self.truncate=False;self.block=None
    def get_bucket_versioning(self,request):return SimpleNamespace(version_status=None)
    def head_object(self,request):
        raw=self.objects[request.key];return SimpleNamespace(content_length=len(raw),etag='"'+hashlib.md5(raw,usedforsecurity=False).hexdigest()+'"')
    def get_object(self,request):
        self.gets.append(request.key)
        assert request.if_match==self.head_object(request).etag
        if self.block:self.block()
        raw=self.objects[request.key];return SimpleNamespace(body=io.BytesIO(raw[:-1] if self.truncate else raw),etag=self.head_object(request).etag,request_id='offline-object-request')
    def put_object(self,request):
        raw=request.body.read() if hasattr(request.body,'read') else request.body
        assert request.forbid_overwrite and request.acl=='private' and request.content_type=='application/zip' and request.content_length==len(raw)
        self.puts.append(request.key)
        if request.key in self.objects:assert self.objects[request.key]==raw
        else:self.objects[request.key]=raw
        return SimpleNamespace(status_code=200)
    def presign(self,request,expires):return SimpleNamespace(url='https://fixture-bucket.oss-cn-beijing.aliyuncs.com/'+request.key+'?signature=fixture',expiration=datetime.now(timezone.utc)+expires)


@pytest.fixture
def gateway(tmp_path):
    import alibabacloud_oss_v2 as oss
    objects=Objects();store=OSSStore(bucket='fixture-bucket',region='cn-beijing',client=objects,credentials_provider=oss.credentials.StaticCredentialsProvider('fixture-id','fixture-secret'))
    settings=GatewaySettings(api_key=KEY,attestation_secret=SECRET,oss_bucket='fixture-bucket',oss_region='cn-beijing',
        database_url='sqlite:///'+(tmp_path/'gateway.db').as_posix(),package_enabled=True,package_max_operations=2,package_temp_root=str(tmp_path/'package-work'))
    app=create_app(settings,store=store)
    with TestClient(app) as client:yield SimpleNamespace(client=client,objects=objects,settings=settings,app=app,root=tmp_path)
    app.state.engine.dispose()


def body(id='package-a',*,sha=SHA):
    text='1\n00:00:00,000 --> 00:00:01,000\nfixture\n';text_hash=hashlib.sha256(text.encode()).hexdigest()
    manifest={'schema_version':'batch-delivery/1','items':[{'included':True,'filename':'001-artifact-a.mp4','artifact_id':'artifact-a','output_sha256':sha,'size_bytes':len(DATA),
        'sidecars':[{'filename':'001-artifact-a.srt','sha256':text_hash,'size_bytes':len(text.encode())}]}]}
    content=json.dumps(manifest,sort_keys=True)
    return {'package_id':id,'tenant_id':'tenant-a','manifest_hash':hashlib.sha256(content.encode()).hexdigest(),'manifest':content,'files':[
        {'kind':'video','filename':'001-artifact-a.mp4','artifact_id':'artifact-a','object_uri':'oss://fixture-bucket/tenants/tenant-a/artifacts/artifact-a/output','sha256':sha,'size_bytes':len(DATA)},
        {'kind':'text','filename':'001-artifact-a.srt','content':text,'sha256':text_hash,'size_bytes':len(text.encode())}]}


def post(gateway,path,body):return gateway.client.post('/v1/packages/'+path,headers={'Authorization':'Bearer '+KEY},json=body)


def identity(body):return {key:body[key] for key in ('package_id','tenant_id','manifest_hash')}


def test_zip_contains_exact_manifest_original_bytes_and_subtitles_with_verified_proof(gateway):
    payload=body();response=post(gateway,'build',payload);assert response.status_code==200,response.text;result=response.json();assert result['status']=='succeeded'
    key='tenants/tenant-a/packages/package-a/bundle.zip';raw=gateway.objects.objects[key]
    assert hashlib.sha256(raw).hexdigest()==result['sha256'] and len(raw)==result['size_bytes']
    with ZipFile(io.BytesIO(raw)) as zip:
        assert zip.namelist()==['manifest.json','001-artifact-a.mp4','001-artifact-a.srt'] and zip.testzip() is None
        assert zip.read('001-artifact-a.mp4')==DATA and zip.read('manifest.json').decode()==payload['manifest']
    proof=result['attestation'];verify_attestation(SECRET,proof['signature'],asset_id=payload['package_id'],object_uri=result['object_uri'],size_bytes=result['size_bytes'],sha256=result['sha256'],
        expires_at=datetime.fromisoformat(proof['expires_at']),nonce=proof['nonce'])
    assert post(gateway,'build',payload).json()['sha256']==result['sha256'] and len(gateway.objects.puts)==1
    assert not list((gateway.root/'package-work').iterdir())


def test_distinct_package_ids_build_identical_deterministic_zip_bytes(gateway):
    first=post(gateway,'build',body()).json();second=post(gateway,'build',body('package-b')).json()
    assert first['sha256']==second['sha256'] and first['size_bytes']==second['size_bytes']


@pytest.mark.parametrize('change',['hash','truncate','tenant','filename','manifest'])
def test_changed_or_unmanaged_input_never_publishes_a_package(gateway,change):
    payload=body(sha='0'*64) if change=='hash' else body()
    if change=='truncate':gateway.objects.truncate=True
    elif change=='tenant':payload['files'][0]['object_uri']='oss://fixture-bucket/tenants/other/artifacts/artifact-a/output'
    elif change=='filename':payload['files'][0]['filename']='../bad.mp4'
    elif change=='manifest':payload['manifest']='{}'
    response=post(gateway,'build',payload)
    if change in {'hash','truncate'}:assert response.json()['status']=='failed' and not response.json()['retryable']
    else:assert response.status_code in {403,422}
    assert not gateway.objects.puts


def test_cancel_tombstone_blocks_late_original_build_without_io_or_quota(gateway):
    payload=body();assert post(gateway,'cancel',identity(payload)).json()['status']=='failed'
    response=post(gateway,'build',payload).json();assert response['status']=='failed' and response['error_code']=='package_cancelled'
    assert not gateway.objects.puts and not gateway.objects.gets
    with gateway.app.state.sessions() as db:assert db.get(PackageQuota,'packages') is None


def test_same_id_changed_selection_and_cross_tenant_status_are_rejected(gateway):
    payload=body();assert post(gateway,'build',payload).json()['status']=='succeeded'
    changed=body(sha='0'*64);assert post(gateway,'build',changed).status_code==409
    other={**identity(payload),'tenant_id':'tenant-other'};assert post(gateway,'status',other).status_code==409
    assert len(gateway.objects.puts)==1


def test_concurrent_replay_and_cancellation_do_not_start_a_second_builder(gateway):
    arrived=Event();release=Event();payload=body()
    def block():arrived.set();assert release.wait(5)
    gateway.objects.block=block
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(post,gateway,'build',payload);assert arrived.wait(5)
        assert post(gateway,'build',payload).json()['status']=='running'
        assert post(gateway,'cancel',identity(payload)).json()['status']=='running'
        release.set();result=future.result(timeout=10).json()
    assert result['status']=='failed' and result['error_code']=='package_cancelled' and not gateway.objects.puts


def test_operation_quota_is_persistent_and_does_not_reset_on_replay(gateway):
    gateway.settings.package_max_operations=1
    assert post(gateway,'build',body()).json()['status']=='succeeded'
    second=post(gateway,'build',body('package-b')).json();assert second['status']=='failed' and second['error_code']=='package_quota_exceeded'
    assert post(gateway,'build',body('package-b')).json()==second
    with gateway.app.state.sessions() as db:
        assert db.scalar(select(PackageQuota.used))==1
        assert db.get(PackageOperation,'package-b').attempts==0


def test_authentication_and_package_metadata_body_limit(gateway):
    assert gateway.client.post('/v1/packages/build',json=body()).status_code==401
    raw=b'{"padding":"'+b'x'*(2*1024*1024)+b'"}'
    response=gateway.client.post('/v1/packages/build',headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json'},content=raw)
    assert response.status_code==413 and not gateway.objects.gets and not gateway.objects.puts
