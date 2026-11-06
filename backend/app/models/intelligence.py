import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check
from app.models.enums import IntelligenceStatus


class StoryIntelligenceRecord(UUIDPrimaryKey, Timestamps, Base):
    """Phase 3.3 AI analysis of one Stage 3.2 story cluster, plus the bookkeeping that makes runs idempotent.

    One row per cluster (`cluster_id` is unique). The row is re-analysed only when the cluster's revision or
    the engine version changes, or when a failed attempt is retried. `intelligence` holds the validated
    `StoryIntelligence` contract (generated text plus source *metadata*: titles, URLs, publishers, dates);
    article excerpts are never copied here. Working data, not an archive: every row records `expires_at`.
    Nothing in this table is public; publishing (Stage 3.5) copies what it needs into `stories`."""

    __tablename__ = "story_intelligence"
    __table_args__ = (
        enum_check("status", IntelligenceStatus),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens_non_negative"),
        CheckConstraint("estimated_cost_usd IS NULL OR estimated_cost_usd >= 0", name="cost_non_negative"),
        # Analysis is stored exactly when the status says a usable analysis exists.
        CheckConstraint(
            "(status IN ('analysed', 'needs_review')) = (intelligence IS NOT NULL)", name="intelligence_matches_status"
        ),
        Index("ix_story_intelligence_status_updated", "status", "updated_at"),
    )

    # The Stage 3.2 cluster id (deterministic; clusters themselves are built on read from ingestion_items).
    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    # `processing.revision` of the cluster this row was computed from.
    cluster_revision: Mapped[str] = mapped_column(String(64))
    # Prompt, schema and category-set version. A change makes earlier analyses stale.
    engine_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Failed stories are not retried before this time (unless a run asks to retry failures).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(120))
    # Token usage and estimated cost of the latest attempt cycle (all calls, including repairs).
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    intelligence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Validation issues or the provider error of the latest attempt (codes and field paths, no article text).
    errors: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    review_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="SET NULL"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
