"""Declarative base, constraint naming convention and shared column mixins."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable, predictable names so later Alembic migrations can alter constraints by name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


class Timestamps:
    """UTC-aware timestamps. updated_at is also maintained by a database trigger (see the initial migration)."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def sql_list(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def enum_check(column: str, enum: type[StrEnum], name: str | None = None) -> CheckConstraint:
    """Controlled vocabulary stored as text, enforced by a CHECK (easier to migrate than native enums)."""
    return CheckConstraint(f"{column} IN ({sql_list(e.value for e in enum)})", name=name or f"{column}_valid")


SLUG_PATTERN = "^[a-z0-9]+(-[a-z0-9]+)*$"
HTTP_URL_PATTERN = "^https?://[^\\s]+$"


def slug_check(column: str = "slug") -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '{SLUG_PATTERN}'", name=f"{column}_format")


def url_check(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} IS NULL OR {column} ~ '{HTTP_URL_PATTERN}'", name=f"{column}_http")
