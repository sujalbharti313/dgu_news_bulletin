import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db.session import Database
from tests.conftest import make_settings


def test_statement_timeout_is_applied_per_transaction(engine):
    db = Database(make_settings(db_statement_timeout_ms=200, db_use_null_pool=True))
    try:
        gen = db.read_session()
        session = next(gen)
        assert session.scalar(text("SHOW statement_timeout")) == "200ms"
        with pytest.raises(OperationalError):
            session.execute(text("SELECT pg_sleep(2)"))
        gen.close()
        # The setting is transaction-local: a plain connection afterwards is unaffected.
        with db.engine.connect() as conn:
            assert conn.scalar(text("SHOW statement_timeout")) == "0"
    finally:
        db.dispose()


def test_write_session_is_all_or_nothing(engine):
    db = Database(make_settings(db_use_null_pool=True))
    try:
        with pytest.raises(RuntimeError):
            with db.write_session() as session:
                session.execute(
                    text(
                        "INSERT INTO sources (slug, name, kind, canonical_domain) "
                        "VALUES ('rollback-check', 'R', 'rss', 'rollback.example.test')"
                    )
                )
                raise RuntimeError("abort")
        with db.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM sources WHERE slug = 'rollback-check'")) == 0
    finally:
        db.dispose()
