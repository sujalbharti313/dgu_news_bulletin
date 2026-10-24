from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check, slug_check
from app.models.enums import PublicationStatus


class Edition(UUIDPrimaryKey, Timestamps, Base):
    """A weekly bulletin. Its slug (for example "2026-w43") is the public identifier the frontend uses."""

    __tablename__ = "editions"
    __table_args__ = (
        slug_check(),
        enum_check("status", PublicationStatus),
        CheckConstraint("number > 0", name="number_positive"),
        CheckConstraint("week_end >= week_start AND week_end - week_start <= 6", name="week_range"),
        CheckConstraint("status <> 'published' OR published_at IS NOT NULL", name="published_has_timestamp"),
        # Demo and real editions are numbered independently so demo data never blocks real numbering.
        UniqueConstraint("number", "is_demo"),
        UniqueConstraint("week_start", "is_demo"),
        Index("ix_editions_public", "status", "week_start"),
    )

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    week_start: Mapped[date] = mapped_column(Date)
    week_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=PublicationStatus.DRAFT, server_default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    stories = relationship("Story", back_populates="edition")
    briefing = relationship("Briefing", back_populates="edition", uselist=False)
