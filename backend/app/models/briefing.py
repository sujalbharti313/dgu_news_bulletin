import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check, url_check
from app.models.enums import PublicationStatus


class Briefing(UUIDPrimaryKey, Timestamps, Base):
    """Audio briefing metadata for one edition. Audio generation itself is Phase 3; audio_url stays
    null until a real recorded or synthesised file exists."""

    __tablename__ = "audio_briefings"
    __table_args__ = (
        enum_check("status", PublicationStatus),
        url_check("audio_url"),
        CheckConstraint("duration_seconds IS NULL OR duration_seconds > 0", name="duration_positive"),
        CheckConstraint("audio_url IS NOT NULL OR duration_seconds IS NULL", name="duration_needs_audio"),
        CheckConstraint("status <> 'published' OR published_at IS NOT NULL", name="published_has_timestamp"),
    )

    # A briefing is owned by its edition: deleting the edition removes the briefing metadata.
    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editions.id", ondelete="CASCADE"), unique=True
    )
    title: Mapped[str] = mapped_column(String(200))
    audio_url: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    # Paragraphs separated by a blank line.
    transcript: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default=PublicationStatus.DRAFT, server_default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    edition = relationship("Edition", back_populates="briefing")
