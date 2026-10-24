import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check
from app.models.enums import IngestionJobStatus, IngestionJobType


class IngestionJob(UUIDPrimaryKey, Timestamps, Base):
    """Bookkeeping for Phase 3 ingestion runs. Nothing in Phase 2 creates jobs, and there is no public
    endpoint. Never store credentials, tokens or raw newsletter bodies here."""

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        enum_check("job_type", IngestionJobType),
        enum_check("status", IngestionJobStatus),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at", name="time_order"),
        CheckConstraint("error_summary IS NULL OR length(error_summary) <= 2000", name="error_summary_length"),
        Index("ix_ingestion_jobs_status_created", "status", "created_at"),
    )

    # Job history outlives a deleted source.
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=IngestionJobStatus.QUEUED, server_default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)
