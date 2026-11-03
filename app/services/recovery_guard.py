"""A restore starts isolated. No HTTP or poller action activates it implicitly."""
from sqlalchemy import select,text
from .. import models


class RecoveryQuarantined(RuntimeError):
    pass


def database_state(db):
    if db.get_bind().dialect.name=='postgresql':
        quarantine,readonly=db.execute(text("SELECT current_setting('video_ai.recovery_quarantine',true),current_setting('transaction_read_only')")).one()
        if quarantine=='on':return {'state':'quarantined','instance_id':None,'backup_id':None}
        # A read-only source must not report ready or create execution tickets.
        # This is a tripwire; it does not prove all source processes are fenced.
        if readonly=='on':return {'state':'fenced','instance_id':None,'backup_id':None}
    # Select columns on every check; an old identity-map value is not a fence.
    row=db.execute(select(models.DatabaseRecoveryState.state,models.DatabaseRecoveryState.instance_id,models.DatabaseRecoveryState.backup_id).where(models.DatabaseRecoveryState.id==1)).first()
    # create_all-only fixtures / pre-adoption sources have no seeded row.
    return {'state':row.state,'instance_id':row.instance_id,'backup_id':row.backup_id} if row else {'state':'active','instance_id':None,'backup_id':None}


def database_active(db):return database_state(db)['state']=='active'


def require_active_database(db):
    if not database_active(db):raise RecoveryQuarantined('工作台正在恢复维护，请稍后核对原任务。')
