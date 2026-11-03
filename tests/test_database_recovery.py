"""Authenticated metadata archives must fail before any restore side effect."""
import base64
import hashlib
import io
import json
import os
import tarfile
from uuid import uuid4

import pytest
from app.services import database_recovery as recovery
from app.services.pg_recovery import canonical
from app.services.recovery_bundle import (
    RecoveryToolError,decrypt_file,encrypt_file,private_scratch,
)


def fixture_archive(tmp_path,key,*,member=None,wrong_hash=False):
    dump=b'Explicit non-PG archive fixture, never passed to pg_restore.'
    state={'tables':{},'migrations':[]}
    state['sha256']=hashlib.sha256(canonical(state)).hexdigest()
    manifest={'format':recovery.FORMAT,'postgresql_major':16,'backup_id':str(uuid4()),
        'snapshot_observed_at':'2026-10-18T00:00:00+00:00',
        'snapshot':state,'database_properties':{'encoding':'UTF8','collate':'C','ctype':'C','locale_provider':'c','icu_locale':None},
        'files':{'metadata.dump':{'size':len(dump),'sha256':'0'*64 if wrong_hash else hashlib.sha256(dump).hexdigest()}}}
    tar=tmp_path/(uuid4().hex+'.tar')
    with tarfile.open(tar,'w') as archive:
        for name,raw in [('manifest.json',json.dumps(manifest).encode()),('metadata.dump',dump)]:
            info=tarfile.TarInfo(name);info.size=len(raw);archive.addfile(info,io.BytesIO(raw))
        if member:
            info=tarfile.TarInfo(member);info.size=1;archive.addfile(info,io.BytesIO(b'x'))
    envelope=tmp_path/(uuid4().hex+'.vab');encrypt_file(tar,envelope,key)
    return envelope,manifest


class NoDatabase:
    def connect(self,**kwargs):raise AssertionError('unauthenticated input reached PostgreSQL')


@pytest.mark.parametrize('damage',['wrong_key','tag','header','truncated'])
def test_authentication_failure_never_connects_or_exports(tmp_path,damage):
    key=os.urandom(32);envelope,_=fixture_archive(tmp_path,key)
    if damage=='wrong_key':key=os.urandom(32)
    else:
        raw=bytearray(envelope.read_bytes())
        if damage=='truncated':raw=raw[:len(raw)//2]
        else:raw[-1 if damage=='tag' else 0]^=1
        envelope.write_bytes(raw)
    output=tmp_path/'restore'
    with pytest.raises(RecoveryToolError):
        recovery.restore_database(envelope,key,NoDatabase(),None,database='video_ai_restore_invalid',output_directory=output,scratch_root=tmp_path,export_configs=True)
    assert not output.exists() and not list(tmp_path.glob('video-ai-recovery-*'))


@pytest.mark.parametrize('member',['../outside','config/../../outside','/absolute','metadata.dump'])
def test_authenticated_archive_rejects_unexpected_traversal_and_duplicate_members(tmp_path,member):
    key=os.urandom(32);envelope,_=fixture_archive(tmp_path,key,member=member)
    with pytest.raises(RecoveryToolError):
        recovery.restore_database(envelope,key,NoDatabase(),None,database='video_ai_restore_invalid',output_directory=tmp_path/'restore',scratch_root=tmp_path)
    assert not list(tmp_path.glob('video-ai-recovery-*'))


def test_member_digest_is_verified_before_database_action(tmp_path):
    key=os.urandom(32);envelope,_=fixture_archive(tmp_path,key,wrong_hash=True)
    with pytest.raises(RecoveryToolError,match='checksum'):
        recovery.restore_database(envelope,key,NoDatabase(),None,database='video_ai_restore_invalid',output_directory=tmp_path/'restore',scratch_root=tmp_path)


def test_rotation_authenticates_old_key_preserves_archive_and_never_overwrites(tmp_path):
    old=os.urandom(32);new=os.urandom(32);envelope,manifest=fixture_archive(tmp_path,old)
    receipt=recovery.rewrap_backup(envelope,old,new,output_directory=tmp_path/'rotated',scratch_root=tmp_path)
    rotated=tmp_path/'rotated'/'metadata.vab'
    assert receipt['backup_id']==manifest['backup_id'] and receipt['operation']=='rewrap'
    assert envelope.read_bytes()!=rotated.read_bytes()
    assert recovery.inspect_backup(rotated,new,scratch_root=tmp_path)['authenticated']
    with pytest.raises(RecoveryToolError,match='authentication'):
        recovery.inspect_backup(rotated,old,scratch_root=tmp_path)
    with pytest.raises(RecoveryToolError,match='new_private_directory'):
        recovery.rewrap_backup(envelope,old,new,output_directory=tmp_path/'rotated',scratch_root=tmp_path)
    assert recovery.inspect_backup(envelope,old,scratch_root=tmp_path)['backup_id']==receipt['backup_id']


def test_private_scratch_permissions_cleanup_and_keygen_do_not_print_key(tmp_path,capsys):
    with private_scratch(tmp_path) as scratch:
        (scratch/'test').write_bytes(b'explicit scratch fixture')
        if os.name!='nt':assert scratch.stat().st_mode&0o077==0
    assert not scratch.exists()
    assert recovery.main(['keygen','--output-directory',str(tmp_path/'key')])==0
    captured=capsys.readouterr().out;key=(tmp_path/'key'/'backup.key').read_text().strip()
    assert len(base64.b64decode(key))==32 and key not in captured
    assert recovery.main(['keygen','--output-directory',str(tmp_path/'key')])==1
    assert (tmp_path/'key'/'backup.key').read_text().strip()==key


def test_cli_errors_never_echo_connection_values(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('RECOVERY_TEST_KEY',base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv('RECOVERY_TEST_DATABASE','postgresql+psycopg://fixture:PRIVATE_SENTINEL@127.0.0.1:1/db?options=PRIVATE_SENTINEL')
    assert recovery.main(['backup','--database-env','RECOVERY_TEST_DATABASE','--key-env','RECOVERY_TEST_KEY','--output-directory',str(tmp_path/'out')])==1
    raw=capsys.readouterr().out
    assert 'PRIVATE_SENTINEL' not in raw and 'postgresql' not in raw.replace('postgresql_endpoint_invalid','')
    assert json.loads(raw)['code']=='postgresql_endpoint_invalid'


def test_truncated_plaintext_is_not_released_as_valid_tar(tmp_path):
    key=os.urandom(32);envelope,_=fixture_archive(tmp_path,key)
    envelope.write_bytes(envelope.read_bytes()[:-1])
    with private_scratch(tmp_path) as scratch:
        with pytest.raises(RecoveryToolError):decrypt_file(envelope,scratch/'raw',key)
    assert not list(tmp_path.glob('video-ai-recovery-*'))
