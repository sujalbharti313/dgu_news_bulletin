"""Smaller editorial collections shown on the home page: the India ecosystem, career signals and campus items."""

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check, slug_check, sql_list, url_check
from app.models.enums import CampusItemKind, CareerSignalKind, IndiaEntryKind, PublicationStatus, Topic


class IndiaEntry(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "india_entries"
    __table_args__ = (
        slug_check(),
        enum_check("kind", IndiaEntryKind),
        enum_check("status", PublicationStatus),
        url_check("url"),
        Index("ix_india_entries_public", "status", "display_order"),
    )

    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    focus: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=PublicationStatus.DRAFT, server_default="draft")
    display_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class CareerSignal(UUIDPrimaryKey, Timestamps, Base):
    """General learning suggestions. Not job listings, and no hiring or salary claims."""

    __tablename__ = "career_signals"
    __table_args__ = (
        slug_check(),
        enum_check("kind", CareerSignalKind),
        enum_check("status", PublicationStatus),
        CheckConstraint(
            f"related_topics <@ ARRAY[{sql_list(t.value for t in Topic)}]::text[]", name="related_topics_valid"
        ),
        Index("ix_career_signals_public", "status", "display_order"),
    )

    slug: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    start_with: Mapped[str] = mapped_column(Text)
    related_topics: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), default=PublicationStatus.DRAFT, server_default="draft")
    display_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class CampusItem(UUIDPrimaryKey, Timestamps, Base):
    """Campus listings. Real entries must come from approved university or student-organiser submissions;
    the seed only inserts clearly labelled demo formats."""

    __tablename__ = "campus_items"
    __table_args__ = (
        slug_check(),
        enum_check("kind", CampusItemKind),
        enum_check("status", PublicationStatus),
        Index("ix_campus_items_public", "status", "display_order"),
    )

    slug: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    when_text: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default=PublicationStatus.DRAFT, server_default="draft")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    display_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
