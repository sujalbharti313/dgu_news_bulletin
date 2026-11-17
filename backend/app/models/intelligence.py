import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check
from app.models.enums import AnalysisStatus


class StoryAnalysisRecord(UUIDPrimaryKey, Timestamps, Base):
    """Phase 3.3 AI analysis of one Stage 3.2 story cluster, plus the bookkeeping that makes runs idempotent.

    One row per cluster (`cluster_id` is unique). A cluster is analysed again only when its Stage 3.2 revision or
    the engine key changes, or when a failed attempt is retried. `analysis` holds the validated `StoryAnalysis`
    contract (generated text plus source *metadata*: titles, URLs, publishers, dates); article excerpts are never
    copied here. A few fields are also columns so Stage 3.4 can filter and sort without parsing JSON.
    Working data, not an archive: every row records `expires_at`. Nothing here is public."""

    __tablename__ = "story_analyses"
    __table_args__ = (
        enum_check("status", AnalysisStatus),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens_non_negative"),
        CheckConstraint("priority_score IS NULL OR (priority_score BETWEEN 0 AND 100)", name="priority_score_range"),
        CheckConstraint(
            "priority_level IS NULL OR priority_level IN ('Critical', 'High', 'Medium', 'Low')",
            name="priority_level_valid",
        ),
        # A usable analysis exists for completed/needs_review, may exist for insufficient_evidence, never otherwise.
        CheckConstraint(
            "(status IN ('completed', 'needs_review') AND analysis IS NOT NULL) "
            "OR status = 'insufficient_evidence' "
            "OR (status IN ('invalid_output', 'failed', 'quota_limited') AND analysis IS NULL)",
            name="analysis_matches_status",
        ),
        Index("ix_story_analyses_status_updated", "status", "updated_at"),
        Index("ix_story_analyses_priority", "priority_score"),
    )

    # The Stage 3.2 cluster id (deterministic; clusters are built on read from ingestion_items).
    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    # `processing.revision` of the cluster this row was computed from.
    cluster_revision: Mapped[str] = mapped_column(String(64))
    # Prompt, schema and category-set version. A change makes earlier analyses stale.
    engine_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Not retried before this time (failed: backoff; quota_limited: the next quota reset).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_id: Mapped[str | None] = mapped_column(String(120))
    primary_category: Mapped[str | None] = mapped_column(String(48))
    priority_score: Mapped[int | None] = mapped_column(SmallInteger)
    priority_level: Mapped[str | None] = mapped_column(String(16))
    # Token usage of the latest attempt cycle (all calls, including retries and repairs).
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    analysis: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Validation issues or the Gemini error of the latest attempt (codes and field paths, no article text).
    errors: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    review_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="SET NULL"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AiUsageDaily(UUIDPrimaryKey, Timestamps, Base):
    """Gemini usage per model per quota day (the date in America/Los_Angeles, when Google's daily free quotas
    reset). Counts only: no prompts, article text or keys. Used to stop before the free quota runs out."""

    __tablename__ = "ai_usage_daily"
    __table_args__ = (
        UniqueConstraint("usage_date", "provider", "model"),
        CheckConstraint(
            "requests >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND rate_limited >= 0 AND failed_requests >= 0",
            name="counts_non_negative",
        ),
    )

    usage_date: Mapped[date] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(120))
    requests: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    rate_limited: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_requests: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Set when Gemini reports the daily quota as used up; no request is sent for this model before then.
    quota_exhausted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
