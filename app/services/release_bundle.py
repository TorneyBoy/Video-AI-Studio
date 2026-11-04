"""Reproducible review-source bundles. No build, migration or deployment runs.

An externally retained SHA256 is required for verification/extraction. This
is content integrity, not a publisher signature or a product acceptance claim.
"""
import ast
import gzip
import hashlib
import io
import json
import os
from pathlib import Path,PurePosixPath
import re
import stat
import subprocess
import tarfile
import tomllib
from urllib.parse import urlsplit

from .recovery_bundle import private_directory,private_file,write_json

FORMAT='video-ai-source-release/1'
MAX_FILE=16*1024**2
MAX_SOURCE=64*1024**2
MAX_ARCHIVE=80*1024**2
MAX_FILES=5000
MANIFEST='release-manifest.json'
ROOT_FILES=frozenset('''
.dockerignore .gitignore .env.example .env.gateway.example .env.m2.example
Dockerfile Makefile README.md pyproject.toml requirements.txt requirements-dev.lock
index.html app.js styles.css login.html auth.js auth.css share.html share.js share.css
package.json package-lock.json
'''.split())
CONFIG_FILES=frozenset('''
aliyun-analysis-client-policy.json aliyun-analysis-client-policy.template.json
aliyun-ecs-role-trust.json aliyun-ice-client-policy.json aliyun-ice-role-trust.json
aliyun-ice-storage-policy.json aliyun-ice-storage-policy.proposed-v2.json
aliyun-storage-policy.json provider-smoke.example.json providers.aliyun.m1.json providers.aliyun.ai.example.json
providers.contract.fixture.json providers.example.json scenarios.example.json scenario-benchmark.example.json
providers.multimodel.example.json
'''.split())
EXTENSIONS={
    'app':{'.py'},'media_gateway':{'.py'},'timeline_engine':{'.py'},
    'scripts':{'.py','.sh','.cjs'},'infra':{'.yml','.yaml'},'config':{'.json'},
    'docs':{'.md'},'web':{'.js','.json'},'third_party':{'.txt','.json'},
    'miniprogram':{'.json','.md','.ts','.wxml','.wxss'},
    'cloud_functions':{'.py','.json','.txt'},'tests':{'.py','.cjs','.json'},
    'migrations':{'.sql'},'.github':{'.yml','.yaml'},
}
IGNORED_PARTS=frozenset({'.git','.codex','.venv','.pytest_cache','.ruff_cache',
    '__pycache__','node_modules','data','secrets','credentials','keys','.idea','.vscode'})
IGNORED_SUFFIXES=frozenset({'.pyc','.pyo','.pem','.key','.p12','.pfx','.vab','.dump',
    '.db','.sqlite3','.zip','.gz','.tar','.mp4','.mov','.wav','.mp3','.log'})


