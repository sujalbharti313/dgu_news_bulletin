import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check, slug_check, url_check
from app.models.enums import PublicationStatus, ToolCategory


class Tool(UUIDPrimaryKey, Timestamps, Base):
    """An AI tool or model. Pricing and capabilities change often: pricing_notes stays null unless
    checked against the official page, and last_verified_at records when that happened."""

    __tablename__ = "tools"
    __table_args__ = (
        slug_check(),
        enum_check("category", ToolCategory),
        enum_check("status", PublicationStatus),
        url_check("official_url"),
        CheckConstraint("pricing_notes IS NULL OR last_verified_at IS NOT NULL", name="pricing_is_verified"),
        Index("ix_tools_public", "status", "display_order"),
    )

    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[str] = mapped_column(Text)
    good_for: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    limitations: Mapped[str] = mapped_column(Text)
    access_notes: Mapped[str] = mapped_column(Text)
    pricing_notes: Mapped[str | None] = mapped_column(Text)
    official_url: Mapped[str] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default=PublicationStatus.DRAFT, server_default="draft")
    display_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
