"""Capture synthetic upgrade fixtures from the actual cached M1 control v8.

The immutable image runs read-only against a fresh local disposable PG16 DB.
No production configuration, cloud request, production DB or media IO.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from psycopg import sql  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from app.services.pg_recovery import DatabaseEndpoint,PgClient,instance_identity  # noqa: E402
from app.services.recovery_bundle import private_scratch  # noqa: E402

IMAGE='sha256:227302226b8fcd518ca0dde35b3857da483e2d723c4b6e7659d33fd0dec8c9d5'
CAPTURE=r'''
from datetime import datetime,timezone,timedelta
import json,os
from sqlalchemy import create_engine,create_mock_engine,select,text
from sqlalchemy.orm import Session
from app import models
from app.database import Base
from app.migrations import migrate,_ensure_migration_table

engine=create_engine(os.environ['DATABASE_URL']);migrate(engine)
stamp=datetime(2026,10,18,tzinfo=timezone.utc)
tenant_id='10000000-0000-0000-0000-000000000001'
project_id='10000000-0000-0000-0000-000000000002'
unknown_id='10000000-0000-0000-0000-000000000003'
done_id='10000000-0000-0000-0000-000000000004'
with Session(engine) as db:
    tenant=models.Tenant(id=tenant_id,slug='m1-upgrade-fixture',name='Explicit offline M1 fixture',created_at=stamp,updated_at=stamp)
    db.add(tenant);db.flush()
    db.add(models.Project(id=project_id,tenant_id=tenant_id,name='M1 preservation fixture',metadata_json={'fixture_only':True},created_at=stamp,updated_at=stamp));db.flush()
    db.add(models.Asset(tenant_id=tenant_id,project_id=project_id,name='Fixture source metadata',kind='video',uri='oss://fixture-bucket/tenants/'+tenant_id+'/source',metadata_json={'fixture_only':True},created_at=stamp,updated_at=stamp))
    job=models.Job(id=unknown_id,tenant_id=tenant_id,project_id=project_id,type='video.generate',status='running',execution_generation=3,payload={'fixture_only':True},created_at=stamp,updated_at=stamp)
    db.add(job);db.add(models.Job(id=done_id,tenant_id=tenant_id,project_id=project_id,type='timeline.render',status='succeeded',payload={'fixture_only':True},created_at=stamp,updated_at=stamp));db.flush()
    db.add(models.WorkflowRun(tenant_id=tenant_id,job_id=unknown_id,workflow_type='video.generate',status='running',attempt_count=2,worker_id='offline-m1-worker',lease_expires_at=stamp+timedelta(hours=1),input_json={'fixture_only':True},created_at=stamp,updated_at=stamp))
    db.add(models.ProviderOperation(tenant_id=tenant_id,job_id=unknown_id,request_id='offline-original-request',external_job_id='offline-original-remote',provider_key='fixture-only',state='unknown',execution_generation=3,created_at=stamp,updated_at=stamp))
    db.add(models.UsageLedger(tenant_id=tenant_id,job_id=unknown_id,period_key='2026-10',status='pending_reconciliation',source='explicit_fixture_not_billed',currency='CNY',amount_micros=350000,created_at=stamp,updated_at=stamp))
    artifact=models.Artifact(tenant_id=tenant_id,project_id=project_id,job_id=done_id,kind='video.rendered',uri='oss://fixture-bucket/tenants/'+tenant_id+'/rendered',metadata_json={'fixture_only':True},created_at=stamp,updated_at=stamp)
    db.add(artifact);db.flush()
    db.add(models.ArtifactStorage(artifact_id=artifact.id,tenant_id=tenant_id,object_uri='oss://fixture-bucket/tenants/'+tenant_id+'/archived',sha256='f'*64,size_bytes=1234,expires_at=stamp+timedelta(days=2),evidence_json={'fixture_only':True},created_at=stamp,updated_at=stamp))
    db.add(models.TenantBudget(tenant_id=tenant_id,currency='CNY',monthly_limit_micros=1000000,enabled=True,created_at=stamp,updated_at=stamp))
    db.commit()
ddl=[]
def record(statement,*args,**kwargs):ddl.append(str(statement.compile(dialect=mock.dialect)))
mock=create_mock_engine('postgresql+psycopg://',record)
Base.metadata.create_all(mock,checkfirst=False)
class RecordingConnection:
    def execute(self,statement,*args,**kwargs):ddl.append(str(statement))
_ensure_migration_table(RecordingConnection())
rows={}
with engine.connect() as db:
    for table in Base.metadata.sorted_tables:
        data=[dict(row) for row in db.execute(select(table)).mappings()]
        if data:rows[table.name]=data
    versions=[dict(row) for row in db.execute(text('SELECT * FROM schema_migrations ORDER BY version')).mappings()]
def encode(value):
    if isinstance(value,datetime):return value.isoformat()
    raise TypeError()
print(json.dumps({'format':'video-ai-m1-schema-fixture/1','fixture_only':True,
    'ddl':ddl,'tables':[table.name for table in Base.metadata.sorted_tables],
    'rows':rows,'migration_rows':versions,
    'database_identity':engine.connect().execute(text("SELECT json_build_array(current_database(),(SELECT oid FROM pg_database WHERE datname=current_database()),pg_postmaster_start_time(),current_setting('server_version_num'))")).scalar()},default=encode,sort_keys=True))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--database-env',required=True);parser.add_argument('--pg-container',required=True);args=parser.parse_args()
    if args.output.exists():parser.error('New fixture filename required')
    base=DatabaseEndpoint.from_environment(args.database_env);url=make_url(os.environ[args.database_env])
    if url.host not in {'localhost','127.0.0.1'} or 'test' not in (url.database or ''):parser.error('Explicit local disposable test database required')
    checked=subprocess.run(['docker','image','inspect','--format','{{.Id}}',IMAGE],capture_output=True,text=True,check=False)
    if checked.returncode or checked.stdout.strip()!=IMAGE:raise RuntimeError('cached_m1_image_required')
    name='video_ai_restore_capture_'+uuid4().hex[:24]
    admin=DatabaseEndpoint({**base.parameters,'dbname':'postgres'});source=DatabaseEndpoint({**base.parameters,'dbname':name})
    client=PgClient(container=args.pg_container,host='127.0.0.1',port=5432,timeout=90);created=False
    try:
        with admin.connect(autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)));created=True
        with source.connect() as db:identity=instance_identity(db)
        with private_scratch(tempfile.gettempdir()) as scratch:
            client.verify_route(source,identity,scratch);client.verify_version('pg_dump',source,scratch)
            env={**os.environ,'DATABASE_URL':url.set(host='127.0.0.1',port=5432,database=name).render_as_string(hide_password=False)}
            result=subprocess.run(['docker','run','--rm','--network','container:'+args.pg_container,'--read-only','--memory','256m','--cpus','1',
                '--pids-limit','64','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--env','DATABASE_URL','--env','APP_ENVIRONMENT=test','--env','AUTH_MODE=development_header',
                '--env','PROVIDER_MODE=api_only','--env','ASSET_STORAGE_MODE=external',
                '--entrypoint','python',IMAGE,'-c',CAPTURE],env=env,capture_output=True,text=True,encoding='utf-8',timeout=90,check=False)
            if result.returncode:raise RuntimeError('offline_m1_pg_fixture_capture_failed')
            value=json.loads(result.stdout)
            if value.pop('database_identity')!=identity:raise RuntimeError('legacy_container_database_route_mismatch')
            dump=scratch/'schema.sql'
            client.run('pg_dump',['--schema-only','--no-owner','--no-privileges','--no-password'],source,output=dump,scratch=scratch)
            lines=[]
            for line in dump.read_text(encoding='utf-8').splitlines():
                if line.startswith(('\\restrict ','\\unrestrict ')):continue
                if line.startswith('\\'):raise RuntimeError('unexpected_pg_dump_meta_command')
                lines.append(line)
            value.pop('ddl');value['postgres_schema_sql']='\n'.join(lines)+'\n'
            value['format']='video-ai-m1-pg-schema-fixture/1';value['postgresql_major']=16;value['origin_image_id']=IMAGE
            if value['migration_rows'][-1]['version']!='0007_usage_ledger_status':raise RuntimeError('unexpected_m1_schema_version')
    finally:
        if created:
            with admin.connect(autocommit=True) as db:db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    with args.output.open('xb') as stream:stream.write(raw)
    print(json.dumps({'ok':True,'origin_image_id':IMAGE,'tables':len(value['tables']),
        'rows':sum(map(len,value['rows'].values())),'fixture_sha256':hashlib.sha256(raw).hexdigest(),'cloud_calls':0}))


if __name__=='__main__':main()
