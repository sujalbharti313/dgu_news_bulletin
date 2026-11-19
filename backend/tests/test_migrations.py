"""Migrations apply, roll back and re-apply on an empty database, and match the ORM models exactly."""

import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.core.config import normalise_database_url
from app.db.base import Base
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL, make_settings

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")


@pytest.fixture
def scratch_url(engine):  # engine fixture enforces the local/test guard
    base = normalise_database_url(TEST_DATABASE_URL)
    name = f"{base.database}_migrations"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield base.set(database=name)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def test_upgrade_downgrade_upgrade_and_no_drift(scratch_url):
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.attributes["settings"] = make_settings(database_url=scratch_url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    eng = create_engine(scratch_url)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
        assert diff == [], diff
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
    eng.dispose()


def test_upgrade_works_from_the_rolled_back_phase_3_3_state(scratch_url):
    """A database still at the old 0004 (rolled-back story_intelligence table present) upgrades to the head without
    touching that table, and the rollback script stays safe to run afterwards."""
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.attributes["settings"] = make_settings(database_url=scratch_url.render_as_string(hide_password=False))
    command.upgrade(cfg, "0003")
    eng = create_engine(scratch_url)
    with eng.begin() as conn:  # what the old migration 0004 left behind
        conn.execute(text("CREATE TABLE story_intelligence (id uuid PRIMARY KEY, note text)"))
        conn.execute(text("INSERT INTO story_intelligence VALUES (gen_random_uuid(), 'old')"))
        conn.execute(text("UPDATE alembic_version SET version_num = '0004'"))
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
        assert conn.scalar(text("SELECT count(*) FROM story_intelligence")) == 1  # untouched
        assert conn.scalar(text("SELECT to_regclass('story_analyses') IS NOT NULL"))
    rollback = os.path.join(BACKEND_DIR, "migrations", "rollback", "0004_drop_story_intelligence.sql")
    with open(rollback, encoding="utf-8") as fh, eng.connect() as conn:
        conn.connection.driver_connection.execute(fh.read())
        conn.commit()
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
        assert not conn.scalar(text("SELECT to_regclass('story_intelligence') IS NOT NULL"))
        assert conn.scalar(text("SELECT to_regclass('story_analyses') IS NOT NULL"))
    eng.dispose()
