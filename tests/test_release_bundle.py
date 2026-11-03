"""Complete source candidates, reproducibility, integrity and private exclusions."""
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest
from app.services import release_bundle as release
from app.services.release_bundle import ReleaseError
from app.services.release_preflight import assess_runtime_file


@pytest.fixture(scope='module')
def release_source_base(tmp_path_factory):
    source=Path(__file__).resolve().parents[1];root=tmp_path_factory.mktemp('release-source')
    for name,path in release.inventory(source).items():
        target=root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(path.read_bytes())
    return root


@pytest.fixture
def release_source(release_source_base,tmp_path):
    return shutil.copytree(release_source_base,tmp_path/'source')


def test_complete_candidate_is_reproducible_and_extracts_both_clients(release_source,tmp_path):
    sentinel=os.urandom(32)
    for name in ('.env.m2','miniprogram/project.private.config.json','config/backup.key','data/state.db','miniprogram/node_modules/not-shipped/index.js'):
        path=release_source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(sentinel)
    first=release.build_release(release_source,tmp_path/'first',label='m2-review')
    for path in release.inventory(release_source).values():os.utime(path,(123456789,123456789))
    second=release.build_release(release_source,tmp_path/'second',label='m2-review')
    assert first['sha256']==second['sha256'] and first['content_id']==second['content_id']
    archive=tmp_path/'first'/'release.tar.gz'
    assert sentinel not in gzip.decompress(archive.read_bytes())
    contracts=first['contracts'];assert contracts['native_pages']==15 and contracts['web_scripts']==25
    assert contracts['target_database_version']=='0023_shot_matching'
    assert contracts['wechat_appid_configured']==bool(json.loads((release_source/'miniprogram/project.config.json').read_bytes()).get('appid'))
    assert not first['stage_validation_run']
    assert contracts['acceptance']=={'m2_stage':'not_attested','real_cloud':'not_attested','wechat_device':'not_attested','production':'not_deployed'}
    extracted=tmp_path/'extracted'
    release.extract_release(archive,expected_sha256=first['sha256'],destination=extracted)
    assert (extracted/'Dockerfile').is_file() and (extracted/'infra/Dockerfile.control').is_file()
    assert (extracted/'tests/test_postgres_recovery.py').is_file() and (extracted/'miniprogram/src/pages/batch-delivery/index.ts').is_file()
    assert release.verify_tree(extracted,expected_content_id=first['content_id'])['ok']
    with pytest.raises(ReleaseError,match='new_output_directory'):release.build_release(release_source,tmp_path/'first',label='m2-review')


@pytest.mark.parametrize('damage',['bytes','wrong_hash','truncated'])
def test_external_checksum_failure_creates_no_destination(release_source,tmp_path,damage):
    value=release.build_release(release_source,tmp_path/'candidate',label='checksum-test');path=tmp_path/'candidate'/'release.tar.gz'
    expected=value['sha256']
    if damage=='wrong_hash':expected='0'*64
    else:
        raw=bytearray(path.read_bytes())
        if damage=='bytes':raw[-1]^=1
        else:raw=raw[:len(raw)//2]
        path.write_bytes(raw)
    with pytest.raises(ReleaseError,match='sha256_mismatch'):
        release.extract_release(path,expected_sha256=expected,destination=tmp_path/'must-not-exist')
    assert not (tmp_path/'must-not-exist').exists()


@pytest.mark.parametrize('member,kind',[('../outside','file'),('/absolute','file'),('app/main.py','duplicate'),('app/link.py','symlink')])
def test_archive_structure_is_checked_even_with_matching_transport_checksum(release_source,tmp_path,member,kind):
    release.build_release(release_source,tmp_path/'candidate',label='malformed-test')
    original=tmp_path/'candidate'/'release.tar.gz';buffer=io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(original.read_bytes())),mode='r:') as source,tarfile.open(fileobj=buffer,mode='w',format=tarfile.USTAR_FORMAT) as archive:
        for info in source:archive.addfile(info,source.extractfile(info))
        extra=tarfile.TarInfo(member);extra.mode=0o644
        if kind=='symlink':extra.type=tarfile.SYMTYPE;extra.linkname='../outside';archive.addfile(extra)
        else:extra.size=1;archive.addfile(extra,io.BytesIO(b'x'))
    altered=tmp_path/'altered.gz';altered.write_bytes(gzip.compress(buffer.getvalue(),mtime=0))
    with pytest.raises(ReleaseError):
        release.extract_release(altered,expected_sha256=release.sha(altered.read_bytes()),destination=tmp_path/'must-not-exist')
    assert not (tmp_path/'must-not-exist').exists()