class ReleaseError(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()


def ignored(name):
    path=PurePosixPath(name)
    if any(part in IGNORED_PARTS or part.endswith('.egg-info') for part in path.parts):return True
    if path.name=='project.private.config.json':return True
    if path.name.startswith('.env') and name not in ROOT_FILES:return True
    return path.suffix.lower() in IGNORED_SUFFIXES


def admitted(name):
    path=PurePosixPath(name)
    if not name or path.is_absolute() or '\\' in name or any(part in {'','..','.'} for part in name.split('/')):
        return False
    if ignored(name):return False
    if name in ROOT_FILES or name=='infra/Dockerfile.control':return True
    if name in {'miniprogram/src/vendor/sha256.js','miniprogram/src/vendor/batch-form.js','miniprogram/src/vendor/storyboard-form.js','miniprogram/src/vendor/shot-matching-form.js','miniprogram/src/vendor/ai-tools-form.js','miniprogram/src/vendor/scenario-review-form.js'}:return True
    if path.parts[0]=='config' and (len(path.parts)!=2 or path.name not in CONFIG_FILES):return False
    if path.parts[0]=='.github' and name!='.github/workflows/ci.yml':return False
    return len(path.parts)>1 and path.suffix in EXTENSIONS.get(path.parts[0],set())


def inventory(root,*,allow_manifest=False):
    root=Path(root).resolve(strict=True);files={}
    def visit(directory):
        for path in sorted(directory.iterdir()):
            name=path.relative_to(root).as_posix()
            if allow_manifest and name==MANIFEST:continue
            if ignored(name):continue
            if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise ReleaseError('source_link_rejected')
            if not path.resolve().is_relative_to(root):raise ReleaseError('source_outside_root')
            if path.is_dir():
                if directory==root and path.name not in EXTENSIONS:raise ReleaseError('undeclared_source_directory')
                visit(path)
            elif not path.is_file() or not admitted(name):raise ReleaseError('undeclared_source_file')
            else:files[name]=path
    visit(root)
    if len(files)>MAX_FILES:raise ReleaseError('source_file_count_limit')
    return files


def _read_stable(path):
    before=path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_FILE:raise ReleaseError('source_file_size_or_type')
    with path.open('rb') as stream:
        opened=os.fstat(stream.fileno());raw=stream.read(MAX_FILE+1)
    after=path.stat()
    def signature(row):return (row.st_dev,row.st_ino,row.st_size,row.st_mtime_ns)
    if signature(before)!=signature(opened) or signature(before)!=signature(after) or len(raw)!=before.st_size:
        raise ReleaseError('source_changed_during_capture')
    return raw,signature(after)


def _check_public_configuration(name,raw):
    if name.startswith('.env'):
        for line in raw.decode('utf-8').splitlines():
            if line.strip().startswith('#') or '=' not in line:continue
            key,value=line.split('=',1);value=value.strip().strip('\"\'')
            if key.strip().endswith('DATABASE_URL') and value and urlsplit(value).password:
                raise ReleaseError('public_template_contains_credential')
            if not re.search(r'(PASSWORD|SECRET|API_KEY|PRIVATE_KEY|ACCESS_KEY|ACCESS_KEY_ID|SESSION_TOKEN)$',key.strip()):continue
            if value and value not in {'video_ai_dev','video_ai_dev_secret'} and not value.lower().startswith(('replace-','change-','${')):
                raise ReleaseError('public_template_contains_credential')
    if name.startswith('config/'):
        def visit(value,env=False):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key.lower().endswith('_env'):continue
                    if not env and re.fullmatch(r'(password|appsecret|access_key_secret|secret_access_key|client_secret|private_key|authorization|x-api-key)',key.lower()) and item:
                        raise ReleaseError('public_config_contains_literal_credential')
                    visit(item,env)
            elif isinstance(value,list):
                for item in value:visit(item,env)
        visit(json.loads(raw))


def contracts(files):
    missing=ROOT_FILES-set(files)
    if missing:raise ReleaseError('required_root_source_missing')
    for name in ('app/__init__.py','app/main.py','app/cli.py','app/migrations.py','app/models.py','media_gateway/main.py',
        'infra/Dockerfile.control','infra/m2.compose.yml','scripts/quality_gate.py',
        'scripts/database_recovery.py','scripts/package_release.py','scripts/release_preflight.py',
        'web/manifest.json','third_party/client-deps.json',
        'miniprogram/project.config.json','miniprogram/package.json','miniprogram/package-lock.json','miniprogram/tsconfig.json',
        'miniprogram/src/app.ts','miniprogram/src/app.json','miniprogram/src/app.wxss','miniprogram/src/config.ts','miniprogram/src/sitemap.json',
        'cloud_functions/shot_detection/index.py','cloud_functions/shot_detection/requirements.txt',
        '.github/workflows/ci.yml','tests/test_release_bundle.py'):
        if name not in files:raise ReleaseError('required_component_missing')
    tree=ast.parse(files['app/migrations.py'].decode('utf-8'))
    definitions=[node.value for node in tree.body if isinstance(node,(ast.Assign,ast.AnnAssign))
        and any(isinstance(target,ast.Name) and target.id=='MIGRATIONS' for target in (node.targets if isinstance(node,ast.Assign) else [node.target]))]
    if len(definitions)!=1 or not isinstance(definitions[0],ast.Tuple):raise ReleaseError('migration_declaration_invalid')
    versions=[]
    for call in definitions[0].elts:
        if not isinstance(call,ast.Call):raise ReleaseError('migration_declaration_invalid')
        values=[keyword.value for keyword in call.keywords if keyword.arg=='version']
        if len(values)!=1 or not isinstance(values[0],ast.Constant) or not isinstance(values[0].value,str):raise ReleaseError('migration_declaration_invalid')
        versions.append(values[0].value)
    if any(not re.fullmatch(r'\d{4}_[a-z0-9_]+',value) for value in versions) or [int(value[:4]) for value in versions]!=list(range(1,len(versions)+1)):
        raise ReleaseError('migration_sequence_invalid')
    web=json.loads(files['web/manifest.json'])['scripts']
    if len(web)!=len(set(web)) or any('web/'+name not in files or not admitted('web/'+name) for name in web):raise ReleaseError('web_entry_missing')
    native=json.loads(files['miniprogram/src/app.json']);pages=native['pages']
    if len(pages)!=len(set(pages)):raise ReleaseError('native_page_duplicate')
    for page in pages:
        if not re.fullmatch(r'pages/[a-z0-9-]+/index',page):raise ReleaseError('native_page_invalid')
        for suffix in ('.ts','.wxml','.json'):
            if 'miniprogram/src/'+page+suffix not in files:raise ReleaseError('native_page_missing')
    dependencies=json.loads(files['third_party/client-deps.json'])['files']
    for name,expected in dependencies.items():
        if name not in files or sha(files[name])!=expected:raise ReleaseError('vendored_dependency_hash_mismatch')
    for name,raw in files.items():_check_public_configuration(name,raw)
    project=tomllib.loads(files['pyproject.toml'].decode('utf-8'))['project']
    native_project=json.loads(files['miniprogram/project.config.json'])
    return {'project_version':project['version'],'database_versions':['0000_baseline',*versions],
        'target_database_version':versions[-1],'schema_downgrade_supported':False,
        'execution_fence':'database-instance/1','web_scripts':len(web),'native_pages':len(pages),
        'wechat_appid_configured':bool(native_project.get('appid')),
        'acceptance':{'m2_stage':'not_attested','real_cloud':'not_attested','wechat_device':'not_attested','production':'not_deployed'}}


def _git_provenance(root):
    try:
        result=subprocess.run(['git','-C',str(root),'rev-parse','--verify','HEAD'],capture_output=True,text=True,check=False,timeout=5)
        commit=result.stdout.strip()
        if result.returncode or not re.fullmatch('[0-9a-f]{40,64}',commit):return None
        dirty=subprocess.run(['git','-C',str(root),'status','--porcelain=v1','--untracked-files=normal'],capture_output=True,check=False,timeout=5)
        return {'head':commit,'worktree_dirty':bool(dirty.stdout) if not dirty.returncode else None}
    except (OSError,subprocess.TimeoutExpired):return None


def _mode(name):return 0o755 if name.endswith('.sh') else 0o644


def build_release(root,output_directory,*,label):
    if not re.fullmatch('[a-z0-9][a-z0-9._-]{0,79}',label):raise ReleaseError('release_label_invalid')
    root=Path(root).resolve(strict=True);destination=Path(output_directory).absolute()
    if destination.exists() or destination.is_symlink():raise ReleaseError('new_output_directory_required')
    if destination.is_relative_to(root) and destination.relative_to(root).parts[0] in EXTENSIONS:
        raise ReleaseError('release_output_inside_source_component')
    candidates=inventory(root);files={};signatures={};total=0
    for name,path in candidates.items():
        raw,signature=_read_stable(path);files[name]=raw;signatures[name]=signature;total+=len(raw)
        if total>MAX_SOURCE:raise ReleaseError('source_total_size_limit')
    metadata=contracts(files)
    if set(inventory(root))!=set(candidates):raise ReleaseError('source_inventory_changed')
    for name,path in candidates.items():
        row=path.stat()
        if (row.st_dev,row.st_ino,row.st_size,row.st_mtime_ns)!=signatures[name]:raise ReleaseError('source_changed_during_capture')
    manifest={'format':FORMAT,'label':label,'git':_git_provenance(root),'contracts':metadata,
        'files':{name:{'size':len(raw),'sha256':sha(raw),'mode':_mode(name)} for name,raw in sorted(files.items())}}
    manifest['content_id']=sha(canonical(manifest));raw_manifest=canonical(manifest)+b'\n'
    if len(raw_manifest)>2*1024**2:raise ReleaseError('release_manifest_size_limit')
    output=private_directory(destination);partial=output/'release.tar.gz.partial';archive_path=output/'release.tar.gz'
    with private_file(partial) as stream:
        with gzip.GzipFile(filename='',mode='wb',fileobj=stream,mtime=0) as compressed,tarfile.open(fileobj=compressed,mode='w',format=tarfile.USTAR_FORMAT) as archive:
            for name,raw in {MANIFEST:raw_manifest,**dict(sorted(files.items()))}.items():
                info=tarfile.TarInfo(name);info.size=len(raw);info.mode=_mode(name);info.uid=0;info.gid=0;info.mtime=0
                archive.addfile(info,io.BytesIO(raw))
        stream.flush();os.fsync(stream.fileno())
    partial.rename(archive_path);digest=sha(archive_path.read_bytes())
    verified=verify_release(archive_path,expected_sha256=digest)
    receipt={'ok':True,'format':FORMAT,'label':label,'artifact':'release.tar.gz','sha256':digest,
        'content_id':manifest['content_id'],'source_files':len(files),'source_bytes':total,
        'contracts':metadata,'git':manifest['git'],'stage_validation_run':False,
        'archive_integrity_verified':verified['ok']}
    write_json(output/'receipt.json',receipt);return receipt


def _read_release(archive_path,expected_sha256):
    if not re.fullmatch('[0-9a-f]{64}',expected_sha256):raise ReleaseError('trusted_archive_sha256_required')
    path=Path(archive_path)
    if not path.is_file() or path.stat().st_size>MAX_ARCHIVE:raise ReleaseError('archive_size_limit')
    compressed=path.read_bytes()
    if sha(compressed)!=expected_sha256:raise ReleaseError('archive_sha256_mismatch')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed),mode='rb') as stream:tar=stream.read(MAX_ARCHIVE+1)
        if len(tar)>MAX_ARCHIVE:raise ReleaseError('archive_expanded_size_limit')
        files={};manifest=None;total=0
        with tarfile.open(fileobj=io.BytesIO(tar),mode='r:') as archive:
            for index,member in enumerate(archive):
                if index>MAX_FILES or not member.isfile() or member.sparse is not None or member.uid or member.gid or member.mtime:
                    raise ReleaseError('archive_member_invalid')
                name=member.name
                if index==0:
                    if name!=MANIFEST or not 0<member.size<=2*1024**2:raise ReleaseError('manifest_missing')
                    manifest=json.loads(archive.extractfile(member).read());continue
                if not admitted(name) or name in files or not 0<=member.size<=MAX_FILE or member.mode!=_mode(name):raise ReleaseError('archive_member_invalid')
                total+=member.size
                if total>MAX_SOURCE:raise ReleaseError('source_total_size_limit')
                raw=archive.extractfile(member).read()
                if len(raw)!=member.size:raise ReleaseError('archive_member_truncated')
                files[name]=raw
        if manifest is None or manifest.get('format')!=FORMAT:raise ReleaseError('manifest_invalid')
        declared={name:{'size':len(raw),'sha256':sha(raw),'mode':_mode(name)} for name,raw in sorted(files.items())}
        if declared!=manifest['files'] or contracts(files)!=manifest['contracts']:raise ReleaseError('manifest_contract_mismatch')
        if sha(canonical({key:value for key,value in manifest.items() if key!='content_id'}))!=manifest['content_id']:raise ReleaseError('manifest_content_id_mismatch')
        return manifest,files
    except (OSError,EOFError,ValueError,KeyError,TypeError,AttributeError,tarfile.TarError):raise ReleaseError('release_archive_invalid') from None


