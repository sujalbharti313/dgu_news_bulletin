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
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
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
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
        assert conn.scalar(text("SELECT count(*) FROM story_intelligence")) == 1  # untouched
        assert conn.scalar(text("SELECT to_regclass('story_analyses') IS NOT NULL"))
    rollback = os.path.join(BACKEND_DIR, "migrations", "rollback", "0004_drop_story_intelligence.sql")
    with open(rollback, encoding="utf-8") as fh, eng.connect() as conn:
        conn.connection.driver_connection.execute(fh.read())
        conn.commit()
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
        assert not conn.scalar(text("SELECT to_regclass('story_intelligence') IS NOT NULL"))
        assert conn.scalar(text("SELECT to_regclass('story_analyses') IS NOT NULL"))
    eng.dispose()


def test_data_api_roles_have_no_privileges_on_any_table(scratch_url):
    """Supabase's anon/authenticated roles (simulated here) end with no table, sequence or function rights in
    public, including on the 0001-0006 tables that were created before the per-table revokes."""
    eng = create_engine(scratch_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        for role in ("anon", "authenticated"):
            if not conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}):
                conn.execute(text(f"CREATE ROLE {role} NOLOGIN"))
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.attributes["settings"] = make_settings(database_url=scratch_url.render_as_string(hide_password=False))
    command.upgrade(cfg, "0008")
    with eng.connect() as conn:  # what Supabase's default privileges do for tables made by 0001-0006
        conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated"))
        conn.execute(text("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated"))
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        tables = conn.scalars(
            text("SELECT c.oid::regclass::text FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
                 "AND c.relkind IN ('r', 'p', 'S')")
        ).all()  # fmt: skip
        assert "stories" in tables and "story_embeddings" in tables
        for role in ("anon", "authenticated"):
            for table in tables:
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    if priv != "SELECT" and table.endswith("_seq"):
                        continue
                    query = "SELECT has_table_privilege(:r, :t, :p)"
                    assert not conn.scalar(text(query), {"r": role, "t": table, "p": priv}), (role, table, priv)
            granted = conn.scalar(
                text("SELECT count(*) FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
                     "AND has_function_privilege(:r, p.oid, 'EXECUTE') AND p.proname = 'match_story_embeddings'"),
                {"r": role},
            )  # fmt: skip
            assert granted == 0
    eng.dispose()