@pytest.mark.parametrize('change',['missing_native','vendor_changed','private_template','undeclared_config'])
def test_incomplete_or_unreviewed_sources_are_not_published(release_source,tmp_path,change):
    if change=='missing_native':(release_source/'miniprogram/src/pages/studio/index.ts').unlink()
    elif change=='vendor_changed':
        path=release_source/'web/vendor-sha256.js';path.write_bytes(path.read_bytes()+b' ')
    elif change=='private_template':
        with (release_source/'.env.m2.example').open('a') as stream:stream.write('\nSTORAGE_GATEWAY_API_KEY=PRIVATE_TEMPLATE_SENTINEL\n')
    else:(release_source/'config/unreviewed.json').write_text('{}')
    with pytest.raises(ReleaseError):release.build_release(release_source,tmp_path/'must-not-exist',label='rejected')
    assert not (tmp_path/'must-not-exist').exists()


def test_capture_refuses_concurrent_source_edit(release_source,tmp_path,monkeypatch):
    original=release._read_stable;changed=False
    def changing(path):
        nonlocal changed
        value=original(path)
        if not changed:changed=True;path.write_bytes(value[0]+b'\n')
        return value
    monkeypatch.setattr(release,'_read_stable',changing)
    with pytest.raises(ReleaseError,match='source_changed'):release.build_release(release_source,tmp_path/'must-not-exist',label='edited')
    assert not (tmp_path/'must-not-exist').exists()


def test_extracted_tree_must_match_trusted_id_and_rejects_extra_executable_source(release_source,tmp_path):
    value=release.build_release(release_source,tmp_path/'candidate',label='tree-test');path=tmp_path/'candidate'/'release.tar.gz'
    tree=tmp_path/'tree';release.extract_release(path,expected_sha256=value['sha256'],destination=tree)
    with pytest.raises(ReleaseError,match='trusted_content_id'):release.verify_tree(tree,expected_content_id='0'*64)
    (tree/'app/extra.py').write_text('raise RuntimeError()')
    with pytest.raises(ReleaseError,match='inventory_changed'):release.verify_tree(tree,expected_content_id=value['content_id'])
    (tree/'app/extra.py').unlink();(tree/'app/main.py').write_text('raise RuntimeError()')
    with pytest.raises(ReleaseError,match='hash_mismatch'):release.verify_tree(tree,expected_content_id=value['content_id'])


def runtime_file(tmp_path,**changes):
    values={'APP_ENVIRONMENT':'production','AUTH_MODE':'session','AUTH_PUBLIC_ORIGIN':'https://fixture.invalid',
        'DATABASE_URL':'postgresql+psycopg://fixture:TEST_ONLY_PRIVATE_PASSWORD@127.0.0.1/test',
        'PROVIDER_MODE':'api_only','ASSET_STORAGE_MODE':'external','PROVIDER_WEBHOOK_REQUIRE_REQUEST_ID':'true','DEBUG':'false',
        'STORAGE_GATEWAY_URL':'https://internal.fixture.invalid:8100','STORAGE_GATEWAY_API_KEY':'g'*40,
        'STORAGE_ATTESTATION_SECRET':'a'*40,'INTERNAL_API_SECRET':'i'*40,'METRICS_API_SECRET':'m'*40,
        'STORAGE_UPLOAD_HOSTS':'fixture.invalid','STORAGE_PUBLIC_HOSTS':'fixture.invalid',
        'PROVIDER_REGISTRY_PATH':'/run/video-ai/providers.json','SSL_CERT_FILE':'/run/video-ai/ca.pem','DELIVERY_PACKAGE_ENABLED':'false',**changes}
    path=tmp_path/'private.env';path.write_text('\n'.join(name+'='+value for name,value in values.items()),encoding='utf-8');return path


@pytest.mark.parametrize('changes',[{'AUTH_MODE':'trusted_proxy'},{'METRICS_API_SECRET':'i'*40},{'DELIVERY_PACKAGE_ENABLED':'true'},{'AUTH_PUBLIC_ORIGIN':'http://fixture.invalid'},{'DATABASE_URL':'$(do-not-run)'}])
def test_runtime_config_rejects_unsafe_m2_boundaries_without_execution(tmp_path,changes):
    with pytest.raises(ReleaseError):assess_runtime_file(runtime_file(tmp_path,**changes))


def test_runtime_config_does_not_disclose_values_or_claim_deployment(tmp_path):
    result=assess_runtime_file(runtime_file(tmp_path))
    assert result['production_boundaries_valid'] and not result['ready_for_production']
    assert 'TEST_ONLY_PRIVATE_PASSWORD' not in json.dumps(result) and 'g'*40 not in json.dumps(result)


def test_retired_m1_entry_cannot_silently_package_current_m2_sources(release_source,tmp_path):
    output=tmp_path/'old-m1.tar.gz'
    result=subprocess.run([sys.executable,str(release_source/'scripts/package_m1_release.py'),'--output',str(output)],capture_output=True,text=True,check=False)
    assert result.returncode==2 and 'scripts/package_release.py' in result.stderr
    assert not output.exists()
