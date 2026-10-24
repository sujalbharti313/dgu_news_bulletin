"""Engine and session lifecycle.

The engine is created lazily and never connects at import or startup, so the API can boot (and report
an honest health status) when the database is down or not configured yet.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.errors import DatabaseNotConfiguredError


def build_engine(settings: Settings) -> Engine | None:
    url = settings.sqlalchemy_url()
    if url is None:
        return None
    connect_args: dict = {
        "connect_timeout": settings.db_connect_timeout_seconds,
        "application_name": "ai-campus-api",
    }
    if not settings.use_prepared_statements():
        # Supabase's transaction pooler cannot keep server-side prepared statements between transactions.
        connect_args["prepare_threshold"] = None
    pool_args: dict = (
        {"poolclass": NullPool}
        if settings.db_use_null_pool
        else {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_timeout": settings.db_pool_timeout_seconds,
            "pool_recycle": 1800,
        }
    )
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args, **pool_args)


class Database:
    def __init__(self, settings: Settings, engine: Engine | None = None):
        self.settings = settings
        self.engine = engine if engine is not None else build_engine(settings)
        self._sessions = (
            sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False) if self.engine else None
        )
        if self._sessions is not None and settings.db_statement_timeout_ms:
            event.listen(self._sessions, "after_begin", self._apply_statement_timeout)

    @property
    def configured(self) -> bool:
        return self.engine is not None

    def _open(self) -> Session:
        if self._sessions is None:
            raise DatabaseNotConfiguredError()
        return self._sessions()

    def _apply_statement_timeout(self, session: Session, transaction, connection) -> None:
        # Runs when a transaction starts, i.e. on the first query, never for requests that fail validation.
        # Transaction-local (is_local = true), so it never leaks to other clients of a shared pooler.
        connection.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(self.settings.db_statement_timeout_ms)},
        )

    def read_session(self) -> Iterator[Session]:
        """Request-scoped session for read endpoints. Connects lazily; always rolled back and closed."""
        session = self._open()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        """Unit of work for CLI writes (seeding): commits everything or nothing."""
        session = self._open()
        try:
            with session.begin():
                yield session
        finally:
            session.close()

    def ping(self) -> None:
        if self.engine is None:
            raise DatabaseNotConfiguredError()
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def dispose(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
