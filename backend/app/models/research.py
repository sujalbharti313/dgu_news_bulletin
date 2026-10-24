import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check, slug_check, sql_list, url_check
from app.models.enums import PublicationStatus, SummaryOrigin, Topic


class ResearchPaper(UUIDPrimaryKey, Timestamps, Base):
    """A paper or report. key_findings and limitations describe what the paper itself reports;
    plain_language_summary is our interpretation, and summary_origin says who wrote it."""

    __tablename__ = "research_papers"
    __table_args__ = (
        slug_check(),
        enum_check("status", PublicationStatus),
        enum_check("summary_origin", SummaryOrigin),
        CheckConstraint(f"topic IS NULL OR topic IN ({sql_list(t.value for t in Topic)})", name="topic_valid"),
        url_check("url"),
        CheckConstraint(
            "status <> 'published' OR (authors IS NOT NULL AND organisation IS NOT NULL AND published_on IS NOT NULL)",
            name="published_complete",
        ),
        Index("ix_research_papers_public", "status", "published_on"),
    )

    slug: Mapped[str] = mapped_column(String(120), unique=True)
    title: Mapped[str] = mapped_column(String(400))
    authors: Mapped[str | None] = mapped_column(Text)
    organisation: Mapped[str | None] = mapped_column(Text)
    published_on: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str] = mapped_column(Text, unique=True)
    doi: Mapped[str | None] = mapped_column(String(255), unique=True)
    topic: Mapped[str | None] = mapped_column(String(16))
    plain_language_summary: Mapped[str] = mapped_column(Text)
    summary_origin: Mapped[str] = mapped_column(String(16), default=SummaryOrigin.EDITORIAL, server_default="editorial")
    key_findings: Mapped[str] = mapped_column(Text)
    limitations: Mapped[str] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=PublicationStatus.DRAFT, server_default="draft")
