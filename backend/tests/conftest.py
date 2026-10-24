"""Test fixtures.

Database tests run against a disposable local PostgreSQL named by TEST_DATABASE_URL (the database name
must contain "test" and the host must be local). The schema is rebuilt with the real Alembic migrations,
and every test runs inside a transaction that is rolled back. Tests never touch Supabase.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import LOCAL_HOSTS, Settings, normalise_database_url
from app.db.session import Database
from app.main import create_app

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def make_settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "database_url": TEST_DATABASE_URL,
        "expose_demo_content": False,
        "rate_limit_per_minute": 0,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set; database tests skipped.")
    url = normalise_database_url(TEST_DATABASE_URL)
    if "test" not in (url.database or "") or url.host not in LOCAL_HOSTS:
        pytest.exit("TEST_DATABASE_URL must point at a local database whose name contains 'test'.", returncode=2)
    eng = create_engine(url)
    with eng.begin() as conn:
        # Safe only because of the guard above: this database exists solely for tests.
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.attributes["settings"] = make_settings()
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        conn.commit()
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


class TransactionalDatabase(Database):
    """Serves the test's rolled-back session to the app instead of opening real sessions."""

    def __init__(self, settings: Settings, session: Session):
        super().__init__(settings, engine=session.get_bind().engine)
        self._session = session

    def read_session(self) -> Iterator[Session]:
        self._session.flush()
        yield self._session


@pytest.fixture
def make_client(db_session: Session):
    def _make(**overrides) -> TestClient:
        settings = make_settings(**overrides)
        return TestClient(create_app(settings, database=TransactionalDatabase(settings, db_session)))

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def demo_client(make_client) -> TestClient:
    return make_client(expose_demo_content=True)
