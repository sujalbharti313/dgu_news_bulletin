import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check, url_check
from app.models.enums import (
    DedupRelation,
    IngestionItemStatus,
    IngestionJobStatus,
    IngestionJobType,
    IngestionSourceType,
)


class IngestionJob(UUIDPrimaryKey, Timestamps, Base):
    """One collection attempt for one configured source (Phase 3.1), with its outcome and counters.

    `details` holds counts and the source's incremental cursor (feed ETag, newest entry date, processed
    Gmail message ids). Never store credentials, tokens, raw newsletter bodies or article text here."""

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        enum_check("job_type", IngestionJobType),
        enum_check("status", IngestionJobStatus),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at", name="time_order"),
        CheckConstraint("error_summary IS NULL OR length(error_summary) <= 2000", name="error_summary_length"),
        Index("ix_ingestion_jobs_status_created", "status", "created_at"),
        Index("ix_ingestion_jobs_source_key_created", "source_key", "created_at"),
        # At most one running job per source, across every process and host: the concurrency guard.
        Index(
            "uq_ingestion_jobs_one_running_per_source",
            "source_key",
            unique=True,
            postgresql_where=text("status = 'running' AND source_key IS NOT NULL"),
        ),
    )

    # Job history outlives a deleted source.
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    # The `key` of the source in the ingestion source configuration file.
    source_key: Mapped[str | None] = mapped_column(String(64))
    job_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=IngestionJobStatus.QUEUED, server_default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONB)


class IngestionItem(UUIDPrimaryKey, Timestamps, Base):
    """A normalised news item waiting for the next pipeline stage. TEMPORARY by design.

    This is a hand-off queue, not an archive: every row has an `expires_at` and is deleted automatically
    by each ingestion run (and `scripts.ingest purge`). Only metadata and a short excerpt are kept, never a
    full article or email body. Published stories live in `stories` and are never touched by ingestion."""

    __tablename__ = "ingestion_items"
    __table_args__ = (
        enum_check("source_type", IngestionSourceType),
        enum_check("processing_status", IngestionItemStatus),
        url_check("original_url"),
        url_check("canonical_url"),
        CheckConstraint("length(title) BETWEEN 1 AND 500", name="title_length"),
        CheckConstraint("excerpt IS NULL OR length(excerpt) <= 2000", name="excerpt_length"),
        CheckConstraint("expires_at > collected_at", name="expires_after_collection"),
        Index("ix_ingestion_items_status_collected", "processing_status", "collected_at"),
        enum_check("dedup_relation", DedupRelation),
        CheckConstraint(
            "processing_status <> 'processed' OR (cluster_id IS NOT NULL AND dedup_relation IS NOT NULL)",
            name="processed_has_cluster",
        ),
    )

    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="SET NULL"), index=True
    )
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(16))
    # sha256 of canonical_url: the same article from two feeds (or two runs) is staged once.
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    original_url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excerpt: Mapped[str | None] = mapped_column(Text)
    # Where the item came from: the feed URL, or "gmail:<message id>" for a newsletter.
    feed_identifier: Mapped[str] = mapped_column(String(500))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processing_status: Mapped[str] = mapped_column(
        String(16), default=IngestionItemStatus.PENDING, server_default="pending"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Small, bounded provider metadata (author, categories, read time, newsletter date). No content.
    # `also_seen` lists other sources that carried the same canonical URL (Phase 3.2 provenance).
    extra: Mapped[dict | None] = mapped_column(JSONB)

    # --- Phase 3.2 processing results (migration 0003). Written only by app/processing; they expire with
    # the row. No Python-side defaults, so Stage 3.1 inserts never name these columns.
    # sha256 of the normalised title + excerpt; set only when the excerpt is long enough to be distinctive.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # Story cluster this item belongs to (a deterministic UUID derived from the cluster's first item).
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    dedup_relation: Mapped[str | None] = mapped_column(String(24))
    # For exact and near duplicates: the item this one duplicates.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_items.id", ondelete="SET NULL"), index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Derived signals and the reasons for the decision (entities, event type, scores). No article text.
    processing: Mapped[dict | None] = mapped_column(JSONB)
