"""Operator-only encrypted PostgreSQL backups and verified, quarantined restores.

No command activates a restored database or contacts a media/provider service.
"""
import argparse
import base64
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import time
from uuid import UUID,uuid4

from .pg_recovery import (
    DatabaseEndpoint,PgClient,canonical,create_quarantined_database,
    database_properties,instance_identity,sequence_state,snapshot_state,
)
from .recovery_bundle import (
    BLOCK,MAX_BUNDLE,RecoveryToolError,copy_config,decrypt_file,digest_file,
    encrypt_file,load_key,private_directory,private_file,private_scratch,write_json,
)

FORMAT='video-ai-recovery/1'
MANIFEST_LIMIT=16*1024**2
CONFIG_LIMIT=2*1024**2
LABEL=r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'


def _now():return datetime.now(timezone.utc).isoformat()
def _key_id(key):return hashlib.sha256(key).hexdigest()[:24]


def _require_server(identity):
    if int(identity[3])//10000!=16:raise RecoveryToolError('postgresql_16_server_required')


def _summary(manifest):
    state=manifest['snapshot']
    return {'format':FORMAT,'backup_id':manifest['backup_id'],
        'snapshot_observed_at':manifest['snapshot_observed_at'],
        'tables':len(state['tables']),'rows':sum(row['count'] for row in state['tables'].values()),
        'migrations':state['migrations'],'configs':len(manifest['files'])-1,
        'media_objects_included':False,'database_roles_included':False}


def _publish(bundle,manifest,key,output_directory,*,started_clock,**extra):
    output=private_directory(output_directory)
    partial=output/'metadata.vab.partial';final=output/'metadata.vab'
    encrypt_file(bundle,partial,key);partial.rename(final)
    receipt={'ok':True,**_summary(manifest),'encrypted_sha256':digest_file(final),
        'key_fingerprint':_key_id(key),'finished_at':_now(),
        'elapsed_seconds':round(time.monotonic()-started_clock,3),**extra}
    write_json(output/'receipt.json',receipt)
    return receipt


def backup_database(endpoint,client,key,*,output_directory,scratch_root,configs=None):
    started=time.monotonic()
    configs=configs or {}
    if len(configs)>32 or any(not re.fullmatch(LABEL,label) for label in configs):
        raise RecoveryToolError('config_labels_invalid')
    # Validate destination before holding a database snapshot. It is never reused.
    if Path(output_directory).exists():raise RecoveryToolError('new_private_directory_required')
    with private_scratch(scratch_root) as scratch:
        client.verify_version('pg_dump',endpoint,scratch)
        with endpoint.connect() as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            identity=instance_identity(db);_require_server(identity)
            client.verify_route(endpoint,identity,scratch)
            exported=db.execute('SELECT pg_export_snapshot()').fetchone()[0]
            observed=db.execute('SELECT clock_timestamp()').fetchone()[0].isoformat()
            state=snapshot_state(db);properties=database_properties(db)
            dump=scratch/'metadata.dump'
            client.run('pg_dump',['--format=custom','--no-owner','--no-privileges','--no-password','--snapshot='+exported],endpoint,output=dump,scratch=scratch)
            # Sequence state is not MVCC. Do not claim consistency if nextval ran.
            if sequence_state(db)!=state['sequences']:
                raise RecoveryToolError('sequence_changed_require_quiet_backup_window')
        files={'metadata.dump':{'size':dump.stat().st_size,'sha256':digest_file(dump)}}
        paths={'metadata.dump':dump}
        for label,path in configs.items():
            copied=scratch/('config-'+label);copy_config(path,copied)
            name='config/'+label;paths[name]=copied
            files[name]={'size':copied.stat().st_size,'sha256':digest_file(copied)}
        manifest={'format':FORMAT,'backup_id':str(uuid4()),'created_at':_now(),
            'snapshot_observed_at':observed,'postgresql_major':16,'database_properties':properties,
            'source_endpoint_tag':endpoint.tag(),'snapshot':state,'files':files}
        manifest_path=scratch/'manifest.json';write_json(manifest_path,manifest)
        if manifest_path.stat().st_size>MANIFEST_LIMIT:raise RecoveryToolError('manifest_size_limit')
        bundle=scratch/'bundle.tar'
        with private_file(bundle) as stream,tarfile.open(fileobj=stream,mode='w',format=tarfile.USTAR_FORMAT) as archive:
            for name,path in {'manifest.json':manifest_path,**paths}.items():
                info=tarfile.TarInfo(name);info.size=path.stat().st_size;info.mode=0o600
                with path.open('rb') as source:archive.addfile(info,source)
        return _publish(bundle,manifest,key,output_directory,started_clock=started,operation='backup')


