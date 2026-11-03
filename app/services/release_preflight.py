"""Read-only release/schema assessment. It never stops or activates a service."""
from pathlib import Path
import re
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from .pg_recovery import instance_identity
from .release_bundle import verify_release,ReleaseError,sha,canonical


def database_facts(endpoint):
    with endpoint.connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        identity=instance_identity(db)
        if int(identity[3])//10000!=16:raise ReleaseError('postgresql_16_server_required')
        def exists(name):return db.execute('SELECT to_regclass(%s) IS NOT NULL',('public.'+name,)).fetchone()[0]
        versions=[row[0] for row in db.execute('SELECT version FROM public.schema_migrations ORDER BY version')] if exists('schema_migrations') else []
        table_count=db.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p')").fetchone()[0]
        guard=db.execute('SELECT state FROM public.database_recovery_state WHERE id=1').fetchone() if exists('database_recovery_state') else None
        quarantine=db.execute("SELECT current_setting('video_ai.recovery_quarantine',true)").fetchone()[0]=='on' or bool(guard and guard[0]!='active')
        default_readonly=db.execute("SELECT current_setting('default_transaction_read_only')").fetchone()[0]=='on'
        jobs=db.execute("SELECT count(*) FROM public.jobs WHERE status IN ('queued','running')").fetchone()[0] if exists('jobs') else 0
        remote=db.execute("SELECT count(*) FROM public.provider_operations WHERE state NOT IN ('succeeded','failed','cancelled','result_received')").fetchone()[0] if exists('provider_operations') else 0
        pending=db.execute("SELECT count(*) FROM public.usage_ledger WHERE status IN ('reserved','pending_reconciliation')").fetchone()[0] if exists('usage_ledger') else 0
        tenants=db.execute('SELECT count(*) FROM public.tenants').fetchone()[0] if exists('tenants') else 0
        unowned=db.execute('SELECT count(*) FROM public.tenants t WHERE NOT EXISTS (SELECT 1 FROM public.users u WHERE u.personal_tenant_id=t.id)').fetchone()[0] if exists('users') and exists('tenants') else tenants
    return {'versions':versions,'public_tables':table_count,'quarantined':quarantine,'default_read_only':default_readonly,
        'queued_or_running_jobs':jobs,'unresolved_remote_operations':remote,'held_ledger_rows':pending,
        'tenants_without_personal_owner':unowned,'database_identity_fingerprint':sha(canonical(identity))}


def assess_release(archive_path,expected_sha256,endpoint):
    release=verify_release(archive_path,expected_sha256=expected_sha256);facts=database_facts(endpoint)
    expected=release['contracts']['database_versions'];applied=facts['versions']
    if set(applied)-set(expected):raise ReleaseError('database_version_newer_or_unknown')
    if applied!=expected[:len(applied)]:raise ReleaseError('database_migration_history_incomplete')
    if not applied and facts['public_tables']:raise ReleaseError('unversioned_database_requires_audit')
    pending=expected[len(applied):]
    return {'ok':True,'release_content_id':release['content_id'],'archive_sha256':expected_sha256,
        'database':facts,'pending_migrations':pending,'schema_assessment':'bootstrap' if not applied else 'upgrade' if pending else 'current',
        'migration_is_automatic':False,'requires_stopped_writers':bool(pending),
        'source_fencing_verified':False,'gateway_state_and_media_verified':False,
        'ready_for_production':False,'acceptance':release['contracts']['acceptance'],
        'target_must_remain_quarantined':facts['quarantined'],
        'rollback':{'database_downgrade_supported':False,'m1_image_supported_after_m2_migration':False,
            'action':'forward_fix_or_verified_snapshot_restore_with_source_fencing','compatibility_verified':False}}


def assess_runtime_file(path):
    """Parse a private dotenv file as data; never source it or echo values."""
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>1024**2:raise ReleaseError('runtime_config_file_invalid')
    values={}
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):continue
        if '=' not in line:raise ReleaseError('runtime_config_syntax_invalid')
        name,value=line.split('=',1);name=name.strip();value=value.strip()
        if not re.fullmatch('[A-Z][A-Z0-9_]*',name) or name in values:raise ReleaseError('runtime_config_key_invalid')
        if value.startswith(('\"',"'")):
            if len(value)<2 or value[-1]!=value[0]:raise ReleaseError('runtime_config_syntax_invalid')
            value=value[1:-1]
        if '${' in value or '`' in value or '$(' in value:raise ReleaseError('runtime_config_must_be_resolved')
        values[name]=value
    required=('DATABASE_URL','AUTH_PUBLIC_ORIGIN','STORAGE_GATEWAY_URL','STORAGE_GATEWAY_API_KEY',
        'STORAGE_ATTESTATION_SECRET','INTERNAL_API_SECRET','METRICS_API_SECRET',
        'STORAGE_UPLOAD_HOSTS','STORAGE_PUBLIC_HOSTS','PROVIDER_REGISTRY_PATH','SSL_CERT_FILE')
    if any(not values.get(name) for name in required):raise ReleaseError('runtime_required_values_missing')
    if any(values.get(name)!=expected for name,expected in {'APP_ENVIRONMENT':'production','AUTH_MODE':'session','PROVIDER_MODE':'api_only','ASSET_STORAGE_MODE':'external','PROVIDER_WEBHOOK_REQUIRE_REQUEST_ID':'true','DEBUG':'false'}.items()):
        raise ReleaseError('runtime_production_boundary_invalid')
    for name in ('STORAGE_GATEWAY_API_KEY','STORAGE_ATTESTATION_SECRET','INTERNAL_API_SECRET','METRICS_API_SECRET'):
        if len(values[name])<32 or values[name].lower().startswith(('replace-','change-')):raise ReleaseError('runtime_secret_unprepared')
    if len({values[name] for name in ('STORAGE_GATEWAY_API_KEY','STORAGE_ATTESTATION_SECRET','INTERNAL_API_SECRET','METRICS_API_SECRET')})!=4:
        raise ReleaseError('runtime_secrets_must_be_separate')
    for name in ('AUTH_PUBLIC_ORIGIN','STORAGE_GATEWAY_URL'):
        try:value=urlsplit(values[name]);value.port
        except ValueError:raise ReleaseError('runtime_https_origin_invalid') from None
        if value.scheme!='https' or not value.hostname or value.username or value.password or value.query or value.fragment:
            raise ReleaseError('runtime_https_origin_invalid')
        if name=='AUTH_PUBLIC_ORIGIN' and value.path:raise ReleaseError('runtime_https_origin_invalid')
    try:
        url=make_url(values['DATABASE_URL'])
        if url.drivername not in {'postgresql','postgresql+psycopg'} or not url.database:raise ValueError()
    except Exception:raise ReleaseError('runtime_postgresql_required') from None
    if values.get('DELIVERY_PACKAGE_ENABLED','false')!='false':raise ReleaseError('runtime_cloud_package_acceptance_pending')
    from ..config import Settings
    prepared={name.lower():value for name,value in values.items() if name.lower() in Settings.model_fields}
    for name in ('storage_upload_hosts','storage_public_hosts'):
        if name in prepared:prepared[name]=tuple(value.strip() for value in prepared[name].split(',') if value.strip())
    try:Settings(**prepared)
    except Exception:raise ReleaseError('runtime_application_settings_invalid') from None
    return {'ok':True,'production_boundaries_valid':True,'config_values_disclosed':False,
        'database_connection_verified':False,'dns_tls_and_domains_verified':False,
        'provider_acceptance_verified':False,'ready_for_production':False}
