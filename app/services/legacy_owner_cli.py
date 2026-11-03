"""Explicit owner binding on a quarantined PG copy; credentials stay private."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets



def _password(args):
    if args.password_env:
        if not re.fullmatch('[A-Z][A-Z0-9_]{0,100}',args.password_env):raise ValueError('password_environment_invalid')
        return os.environ.get(args.password_env,'')
    path=Path(args.password_file)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>2048:raise ValueError('password_file_invalid')
    if os.name!='nt' and path.stat().st_mode&0o077:raise ValueError('password_file_must_be_private')
    return path.read_text(encoding='utf-8').rstrip('\r\n')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    generate=commands.add_parser('make-password');generate.add_argument('--output-directory',required=True)
    for name in ('plan','apply','status'):
        command=commands.add_parser(name);command.add_argument('--database-env',default='VIDEO_AI_DATABASE_URL')
        if name=='status':command.add_argument('--request-id',required=True);continue
        command.add_argument('--tenant-id',required=True);command.add_argument('--expected-slug',required=True);command.add_argument('--login-name',required=True)
        if name=='apply':
            command.add_argument('--request-id',required=True);command.add_argument('--database-instance-id',required=True)
            command.add_argument('--display-name',default='')
            source=command.add_mutually_exclusive_group(required=True);source.add_argument('--password-env');source.add_argument('--password-file')
    args=parser.parse_args(argv)
    try:
        from app.services.recovery_bundle import private_directory,private_file,write_json
        if args.command=='make-password':
            output=private_directory(args.output_directory)
            with private_file(output/'initial-password.txt') as stream:stream.write((secrets.token_urlsafe(32)+'\n').encode())
            result={'ok':True,'credential_created':True,'file':'initial-password.txt','password_returned':False}
            write_json(output/'receipt.json',result)
        else:
            from sqlalchemy import create_engine
            from sqlalchemy.engine import make_url
            from sqlalchemy.orm import Session
            from app.migrations import require_current_schema
            from app.services.pg_recovery import DatabaseEndpoint
            from app.services.legacy_owner import AdoptionError,AdoptionIntent,adoption_status,apply_adoption,plan_adoption
            from app.services.identity import hash_password
            DatabaseEndpoint.from_environment(args.database_env)
            engine=create_engine(make_url(os.environ[args.database_env]).set(drivername='postgresql+psycopg'))
            try:
                require_current_schema(engine)
                with Session(engine,expire_on_commit=False) as db:
                    if args.command=='plan':result={'ok':True,**plan_adoption(db,tenant_id=args.tenant_id,expected_slug=args.expected_slug,login_name=args.login_name)}
                    elif args.command=='status':result={'ok':True,**adoption_status(db,args.request_id)}
                    else:
                        intent=AdoptionIntent.make(request_id=args.request_id,tenant_id=args.tenant_id,expected_slug=args.expected_slug,
                            database_instance_id=args.database_instance_id,login_name=args.login_name,display_name=args.display_name)
                        existing=adoption_status(db,intent.request_id)['found'];db.rollback()
                        if not existing:
                            with db.begin():
                                planned=plan_adoption(db,tenant_id=intent.tenant_id,expected_slug=intent.expected_slug,login_name=intent.login_name)
                                if planned['database_instance_id']!=intent.database_instance_id:raise AdoptionError('adoption_database_instance_mismatch')
                        encoded=None if existing else hash_password(_password(args))
                        with db.begin():result={'ok':True,**apply_adoption(db,intent,password_hash=encoded)}
            finally:engine.dispose()
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except Exception as error:
        code=getattr(error,'code',None)
        allowed={'database_migration_required','database_version_newer_or_unknown','password_environment_invalid','password_file_invalid','password_file_must_be_private'}
        if not code:code=str(error) if str(error) in allowed else 'legacy_adoption_operation_failed'
        print(json.dumps({'ok':False,'code':code,'password_returned':False,'execution_activated':False}));return 1


if __name__=='__main__':raise SystemExit(main())
