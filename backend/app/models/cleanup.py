"""Stage 3.6 data-retention tables: cleanup jobs, their manifests and the append-only cleanup audit log.

None of these hold news content: ids, counts, states, reason codes and safe error text only. Nothing here is
public (RLS on with no policies, no privileges for Supabase's API roles), and the database rejects DELETE and
TRUNCATE on all three tables, so the cleanup history can never be removed by the cleanup engine itself."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check
from app.models.enums import CleanupAuditAction, CleanupJobKind, CleanupJobStatus, CleanupTarget, ManifestState


class CleanupJob(UUIDPrimaryKey, Timestamps, Base):
    """One cleanup of one published week (or one sweep of expired orphans). `job_key` makes creation idempotent:
    the scheduler can run any number of times and never creates a second job for the same edition/window.

    Timeline: `not_before` (published_at + safety delay, and window end + minimum story age) → manifest staged
    (`staged_at`) → `execute_after` (staged_at + manifest hold) → deletion in bounded batches → completed only
    after the deletions were verified."""

    __tablename__ = "cleanup_jobs"
    __table_args__ = (
        enum_check("kind", CleanupJobKind),
        enum_check("status", CleanupJobStatus),
        CheckConstraint("trigger IN ('scheduler', 'admin', 'cli')", name="trigger_valid"),
        CheckConstraint(
            "kind <> 'published_week' OR (edition_id IS NOT NULL AND window_start IS NOT NULL "
            "AND window_end IS NOT NULL)",
            name="published_week_has_edition",
        ),
        CheckConstraint("window_end IS NULL OR window_end = window_start + 6", name="window_is_one_week"),
        CheckConstraint(
            "eligible_count >= 0 AND protected_count >= 0 AND deleted_count >= 0 AND skipped_count >= 0 "
            "AND failure_count >= 0 AND retry_count >= 0",
            name="counts_non_negative",
        ),
        CheckConstraint("status <> 'completed' OR completed_at IS NOT NULL", name="completed_has_timestamp"),
        CheckConstraint("status <> 'staged' OR staged_at IS NOT NULL", name="staged_has_timestamp"),
        CheckConstraint("last_error IS NULL OR length(last_error) <= 500", name="last_error_length"),
        # One cleanup job per published edition, whatever its key.
        Index(
            "uq_cleanup_jobs_one_per_edition",
            "edition_id",
            unique=True,
            postgresql_where=text("kind = 'published_week'"),
        ),
        Index("ix_cleanup_jobs_status_not_before", "status", "not_before"),
    )

    job_key: Mapped[str] = mapped_column(String(80), unique=True)
    kind: Mapped[str] = mapped_column(String(24))
    edition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT")
    )
    window_start: Mapped[date | None] = mapped_column(Date)
    window_end: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editorial_admins.user_id", ondelete="RESTRICT")
    )
    policy_version: Mapped[str] = mapped_column(String(64))
    # Safety delay: no manifest (and so no deletion) before this instant.
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Manifest hold: no deletion before this instant (revalidated again at deletion time).
    execute_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eligible_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    protected_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    deleted_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Safe reason codes of the latest failed publication check (empty when it passed).
    blocked_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    # Reason code -> number of records protected or skipped.
    protection_summary: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Per-target counts and the publication check result. Ids and counts only.
    details: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


class CleanupManifestItem(UUIDPrimaryKey, Timestamps, Base):
    """One record a cleanup job considered: what it is (`target` + `record_id`), and what happened to it. Protected
    records are listed too, with the reason, so an admin can see exactly why something was kept. No payloads."""

    __tablename__ = "cleanup_manifest_items"
    __table_args__ = (
        UniqueConstraint("job_id", "target", "record_id"),
        enum_check("target", CleanupTarget),
        enum_check("state", ManifestState),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("state = 'pending' OR processed_at IS NOT NULL", name="processed_has_timestamp"),
        CheckConstraint("state NOT IN ('protected', 'skipped', 'failed') OR reason IS NOT NULL", name="has_reason"),
        Index("ix_cleanup_manifest_items_job_state", "job_id", "state"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cleanup_jobs.id", ondelete="RESTRICT"))
    target: Mapped[str] = mapped_column(String(24))
    # story_cluster / orphan_verification: the Stage 3.2 cluster id; ranking_run: ranking_runs.id.
    record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(16), default=ManifestState.PENDING, server_default="pending")
    reason: Mapped[str | None] = mapped_column(String(48))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CleanupAuditEntry(Base):
    """Append-only log of cleanup decisions and actions (a trigger rejects UPDATE, DELETE and TRUNCATE). Counts,
    reason codes, ids and the policy version only: never tokens, keys or article text."""

    __tablename__ = "cleanup_audit_log"
    __table_args__ = (
        enum_check("action", CleanupAuditAction),
        CheckConstraint("actor_kind IN ('admin', 'scheduler', 'system', 'cli')", name="actor_kind_valid"),
        Index("ix_cleanup_audit_log_job_occurred", "job_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cleanup_jobs.id", ondelete="RESTRICT")
    )
    edition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(32))
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("editorial_admins.user_id", ondelete="RESTRICT")
    )
    policy_version: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
