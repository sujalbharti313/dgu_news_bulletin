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
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
    eng.dispose()
