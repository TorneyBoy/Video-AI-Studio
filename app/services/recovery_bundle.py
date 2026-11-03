"""Private scratch files and an authenticated, streaming AES-256-GCM envelope.

Decryption must finish authentication before callers may inspect the TAR.
The key is 32 random bytes, never a human passphrase or an archive member.
"""
from contextlib import contextmanager
import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from uuid import uuid4

MAGIC=b'VASTUDIO-BACKUP1\n'
BLOCK=64*1024
MAX_BUNDLE=32*1024**3


class RecoveryToolError(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)


def digest_file(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block:=stream.read(BLOCK):result.update(block)
    return result.hexdigest()


def load_key(name):
    if not re.fullmatch('[A-Z][A-Z0-9_]{0,100}',name):raise RecoveryToolError('key_environment_name_invalid')
    try:key=base64.b64decode(os.environ.get(name,''),validate=True)
    except (ValueError,TypeError):raise RecoveryToolError('backup_key_invalid') from None
    if len(key)!=32:raise RecoveryToolError('backup_key_must_be_32_random_bytes')
    return key


def private_directory(path):
    path=Path(path).absolute()
    if not path.parent.is_dir() or path.exists() or path.is_symlink():raise RecoveryToolError('new_private_directory_required')
    path.mkdir(mode=0o700)
    if os.name=='nt':
        identity=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=False)
        try:sid=next(csv.reader(identity.stdout.splitlines()))[-1]
        except (StopIteration,IndexError):sid=''
        if not re.fullmatch('S-1-[0-9-]+',sid):path.rmdir();raise RecoveryToolError('private_directory_identity_unavailable')
        result=subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F'],capture_output=True,check=False)
        if result.returncode:path.rmdir();raise RecoveryToolError('private_directory_acl_failed')
    return path.resolve()


@contextmanager
def private_scratch(root):
    root=Path(root).resolve(strict=True)
    if not root.is_dir():raise RecoveryToolError('scratch_root_required')
    path=private_directory(root/('video-ai-recovery-'+uuid4().hex))
    try:yield path
    finally:
        # This is the only recursive deletion: the newly created private child.
        if path.exists():
            if path.is_symlink() or path.resolve().parent!=root or not path.name.startswith('video-ai-recovery-'):
                raise RecoveryToolError('scratch_cleanup_scope_changed')
            shutil.rmtree(path)


def private_file(path,mode='wb'):
    flags=os.O_CREAT|os.O_EXCL|os.O_WRONLY
    descriptor=os.open(path,flags,0o600)
    return os.fdopen(descriptor,mode)


def write_json(path,value):
    with private_file(path) as stream:
        stream.write((json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode());stream.flush();os.fsync(stream.fileno())


def encrypt_file(source,target,key):
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    size=Path(source).stat().st_size
    if not 1<=size<=MAX_BUNDLE:raise RecoveryToolError('backup_bundle_size_limit')
    nonce=os.urandom(12);header=MAGIC+nonce;encryptor=Cipher(algorithms.AES256(key),modes.GCM(nonce)).encryptor();encryptor.authenticate_additional_data(header)
    with Path(source).open('rb') as raw,private_file(target) as encrypted:
        encrypted.write(header)
        while block:=raw.read(BLOCK):encrypted.write(encryptor.update(block))
        encrypted.write(encryptor.finalize());encrypted.write(encryptor.tag);encrypted.flush();os.fsync(encrypted.fileno())


def decrypt_file(source,target,key):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    size=Path(source).stat().st_size;header_size=len(MAGIC)+12
    if not header_size+16<size<=MAX_BUNDLE+header_size+16:raise RecoveryToolError('backup_envelope_size_invalid')
    with Path(source).open('rb') as encrypted:
        header=encrypted.read(header_size)
        if header[:len(MAGIC)]!=MAGIC:raise RecoveryToolError('backup_envelope_invalid')
        encrypted.seek(-16,2);tag=encrypted.read(16);encrypted.seek(header_size)
        decryptor=Cipher(algorithms.AES256(key),modes.GCM(header[len(MAGIC):],tag)).decryptor();decryptor.authenticate_additional_data(header)
        remaining=size-header_size-16
        try:
            with private_file(target) as raw:
                while remaining:
                    block=encrypted.read(min(BLOCK,remaining))
                    if not block:raise RecoveryToolError('backup_envelope_truncated')
                    remaining-=len(block);raw.write(decryptor.update(block))
                raw.write(decryptor.finalize());raw.flush();os.fsync(raw.fileno())
        except InvalidTag:raise RecoveryToolError('backup_authentication_failed') from None
    # No TAR parsing, config export or PostgreSQL action is permitted before here.


def copy_config(source,target):
    source=Path(source)
    if source.is_symlink() or not source.is_file():raise RecoveryToolError('config_must_be_regular_file')
    initial=source.stat()
    if not 0<=initial.st_size<=2*1024**2:raise RecoveryToolError('config_size_limit')
    with source.open('rb') as original,private_file(target) as copy:
        actual=os.fstat(original.fileno())
        if not stat.S_ISREG(actual.st_mode) or (initial.st_dev,initial.st_ino)!=(actual.st_dev,actual.st_ino):
            raise RecoveryToolError('config_must_be_regular_file')
        shutil.copyfileobj(original,copy,BLOCK)
    after=source.stat()
    if (initial.st_dev,initial.st_ino,initial.st_size,initial.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
        raise RecoveryToolError('config_changed_during_backup')
