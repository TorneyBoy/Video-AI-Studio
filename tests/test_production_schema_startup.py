"""Production startup checks versions without performing database migrations."""
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine,inspect,text
from app import database
from app.migrations import migrate,DatabaseSchemaError,require_current_schema


def test_production_startup_requires_explicit_migration_and_never_repairs_history(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'schema.db'))
    monkeypatch.setattr(database,'engine',engine);monkeypatch.setattr(database,'get_settings',lambda:SimpleNamespace(app_environment='production'))
    try:
        with pytest.raises(DatabaseSchemaError,match='migration_required'):database.init_db()
        assert inspect(engine).get_table_names()==[]
        migrate(engine);database.init_db()
        with engine.begin() as db:db.execute(text("DELETE FROM schema_migrations WHERE version='0020_database_recovery_state'"))
        with pytest.raises(DatabaseSchemaError,match='migration_required'):database.init_db()
        with engine.connect() as db:assert db.scalar(text("SELECT count(*) FROM schema_migrations WHERE version='0020_database_recovery_state'"))==0
        migrate(engine);database.init_db()
    finally:engine.dispose()


def test_unknown_newer_history_blocks_startup_and_migration_without_writes(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'schema.db'));migrate(engine)
    try:
        with engine.begin() as db:db.execute(text("INSERT INTO schema_migrations VALUES ('9999_unrecognized','Explicit fixture','2026-10-18')"))
        with engine.connect() as db:before=db.execute(text('SELECT * FROM schema_migrations ORDER BY version')).all()
        for action in (require_current_schema,migrate):
            with pytest.raises(DatabaseSchemaError,match='newer_or_unknown'):action(engine)
        with engine.connect() as db:assert db.execute(text('SELECT * FROM schema_migrations ORDER BY version')).all()==before
    finally:engine.dispose()
