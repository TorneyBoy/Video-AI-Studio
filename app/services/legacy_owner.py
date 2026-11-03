"""Operator-only preparation of a verified legacy space on a quarantined copy.

No public route, session creation, credential reset or business-data reassignment.
The caller hashes a first-use password before acquiring database locks.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID

from sqlalchemy import select,text

from .. import models
from .identity import normalize_login_name,IdentityError


class AdoptionError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def _uuid(value):
    try:return str(UUID(value))
    except (ValueError,TypeError,AttributeError):raise AdoptionError('adoption_uuid_invalid') from None


@dataclass(frozen=True)
class AdoptionIntent:
    request_id: str
    tenant_id: str
    expected_slug: str
    database_instance_id: str
    login_name: str
    display_name: str

    @classmethod
    def make(cls,*,request_id,tenant_id,expected_slug,database_instance_id,login_name,display_name=''):
        try:login=normalize_login_name(login_name)
        except IdentityError:raise AdoptionError('adoption_login_name_invalid') from None
        display=display_name.strip() or login
        if not 1<=len(tenant_id)<=36 or tenant_id.strip()!=tenant_id or any(ord(char)<32 for char in tenant_id):
            raise AdoptionError('adoption_tenant_id_invalid')
        if not 1<=len(expected_slug)<=100 or len(display)>80 or any(ord(char)<32 for char in expected_slug+display):
            raise AdoptionError('adoption_display_or_slug_invalid')
        return cls(_uuid(request_id),tenant_id,expected_slug,_uuid(database_instance_id),login,display)

    def fingerprint(self):
        # Passwords and even fast password digests never enter the request receipt.
        return hashlib.sha256(json.dumps(self.__dict__,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _guard(db,*,lock=False):
    if db.get_bind().dialect.name=='postgresql':
        quarantine,readonly=db.execute(text("SELECT current_setting('video_ai.recovery_quarantine',true),current_setting('transaction_read_only')")).one()
        if quarantine!='on':raise AdoptionError('adoption_requires_database_quarantine')
        if readonly=='on':raise AdoptionError('adoption_requires_writable_quarantined_copy')
    query=select(models.DatabaseRecoveryState).where(models.DatabaseRecoveryState.id==1)
    if lock:query=query.with_for_update().execution_options(populate_existing=True)
    guard=db.scalar(query)
    if guard is None or guard.state!='quarantined':raise AdoptionError('adoption_requires_quarantined_copy')
    return guard


def _unowned(db,tenant):
    if db.scalar(select(models.User.id).where(models.User.personal_tenant_id==tenant.id)):
        raise AdoptionError('legacy_tenant_already_owned')
    if db.scalar(select(models.Membership.id).where(models.Membership.tenant_id==tenant.id)):
        raise AdoptionError('legacy_tenant_has_memberships')
    if db.scalar(select(models.LegacyTenantAdoption.request_id).where(models.LegacyTenantAdoption.tenant_id==tenant.id)):
        raise AdoptionError('legacy_tenant_has_prior_adoption')


def plan_adoption(db,*,tenant_id,expected_slug,login_name):
    guard=_guard(db)
    tenant=db.get(models.Tenant,tenant_id)
    if tenant is None or tenant.slug!=expected_slug:raise AdoptionError('adoption_tenant_scope_mismatch')
    _unowned(db,tenant)
    try:login=normalize_login_name(login_name)
    except IdentityError:raise AdoptionError('adoption_login_name_invalid') from None
    if db.scalar(select(models.User.id).where(models.User.login_name==login)):
        raise AdoptionError('adoption_login_name_unavailable')
    return {'tenant_id':tenant.id,'expected_slug':tenant.slug,'database_instance_id':guard.instance_id,
        'login_name':login,'quarantined':True,'can_prepare_owner':True,'ownership_verification_is_operator_responsibility':True,
        'business_data_reassignment':False,'execution_activated':False}


def adoption_status(db,request_id):
    row=db.get(models.LegacyTenantAdoption,_uuid(request_id))
    if row is None:return {'found':False,'request_id':_uuid(request_id)}
    return {'found':True,'result':row.result_json,'bound_user_still_exists':row.user_id is not None,
        'credential_changed':False,'session_issued':False}


def apply_adoption(db,intent,*,password_hash=None):
    guard=_guard(db,lock=True)
    if guard.instance_id!=intent.database_instance_id:raise AdoptionError('adoption_database_instance_mismatch')
    previous=db.get(models.LegacyTenantAdoption,intent.request_id,populate_existing=True)
    if previous:
        if previous.intent_hash!=intent.fingerprint():raise AdoptionError('adoption_request_conflict')
        return {'disposition':'replayed','result':previous.result_json,'credential_changed':False,'session_issued':False}
    tenant=db.scalar(select(models.Tenant).where(models.Tenant.id==intent.tenant_id).with_for_update().execution_options(populate_existing=True))
    if tenant is None or tenant.slug!=intent.expected_slug:raise AdoptionError('adoption_tenant_scope_mismatch')
    _unowned(db,tenant)
    if db.scalar(select(models.User.id).where(models.User.login_name==intent.login_name)):
        raise AdoptionError('adoption_login_name_unavailable')
    if not isinstance(password_hash,str) or not re.fullmatch(r'scrypt\$131072\$8\$1\$[a-f0-9]{32}\$[a-f0-9]{64}',password_hash):
        raise AdoptionError('adoption_first_password_hash_required')
    user=models.User(login_name=intent.login_name,display_name=intent.display_name,
        password_hash=password_hash,personal_tenant_id=tenant.id,credential_epoch=0,status='active')
    db.add(user);db.flush()
    member=models.Membership(user_id=user.id,tenant_id=tenant.id,role='owner',status='active')
    db.add(member);db.flush()
    result={'request_id':intent.request_id,'tenant_id':tenant.id,'user_id':user.id,
        'membership_id':member.id,'login_name':user.login_name,'business_data_reassigned':False,
        'budget_modified':False,'execution_activated':False,'password_returned':False}
    db.add(models.LegacyTenantAdoption(request_id=intent.request_id,tenant_id=tenant.id,user_id=user.id,
        database_instance_id=guard.instance_id,intent_hash=intent.fingerprint(),result_json=result))
    db.flush()
    return {'disposition':'created','result':result,'credential_changed':True,'session_issued':False}