def verify_release(archive_path,*,expected_sha256):
    manifest,files=_read_release(archive_path,expected_sha256)
    return {'ok':True,'sha256':expected_sha256,'content_id':manifest['content_id'],
        'source_files':len(files),'contracts':manifest['contracts'],'stage_validation_run':False}


def extract_release(archive_path,*,expected_sha256,destination):
    manifest,files=_read_release(archive_path,expected_sha256)
    output=private_directory(destination)
    for name,raw in files.items():
        path=output/name;path.parent.mkdir(parents=True,exist_ok=True)
        with private_file(path) as stream:stream.write(raw)
        path.chmod(_mode(name))
    write_json(output/MANIFEST,manifest)
    return {'ok':True,'content_id':manifest['content_id'],'source_files':len(files),'extracted':True,'deployed':False}


def verify_tree(root,*,expected_content_id):
    root=Path(root).resolve(strict=True)
    manifest_path=root/MANIFEST
    if manifest_path.is_symlink():raise ReleaseError('source_link_rejected')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if not re.fullmatch('[0-9a-f]{64}',expected_content_id) or manifest.get('content_id')!=expected_content_id:
        raise ReleaseError('trusted_content_id_mismatch')
    if set(inventory(root,allow_manifest=True))!=set(manifest['files']):raise ReleaseError('source_inventory_changed')
    files={}
    # Inventory's root manifest is a transport artifact, not executable source.
    for name,entry in manifest['files'].items():
        if not admitted(name):raise ReleaseError('manifest_file_invalid')
        path=root/name
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ReleaseError('source_link_rejected')
        raw,_=_read_stable(path)
        if {'size':len(raw),'sha256':sha(raw),'mode':_mode(name)}!=entry:raise ReleaseError('source_file_hash_mismatch')
        files[name]=raw
    if contracts(files)!=manifest['contracts'] or sha(canonical({key:value for key,value in manifest.items() if key!='content_id'}))!=manifest['content_id']:
        raise ReleaseError('manifest_contract_mismatch')
    return {'ok':True,'content_id':manifest['content_id'],'contracts':manifest['contracts'],'source_files':len(files),'stage_validation_run':False}
