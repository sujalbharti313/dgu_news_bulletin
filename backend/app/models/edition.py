import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check, slug_check
from app.models.enums import EditionStatus


class Edition(UUIDPrimaryKey, Timestamps, Base):
    """A weekly bulletin. Its slug (for example "2026-w43") is the public identifier the frontend uses.

    Since Stage 3.5 the same row carries the editorial workflow: it is created in `generating` by the weekly
    generator, reviewed and edited as numbered revisions (`edition_revisions`, append-only), approved against one
    exact revision and published from that revision. Only `published` editions are ever public
    (app.repositories.visibility); every other status is admin-only."""

    __tablename__ = "editions"
    __table_args__ = (
        slug_check(),
        enum_check("status", EditionStatus),
        CheckConstraint("number > 0", name="number_positive"),
        CheckConstraint("week_end >= week_start AND week_end - week_start <= 6", name="week_range"),
        CheckConstraint("status <> 'published' OR published_at IS NOT NULL", name="published_has_timestamp"),
        CheckConstraint("current_revision >= 0", name="current_revision_non_negative"),
        CheckConstraint("generation_attempts >= 0", name="generation_attempts_non_negative"),
        # An approval belongs to exactly one revision; an approved edition's approval is for its current revision.
        CheckConstraint(
            "status <> 'approved' OR (approved_revision = current_revision AND approved_at IS NOT NULL "
            "AND approved_by IS NOT NULL AND approved_content_hash IS NOT NULL)",
            name="approved_matches_revision",
        ),
        # Stage 3.5 editions publish one approved revision (older/demo rows predate revisions).
        CheckConstraint(
            "status <> 'published' OR current_revision = 0 OR "
            "(published_revision IS NOT NULL AND published_revision = approved_revision)",
            name="published_from_approved_revision",
        ),
        # Demo and real editions are numbered independently so demo data never blocks real numbering.
        UniqueConstraint("number", "is_demo"),
        # One edition per week: the duplicate-generation guard.
        UniqueConstraint("week_start", "is_demo"),
        Index("ix_editions_public", "status", "week_start"),
    )

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    week_start: Mapped[date] = mapped_column(Date)
    week_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default=EditionStatus.DRAFT, server_default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # --- Stage 3.5 editorial workflow ---
    introduction: Mapped[str | None] = mapped_column(Text)
    editorial_note: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Kolkata", server_default="Asia/Kolkata")
    # 0 = no revision yet (still generating, or a pre-3.5 edition).
    current_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Not retried automatically before this time (backoff after a failure, or the next Gemini quota reset).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How the latest generation went: mode (ai / stage33_text), model, usage, safe error codes. No secrets.
    generation: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    ranking_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ranking_runs.id", ondelete="RESTRICT"), index=True
    )
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="SET NULL"), index=True
    )
    approved_revision: Mapped[int | None] = mapped_column(Integer)
    approved_content_hash: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editorial_admins.user_id", ondelete="RESTRICT")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_revision: Mapped[int | None] = mapped_column(Integer)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editorial_admins.user_id", ondelete="RESTRICT")
    )
    review_note: Mapped[str | None] = mapped_column(Text)

    stories = relationship("Story", back_populates="edition")
    briefing = relationship("Briefing", back_populates="edition", uselist=False)
