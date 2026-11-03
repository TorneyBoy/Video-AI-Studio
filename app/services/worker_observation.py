"""A small independent DB heartbeat while the poller waits on external IO."""
from contextlib import contextmanager
from datetime import datetime,timezone
from threading import Event,Thread
from uuid import uuid4
from sqlalchemy import update
from .. import models
from .runtime_logging import emit,fingerprint
from .recovery_guard import database_active,require_active_database


def pulse(factory,id):
    with factory() as db:
        if not database_active(db):return
        db.execute(update(models.WorkerPulse).where(models.WorkerPulse.id==id,models.WorkerPulse.stopped_at.is_(None)).values(last_seen_at=datetime.now(timezone.utc)))
        db.commit()


@contextmanager
def observed_worker(factory,worker_id,*,interval=30):
    identifier=str(uuid4());stop=Event()
    with factory() as db:
        require_active_database(db)
        db.add(models.WorkerPulse(id=identifier,worker_tag=fingerprint(worker_id),last_seen_at=datetime.now(timezone.utc)));db.commit()
    def heartbeat():
        while not stop.wait(interval):
            try:pulse(factory,identifier)
            except Exception:emit('worker_heartbeat_error',instance_id=identifier,code='heartbeat_persistence_unavailable')
    thread=Thread(target=heartbeat,name='video-ai-heartbeat',daemon=True);thread.start()
    try:yield identifier
    finally:
        stop.set();thread.join(timeout=2)
        try:
            with factory() as db:
                if database_active(db):
                    now=datetime.now(timezone.utc);db.execute(update(models.WorkerPulse).where(models.WorkerPulse.id==identifier).values(last_seen_at=now,stopped_at=now));db.commit()
        except Exception:emit('worker_heartbeat_error',instance_id=identifier,code='heartbeat_stop_unconfirmed')
