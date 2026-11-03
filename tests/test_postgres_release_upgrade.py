"""Actual frozen M1 v8 PG16 schema -> current migrations, without real media IO."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

import pytest
from psycopg import sql
from sqlalchemy import DateTime,MetaData

import app.migrations as migrations
from app.services.database_recovery import backup_database,restore_database
from app.services.pg_recovery import DatabaseEndpoint
from app.services.release_bundle import build_release,ReleaseError
from app.services.release_preflight import assess_release
from test_postgres_recovery import (
    pg_recovery_lab as _pg_recovery_lab,target_factory,fingerprint,
)
from test_release_bundle import release_source_base as _release_source_base

pg_recovery_lab=_pg_recovery_lab
release_source_base=_release_source_base
FIXTURE=Path(__file__).parent/'fixtures'/'m1-control-v8.postgres.json'
FIXTURE_SHA='10204d5658a56ba1cf6081c2bcbecd72abb6a650303450a231bb5667d2035d51'


@pytest.fixture(scope='module')
def source_candidate(release_source_base,tmp_path_factory):
    parent=tmp_path_factory.mktemp('release-candidate')
    receipt=build_release(release_source_base,parent/'bundle',label='m2-upgrade-fixture')
    return parent/'bundle'/'release.tar.gz',receipt


@pytest.fixture
def legacy_pg(pg_recovery_lab):
    admin,client,reserve,_=pg_recovery_lab;name=reserve()
    raw=FIXTURE.read_bytes();assert hashlib.sha256(raw).hexdigest()==FIXTURE_SHA
    captured=json.loads(raw)
    assert captured['origin_image_id']=='sha256:227302226b8fcd518ca0dde35b3857da483e2d723c4b6e7659d33fd0dec8c9d5'
    assert captured['format']=='video-ai-m1-pg-schema-fixture/1' and captured['fixture_only']
    with admin.connect(autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
    endpoint=DatabaseEndpoint({**admin.parameters,'dbname':name})
    with endpoint.connect() as db:db.execute(captured['postgres_schema_sql'],prepare=False)
    engine,factory=target_factory(admin,name);metadata=MetaData();metadata.reflect(bind=engine)
    try:
        with engine.begin() as db:
            for table in metadata.sorted_tables:
                raw_rows=captured['migration_rows'] if table.name=='schema_migrations' else captured['rows'].get(table.name,[])
                rows=[]
                for row in raw_rows:
                    row=dict(row)
                    for column in table.columns:
                        if isinstance(column.type,DateTime) and isinstance(row.get(column.name),str):row[column.name]=datetime.fromisoformat(row[column.name])
                    rows.append(row)
                if rows:db.execute(table.insert(),rows)
        yield endpoint,engine,admin,client,reserve,captured
    finally:engine.dispose()


def old_migration_rows(endpoint,versions):
    with endpoint.connect() as db:
        return db.execute('SELECT row_to_json(t)::text FROM public.schema_migrations t WHERE version=ANY(%s) ORDER BY version',(versions,)).fetchall()


def evidence(name,value):
    root=os.getenv('RELEASE_EVIDENCE_DIR')
    if root:
        path=Path(root);path.mkdir(parents=True,exist_ok=True)
        (path/name).write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')


def test_actual_m1_backup_restore_upgrade_preserves_original_jobs_costs_and_references(legacy_pg,source_candidate,tmp_path):
    source,_,admin,client,reserve,captured=legacy_pg;archive,candidate=source_candidate
    initial=fingerprint(source);old_versions=initial['migrations'];old_history=old_migration_rows(source,old_versions)
    assert old_versions[-1]=='0007_usage_ledger_status' and len(initial['tables'])==25
    assessment=assess_release(archive,candidate['sha256'],source)
    assert len(assessment['pending_migrations'])==16 and not assessment['ready_for_production']
    assert assessment['database']['held_ledger_rows']==1 and assessment['database']['unresolved_remote_operations']==1
    assert not assessment['rollback']['m1_image_supported_after_m2_migration']
    assert fingerprint(source)==initial
    key=os.urandom(32);backup=backup_database(source,client,key,output_directory=tmp_path/'backup',scratch_root=tmp_path)
    name=reserve();restored=restore_database(tmp_path/'backup'/'metadata.vab',key,admin,client,database=name,output_directory=tmp_path/'restored',scratch_root=tmp_path)
    target=DatabaseEndpoint({**admin.parameters,'dbname':name});target_engine,factory=target_factory(admin,name)
    try:
        with target.connect() as db:control_before=db.execute('SELECT * FROM database_recovery_state').fetchall()
        versions=migrations.migrate(target_engine)
        assert '0020_database_recovery_state' in versions
        after=fingerprint(target)
        for table,state in initial['tables'].items():
            if table!='public.schema_migrations':assert after['tables'][table]==state,table
        assert old_migration_rows(target,old_versions)==old_history
        with target.connect() as db:
            assert db.execute('SELECT * FROM database_recovery_state').fetchall()==control_before
            assert db.execute("SELECT current_setting('video_ai.recovery_quarantine',true)").fetchone()[0]=='on'
            assert db.execute('SELECT status,amount_micros,currency FROM usage_ledger').fetchall()==[('pending_reconciliation',350000,'CNY')]
        current=assess_release(archive,candidate['sha256'],target)
        assert not current['pending_migrations'] and current['target_must_remain_quarantined']
        assert current['database']['tenants_without_personal_owner']==1
        assert migrations.require_current_schema(target_engine)==versions
        assert migrations.migrate(target_engine)==versions and fingerprint(target)==after
        assert fingerprint(source)==initial
        evidence('m1-upgrade-evidence.json',{'fixture_sha256':FIXTURE_SHA,'origin_image_id':captured['origin_image_id'],
            'source_version':old_versions[-1],'target_version':after['migrations'][-1],
            'original_tables':len(initial['tables']),'upgraded_tables':len(after['tables']),
            'original_rows':sum(row['count'] for row in initial['tables'].values()),
            'upgraded_rows':sum(row['count'] for row in after['tables'].values()),
            'all_original_application_rows_preserved':True,'old_migration_rows_preserved':True,
            'source_unchanged':True,'repeated_upgrade_unchanged':True,'restored_quarantine_preserved':True,
            'pending_reconciliation_micros':350000,'fixture_only':True,'cloud_calls':0,
            'backup':backup,'restore':restored,'assessment_before':assessment,'assessment_after':current})
    finally:target_engine.dispose()


def test_postgres_ddl_and_history_rollback_together_on_upgrade_failure(legacy_pg,monkeypatch):
    source,engine,_,_,_,_=legacy_pg;before=fingerprint(source)
    def fail(connection):
        connection.exec_driver_sql('CREATE TABLE intentional_failed_migration (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql("UPDATE jobs SET status='failed'")
        raise RuntimeError('intentional_upgrade_failure')
    injected=migrations.Migration(version='0022_injected_failure',description='Test-only rollback fixture',apply=fail)
    monkeypatch.setattr(migrations,'MIGRATIONS',(*migrations.MIGRATIONS,injected))
    with pytest.raises(RuntimeError,match='intentional_upgrade_failure'):migrations.migrate(engine)
    assert fingerprint(source)==before
    evidence('migration-failure-evidence.json',{'transactional_ddl_rollback':True,'migration_history_unchanged':True,
        'all_original_rows_unchanged':True,'fixture_only':True,'cloud_calls':0})


def test_preflight_and_migrator_refuse_future_or_incomplete_history_without_writes(legacy_pg,source_candidate):
    source,engine,_,_,_,_=legacy_pg;archive,candidate=source_candidate
    with source.connect() as db:db.execute("DELETE FROM schema_migrations WHERE version='0006_artifact_archive_tasks'")
    before=fingerprint(source)
    with pytest.raises(ReleaseError,match='history_incomplete'):assess_release(archive,candidate['sha256'],source)
    assert fingerprint(source)==before
    with source.connect() as db:db.execute("INSERT INTO schema_migrations VALUES ('9999_fixture_future','Explicit unknown version',CURRENT_TIMESTAMP)")
    before=fingerprint(source)
    with pytest.raises(ReleaseError,match='newer_or_unknown'):assess_release(archive,candidate['sha256'],source)
    with pytest.raises(migrations.DatabaseSchemaError,match='newer_or_unknown'):migrations.migrate(engine)
    assert fingerprint(source)==before
