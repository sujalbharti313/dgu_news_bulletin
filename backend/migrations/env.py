"""Alembic environment. Connection details come from DATABASE_URL via app.core.config, never from alembic.ini."""

import sys
from logging.config import fileConfig

from alembic import context

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import Settings, describe_database_target
from app.db.base import Base
from app.db.session import build_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _settings() -> Settings:
    # Tests and tooling may hand over settings explicitly; otherwise read the environment.
    provided = config.attributes.get("settings")
    settings = provided if provided is not None else Settings()
    if settings.database_url is None:
        sys.exit("DATABASE_URL is not set. Configure it in backend/.env or the environment before migrating.")
    # Migrations hold one connection briefly; never keep a pool around.
    return settings.model_copy(update={"db_use_null_pool": True})


def run_migrations_offline() -> None:
    settings = _settings()
    context.configure(
        url=settings.sqlalchemy_url().render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = _settings()
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = build_engine(settings)
    print(f"Migrating {describe_database_target(settings)}", file=sys.stderr)
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()


def _run(connection) -> None:
    # Every migration runs inside one transaction: PostgreSQL DDL is transactional, so a failure rolls back cleanly.
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