def _unique_json(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise RecoveryToolError('manifest_duplicate_key')
        result[key]=value
    return result


def _validate_manifest(value):
    try:
        if value['format']!=FORMAT or value['postgresql_major']!=16:raise ValueError()
        UUID(value['backup_id']);datetime.fromisoformat(value['snapshot_observed_at'])
        files=value['files'];state=value['snapshot'];properties=value['database_properties']
        if not isinstance(files,dict) or not 1<=len(files)<=33 or 'metadata.dump' not in files:raise ValueError()
        if set(properties)!={'encoding','collate','ctype','locale_provider','icu_locale'}:raise ValueError()
        if properties['locale_provider'] not in ('c','i'):raise ValueError()
        if not isinstance(state['tables'],dict) or not isinstance(state['migrations'],list):raise ValueError()
        if hashlib.sha256(canonical({key:item for key,item in state.items() if key!='sha256'})).hexdigest()!=state['sha256']:raise ValueError()
        for name,metadata in files.items():
            if name!='metadata.dump' and not re.fullmatch('config/'+LABEL,name):raise ValueError()
            size=metadata['size']
            if type(size) is not int or not 0<=size<=(MAX_BUNDLE if name=='metadata.dump' else CONFIG_LIMIT):raise ValueError()
            if not re.fullmatch('[0-9a-f]{64}',metadata['sha256']):raise ValueError()
        if sum(item['size'] for item in files.values())>MAX_BUNDLE:raise ValueError()
        _summary(value)
    except (KeyError,ValueError,TypeError,AttributeError):raise RecoveryToolError('backup_manifest_invalid') from None
    return value


def _unpack_authenticated(source,key,scratch):
    bundle=scratch/'authenticated.tar'
    # This authenticates the entire envelope before tarfile or PostgreSQL is used.
    decrypt_file(source,bundle,key)
    paths={};seen=set();manifest=None
    try:
        with tarfile.open(bundle,'r:') as archive:
            for index,member in enumerate(archive):
                if index>=34 or not member.isfile() or member.sparse is not None or member.name in seen:
                    raise RecoveryToolError('backup_archive_member_invalid')
                seen.add(member.name)
                if index==0:
                    if member.name!='manifest.json' or not 0<member.size<=MANIFEST_LIMIT:
                        raise RecoveryToolError('backup_manifest_missing_or_oversized')
                    with archive.extractfile(member) as stream:
                        manifest=_validate_manifest(json.loads(stream.read(),object_pairs_hook=_unique_json))
                    continue
                if member.name not in manifest['files'] or member.size!=manifest['files'][member.name]['size']:
                    raise RecoveryToolError('backup_archive_member_mismatch')
                target=scratch/('verified-'+str(index))
                with archive.extractfile(member) as original,private_file(target) as copy:
                    shutil.copyfileobj(original,copy,BLOCK)
                if target.stat().st_size!=member.size or digest_file(target)!=manifest['files'][member.name]['sha256']:
                    raise RecoveryToolError('backup_member_checksum_mismatch')
                paths[member.name]=target
    except (tarfile.TarError,ValueError,UnicodeError):raise RecoveryToolError('backup_archive_invalid') from None
    if manifest is None or seen!={'manifest.json',*manifest['files']}:
        raise RecoveryToolError('backup_archive_incomplete')
    return bundle,manifest,paths


def inspect_backup(source,key,*,scratch_root):
    with private_scratch(scratch_root) as scratch:
        _,manifest,_=_unpack_authenticated(source,key,scratch)
        return {'ok':True,**_summary(manifest),'operation':'inspect','authenticated':True,
            'encrypted_sha256':digest_file(source),'key_fingerprint':_key_id(key)}


def rewrap_backup(source,key,new_key,*,output_directory,scratch_root):
    started=time.monotonic()
    if key==new_key:raise RecoveryToolError('rotation_requires_different_key')
    with private_scratch(scratch_root) as scratch:
        bundle,manifest,_=_unpack_authenticated(source,key,scratch)
        return _publish(bundle,manifest,new_key,output_directory,started_clock=started,operation='rewrap',
            previous_encrypted_sha256=digest_file(source))


def _install_guard(db,backup_id):
    exists=db.execute("SELECT to_regclass('public.database_recovery_state') IS NOT NULL").fetchone()[0]
    db.execute('''CREATE TABLE IF NOT EXISTS public.database_recovery_state (
        id INTEGER PRIMARY KEY, instance_id VARCHAR(36) NOT NULL,
        state VARCHAR(24) NOT NULL, backup_id VARCHAR(36),
        created_at TIMESTAMP WITH TIME ZONE NOT NULL, updated_at TIMESTAMP WITH TIME ZONE NOT NULL)''')
    instance=str(uuid4())
    db.execute('''INSERT INTO public.database_recovery_state
        (id,instance_id,state,backup_id,created_at,updated_at)
        VALUES (1,%s,'quarantined',%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET instance_id=EXCLUDED.instance_id,
        state=EXCLUDED.state,backup_id=EXCLUDED.backup_id,updated_at=EXCLUDED.updated_at''',(instance,backup_id))
    return {'table_created':not exists,'singleton_fields':['instance_id','state','backup_id','updated_at']}


def restore_database(source,key,admin,client,*,database,output_directory,scratch_root,export_configs=False):
    started=time.monotonic()
    if not re.fullmatch('video_ai_restore_[a-z0-9_]{1,40}',database):
        raise RecoveryToolError('new_restore_database_name_required')
    if Path(output_directory).exists():raise RecoveryToolError('new_private_directory_required')
    with private_scratch(scratch_root) as scratch:
        _,manifest,paths=_unpack_authenticated(source,key,scratch)
        client.verify_version('pg_restore',admin,scratch)
        with admin.connect() as db:
            identity=instance_identity(db);_require_server(identity)
            client.verify_route(admin,identity,scratch)
        create_quarantined_database(admin,database,manifest['database_properties'])
        with admin.connect(database=database) as db:
            identity=instance_identity(db)
            if db.execute("SELECT current_setting('video_ai.recovery_quarantine',true)").fetchone()[0]!='on':
                raise RecoveryToolError('restore_quarantine_unconfirmed')
            client.verify_route(admin,identity,scratch,database=database)
        client.run('pg_restore',['--exit-on-error','--single-transaction','--no-owner','--no-privileges','--no-password','--dbname='+database],
            admin,input_path=paths['metadata.dump'],database=database,scratch=scratch)
        with admin.connect(database=database) as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            actual=snapshot_state(db)
            if actual!=manifest['snapshot']:raise RecoveryToolError('restored_database_fingerprint_mismatch')
            delta=_install_guard(db,manifest['backup_id'])
        output=private_directory(output_directory)
        if export_configs:
            exported=private_directory(output/'recovered-config')
            for name,path in paths.items():
                if name.startswith('config/'):copy_config(path,exported/name.removeprefix('config/'))
        receipt={'ok':True,**_summary(manifest),'operation':'restore','target_database':database,
            'encrypted_sha256':digest_file(source),'key_fingerprint':_key_id(key),
            'metadata_verified_before_guard':True,'control_delta':delta,'quarantined':True,
            'workers_allowed':False,'configs_exported':bool(export_configs),'configs_applied':False,
            'finished_at':_now(),'elapsed_seconds':round(time.monotonic()-started,3)}
        write_json(output/'receipt.json',receipt)
        return receipt


def generate_key(output_directory):
    output=private_directory(output_directory);key=os.urandom(32)
    with private_file(output/'backup.key') as stream:
        stream.write(base64.b64encode(key)+b'\n');stream.flush();os.fsync(stream.fileno())
    receipt={'ok':True,'operation':'keygen','key_fingerprint':_key_id(key)}
    write_json(output/'receipt.json',receipt);return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description='Authenticated PG16 metadata recovery. Restores stay quarantined.')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('backup','restore','inspect','rewrap','keygen'):
        command=commands.add_parser(name)
        if name!='inspect':command.add_argument('--output-directory',required=True)
        if name=='keygen':continue
        command.add_argument('--key-env',default='VIDEO_AI_BACKUP_KEY')
        command.add_argument('--scratch-root',default=tempfile.gettempdir())
        if name!='backup':command.add_argument('--input',required=True)
        if name=='rewrap':command.add_argument('--new-key-env',required=True)
        if name in ('backup','restore'):
            command.add_argument('--database-env',default='VIDEO_AI_DATABASE_URL' if name=='backup' else 'VIDEO_AI_RESTORE_ADMIN_URL')
            command.add_argument('--pg-container')
            command.add_argument('--client-host')
            command.add_argument('--client-port',type=int)
            command.add_argument('--client-binary-directory')
            command.add_argument('--client-timeout',type=int,default=900)
        if name=='backup':command.add_argument('--config',action='append',default=[],metavar='LABEL=PATH')
        if name=='restore':
            command.add_argument('--new-database',required=True)
            command.add_argument('--export-configs',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='keygen':result=generate_key(args.output_directory)
        else:
            key=load_key(args.key_env)
            if args.command in ('backup','restore'):
                endpoint=DatabaseEndpoint.from_environment(args.database_env)
                if not 1<=args.client_timeout<=3600:raise RecoveryToolError('client_timeout_invalid')
                client=PgClient(args.pg_container,args.client_host,args.client_port,args.client_binary_directory,args.client_timeout)
            if args.command=='backup':
                configs={}
                for item in args.config:
                    if '=' not in item:raise RecoveryToolError('config_label_and_path_required')
                    label,path=item.split('=',1)
                    if label in configs:raise RecoveryToolError('config_labels_must_be_unique')
                    configs[label]=path
                result=backup_database(endpoint,client,key,output_directory=args.output_directory,scratch_root=args.scratch_root,configs=configs)
            elif args.command=='restore':result=restore_database(args.input,key,endpoint,client,database=args.new_database,output_directory=args.output_directory,scratch_root=args.scratch_root,export_configs=args.export_configs)
            elif args.command=='inspect':result=inspect_backup(args.input,key,scratch_root=args.scratch_root)
            elif args.command=='rewrap':result=rewrap_backup(args.input,key,load_key(args.new_key_env),output_directory=args.output_directory,scratch_root=args.scratch_root)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except RecoveryToolError as error:code=error.code
    except Exception:code='recovery_operation_failed'
    # Never echo psycopg / libpq errors, config values, DSNs or traceback.
    print(json.dumps({'ok':False,'code':code,'target_activated':False}));return 1


if __name__=='__main__':raise SystemExit(main())
