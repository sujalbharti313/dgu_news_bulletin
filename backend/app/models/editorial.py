"""Stage 3.5 editorial workflow tables: admins, edition revisions (append-only snapshots), the relational selection
of stories per edition, and the append-only audit log. Nothing here is public: RLS is on with no policies and the
Supabase API roles have no privileges. The backend reaches them as the database owner."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check
from app.models.enums import EditionAuditAction


class EditorialAdmin(Timestamps, Base):
    """A person allowed to review, approve and publish editions. `user_id` is their Supabase Auth user id.

    Rows are added from the command line (`python -m scripts.edition admins add <email>`), never through the
    API. Deactivating keeps the row so audit entries and approvals still name the person."""

    __tablename__ = "editorial_admins"
    __table_args__ = (CheckConstraint("length(email) BETWEEN 3 AND 320", name="email_length"),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class EditionRevision(UUIDPrimaryKey, Base):
    """One saved version of an edition's publication content. Append-only (a trigger rejects UPDATE and DELETE),
    so the generated original and every admin revision stay traceable, and the published revision can never be
    changed afterwards. `content` is the full snapshot the public edition is built from (texts, source links,
    categories, order); later changes to news data never alter it."""

    __tablename__ = "edition_revisions"
    __table_args__ = (
        UniqueConstraint("edition_id", "revision"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("origin IN ('generated', 'admin_edit', 'regenerated')", name="origin_valid"),
    )

    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    # What changed compared with the previous revision (added/removed/reordered stories, edited fields).
    changes: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Validation issues found when the revision was saved (codes and field paths only).
    issues: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editorial_admins.user_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EditionItem(UUIDPrimaryKey, Timestamps, Base):
    """A story selected for an edition's current revision, in order, with the Stage 3.4 signals it was selected
    with. Rewritten whenever a revision changes the selection. `cluster_id` references the Stage 3.3 analysis
    (ON DELETE RESTRICT), so Stage 3.6 cleanup cannot remove a story an edition still refers to; `story_id` is
    the public `stories` row created when the edition is published."""

    __tablename__ = "edition_items"
    __table_args__ = (
        UniqueConstraint("edition_id", "cluster_id"),
        UniqueConstraint("edition_id", "position"),
        CheckConstraint("position > 0", name="position_positive"),
        CheckConstraint("selection_source IN ('ranking', 'admin_added')", name="selection_source_valid"),
        CheckConstraint(
            "eligibility IN ('eligible', 'review_required')",
            name="eligibility_valid",
        ),
        CheckConstraint("eligibility <> 'review_required' OR review_acknowledged_by IS NOT NULL", name="review_ack"),
    )

    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editions.id", ondelete="CASCADE"), index=True
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("story_analyses.cluster_id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    revision: Mapped[int] = mapped_column(Integer)
    selection_source: Mapped[str] = mapped_column(String(16))
    eligibility: Mapped[str] = mapped_column(String(24))
    ranking_rank: Mapped[int | None] = mapped_column(Integer)
    shortlist_rank: Mapped[int | None] = mapped_column(Integer)
    final_priority_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    review_acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editorial_admins.user_id", ondelete="RESTRICT")
    )
    story_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stories.id", ondelete="SET NULL"), index=True
    )


class EditionAuditEntry(Base):
    """Append-only log of editorial and workflow actions (a trigger rejects UPDATE and DELETE). Actor ids, action,
    revision and safe context only: never tokens, keys or article text."""

    __tablename__ = "edition_audit_log"
    __table_args__ = (
        enum_check("action", EditionAuditAction),
        CheckConstraint("actor_kind IN ('admin', 'scheduler', 'system', 'cli')", name="actor_kind_valid"),
        Index("ix_edition_audit_log_edition_occurred", "edition_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    edition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT")
    )
    revision: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editorial_admins.user_id", ondelete="RESTRICT")
    )
    details: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
