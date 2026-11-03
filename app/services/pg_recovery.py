"""PostgreSQL 16 snapshot/dump clients. Credentials never enter command arguments."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from .recovery_bundle import RecoveryToolError,private_file


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


@dataclass(frozen=True)
class DatabaseEndpoint:
    parameters: dict

    @classmethod
    def from_environment(cls,name):
        if not re.fullmatch('[A-Z][A-Z0-9_]{0,100}',name):raise RecoveryToolError('database_environment_name_invalid')
        try:
            url=make_url(os.environ.get(name,''))
            if url.drivername not in {'postgresql','postgresql+psycopg'} or not url.database:raise ValueError()
            allowed={'sslmode','sslrootcert','sslcert','sslkey','connect_timeout'}
            if set(url.query)-allowed:raise ValueError()
            params={'dbname':url.database,'user':url.username,'password':url.password,'host':url.host,'port':url.port or 5432,'connect_timeout':10,**url.query}
            return cls({key:value for key,value in params.items() if value is not None})
        except Exception:raise RecoveryToolError('postgresql_endpoint_invalid') from None

    def connect(self,*,database=None,autocommit=False):
        params={**self.parameters,**({'dbname':database} if database else {})}
        return psycopg.connect(**params,autocommit=autocommit)

    def tag(self):
        values={key:self.parameters.get(key) for key in ('host','port','dbname')}
        return hashlib.sha256(canonical(values)).hexdigest()[:24]


@dataclass
class PgClient:
    container: str | None = None
    host: str | None = None
    port: int | None = None
    binary_directory: str | None = None
    timeout: int = 900

    def command(self,program):
        if program not in {'pg_dump','pg_restore','psql'}:raise RecoveryToolError('postgresql_program_invalid')
        if self.container:
            if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',self.container):raise RecoveryToolError('client_container_invalid')
            envs=('PGHOST','PGPORT','PGDATABASE','PGUSER','PGPASSWORD','PGSSLMODE','PGSSLROOTCERT','PGSSLCERT','PGSSLKEY','PGCONNECT_TIMEOUT','PGAPPNAME')
            return ['docker','exec','-i','--user','postgres',*(arg for key in envs for arg in ('--env',key)),self.container,program]
        return [str(Path(self.binary_directory)/program) if self.binary_directory else program]

    def environment(self,endpoint,*,database=None,application=None):
        env={key:value for key,value in os.environ.items() if not key.startswith('PG')}
        names={'host':'PGHOST','port':'PGPORT','dbname':'PGDATABASE','user':'PGUSER','password':'PGPASSWORD','sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','connect_timeout':'PGCONNECT_TIMEOUT'}
        params={**endpoint.parameters,**({'dbname':database} if database else {}),**({'host':self.host} if self.host is not None else {}),**({'port':self.port} if self.port is not None else {})}
        # libpq treats an empty PGSSLMODE as invalid, not as its default.
        for key,name in names.items():
            if params.get(key) is not None:env[name]=str(params[key])
        env['PGAPPNAME']=application or 'video_ai_backup_client'
        return env

    def run(self,program,args,endpoint,*,output=None,input_path=None,database=None,scratch):
        application='video_ai_recovery_'+uuid4().hex
        error_path=Path(scratch)/('client-'+uuid4().hex+'.stderr')
        with private_file(error_path) as errors:
            result_out=private_file(output) if output else subprocess.DEVNULL
            source=Path(input_path).open('rb') if input_path else subprocess.DEVNULL
            try:
                result=subprocess.run([*self.command(program),*args],env=self.environment(endpoint,database=database,application=application),
                    stdin=source,stdout=result_out,stderr=errors,timeout=self.timeout,check=False)
            except subprocess.TimeoutExpired:
                # Docker exec can outlive its CLI. Terminate only our tagged DB session.
                try:
                    with endpoint.connect(database=database,autocommit=True) as db:
                        db.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() AND application_name=%s AND pid<>pg_backend_pid()',(application,))
                except Exception:pass
                raise RecoveryToolError('postgresql_client_timeout_target_unactivated') from None
            except OSError:raise RecoveryToolError('postgresql_client_unavailable') from None
            finally:
                if output:result_out.close()
                if input_path:source.close()
        if result.returncode:raise RecoveryToolError(program+'_failed')

    def verify_route(self,endpoint,expected,scratch,*,database=None):
        route=Path(scratch)/('route-'+uuid4().hex+'.json')
        self.run('psql',['-X','-A','-t','-c',"SELECT json_build_array(current_database(),(SELECT oid FROM pg_database WHERE datname=current_database()),pg_postmaster_start_time(),current_setting('server_version_num'))::text"],endpoint,output=route,database=database,scratch=scratch)
        try:actual=json.loads(route.read_text(encoding='utf-8').strip())
        except (ValueError,OSError):raise RecoveryToolError('postgresql_client_route_unverified') from None
        if actual!=expected:raise RecoveryToolError('postgresql_client_route_mismatch')

    def verify_version(self,program,endpoint,scratch):
        output=Path(scratch)/('version-'+uuid4().hex+'.txt')
        self.run(program,['--version'],endpoint,output=output,scratch=scratch)
        if not re.search(r'\(PostgreSQL\) 16\.',output.read_text(encoding='utf-8')):
            raise RecoveryToolError('postgresql_16_client_required')


def instance_identity(connection):
    return connection.execute("SELECT json_build_array(current_database(),(SELECT oid FROM pg_database WHERE datname=current_database()),pg_postmaster_start_time(),current_setting('server_version_num'))").fetchone()[0]


def database_properties(connection):
    row=connection.execute("SELECT pg_encoding_to_char(encoding),datcollate,datctype,datlocprovider,daticulocale FROM pg_database WHERE datname=current_database()").fetchone()
    return dict(zip(('encoding','collate','ctype','locale_provider','icu_locale'),row))


def snapshot_state(connection,*,projection=None):
    """Stream every physical table row plus schema/sequence/LO fingerprints."""
    connection.execute("SET LOCAL TIME ZONE 'UTC'");connection.execute("SET LOCAL DateStyle='ISO, YMD'")
    foreign=connection.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='f' AND n.nspname NOT IN ('pg_catalog','information_schema')").fetchone()[0]
    if foreign:raise RecoveryToolError('foreign_tables_require_separate_recovery_plan')
    namespaces="n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'"
    tables=connection.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND "+namespaces+" ORDER BY n.nspname,c.relname").fetchall()
    catalog={
        'columns':connection.execute("SELECT n.nspname,c.relname,c.relkind,c.relpersistence,row_number() OVER (PARTITION BY c.oid ORDER BY a.attnum),a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum WHERE a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m') AND "+namespaces+" ORDER BY n.nspname,c.relname,a.attnum").fetchall(),
        'constraints':connection.execute("SELECT n.nspname,c.relname,p.conname,pg_get_constraintdef(p.oid,true) FROM pg_constraint p JOIN pg_class c ON c.oid=p.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE "+namespaces+" ORDER BY n.nspname,c.relname,p.conname").fetchall(),
        'indexes':connection.execute("SELECT n.nspname,c.relname,pg_get_indexdef(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='i' AND "+namespaces+" ORDER BY n.nspname,c.relname").fetchall(),
        'views':connection.execute("SELECT n.nspname,c.relname,pg_get_viewdef(c.oid,true) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('v','m') AND "+namespaces+" ORDER BY n.nspname,c.relname").fetchall(),
        'functions':connection.execute("SELECT n.nspname,p.proname,pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.prokind IN ('f','p','w') AND "+namespaces+" ORDER BY n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)").fetchall(),
        'triggers':connection.execute("SELECT n.nspname,c.relname,t.tgname,pg_get_triggerdef(t.oid,true) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND "+namespaces+" ORDER BY n.nspname,c.relname,t.tgname").fetchall(),
        'extensions':connection.execute("SELECT e.extname,e.extversion,n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace ORDER BY e.extname").fetchall(),
        'sequence_defs':connection.execute("SELECT schemaname,sequencename,data_type::text,start_value,min_value,max_value,increment_by,cycle,cache_size FROM pg_sequences WHERE schemaname NOT LIKE 'pg_%' ORDER BY schemaname,sequencename").fetchall(),
    }
    data={}
    for schema,name in tables:
        identifier=schema+'.'+name
        if projection is not None and identifier not in projection:continue
        columns=projection.get(identifier) if projection is not None else None
        primary=connection.execute("SELECT a.attname FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,n) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE i.indisprimary AND i.indrelid=%s::regclass ORDER BY k.n",(sql.Identifier(schema,name).as_string(connection),)).fetchall()
        query=sql.SQL('SELECT row_to_json(t)::text FROM {}.{} t ORDER BY {}').format(sql.Identifier(schema),sql.Identifier(name),
            sql.SQL(',').join(sql.Identifier(row[0]) for row in primary) if primary else sql.SQL('row_to_json(t)::text COLLATE "C"'))
        if columns is not None:
            query=sql.SQL('SELECT row_to_json(t)::text FROM (SELECT {} FROM {}.{} ORDER BY {}) t').format(sql.SQL(',').join(sql.Identifier(column) for column in columns),sql.Identifier(schema),sql.Identifier(name),
                sql.SQL(',').join(sql.Identifier(row[0]) for row in primary) if primary else sql.SQL(',').join(sql.Identifier(column) for column in columns))
        count=0;digest=hashlib.sha256()
        with connection.cursor(name='backup_'+uuid4().hex) as cursor:
            cursor.execute(query)
            for row in cursor:
                raw=row[0].encode('utf-8');digest.update(len(raw).to_bytes(8,'big'));digest.update(raw);count+=1
        data[identifier]={'count':count,'sha256':digest.hexdigest(),'columns':[row[5] for row in catalog['columns'] if row[0]==schema and row[1]==name]}
    sequences=sequence_state(connection)
    large_hash=hashlib.sha256();pages=0
    large_oids=[row[0] for row in connection.execute('SELECT oid FROM pg_largeobject_metadata ORDER BY oid')]
    with connection.cursor(name='large_'+uuid4().hex) as cursor:
        cursor.execute("SELECT loid,pageno,encode(data,'hex') FROM pg_largeobject ORDER BY loid,pageno")
        for row in cursor:large_hash.update(canonical(row));large_hash.update(b'\n');pages+=1
    migrations=[]
    if ('public','schema_migrations') in tables:migrations=[row[0] for row in connection.execute('SELECT version FROM public.schema_migrations ORDER BY version')]
    result={'catalog_sha256':hashlib.sha256(canonical(catalog)).hexdigest(),'tables':data,'sequences':sequences,
        'large_objects':{'objects':large_oids,'pages':pages,'sha256':large_hash.hexdigest()},'migrations':migrations}
    result['sha256']=hashlib.sha256(canonical(result)).hexdigest();return result


def sequence_state(connection):
    result=[]
    for schema,name in connection.execute("SELECT schemaname,sequencename FROM pg_sequences WHERE schemaname NOT LIKE 'pg_%' ORDER BY schemaname,sequencename"):
        state=connection.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(name))).fetchone()
        result.append([schema,name,*state])
    return result


def create_quarantined_database(admin,name,properties):
    if not re.fullmatch('video_ai_restore_[a-z0-9_]{1,40}',name):raise RecoveryToolError('new_restore_database_name_required')
    with admin.connect(autocommit=True) as db:
        if db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():raise RecoveryToolError('restore_target_already_exists')
        query=sql.SQL('CREATE DATABASE {} TEMPLATE template0 ALLOW_CONNECTIONS false ENCODING {} LC_COLLATE {} LC_CTYPE {}').format(sql.Identifier(name),sql.Literal(properties['encoding']),sql.Literal(properties['collate']),sql.Literal(properties['ctype']))
        if properties['locale_provider']=='i':query+=sql.SQL(' LOCALE_PROVIDER icu ICU_LOCALE {}').format(sql.Literal(properties['icu_locale']))
        elif properties['locale_provider']!='c':raise RecoveryToolError('database_locale_provider_unsupported')
        db.execute(query)
        db.execute(sql.SQL("ALTER DATABASE {} SET video_ai.recovery_quarantine TO 'on'").format(sql.Identifier(name)))
        db.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS true').format(sql.Identifier(name)))
    # Keep template0's empty public schema: PG16 pg_dump deliberately omits
    # recreating the initdb public schema. Other schemas are created by restore.
