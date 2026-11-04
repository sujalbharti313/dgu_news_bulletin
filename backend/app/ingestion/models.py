"""Typed records passed between ingestion stages."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import IngestionItemStatus, IngestionSourceType


@dataclass
class RawItem:
    """What a provider extracted, before validation. Values are untrusted and may be missing."""

    title: str | None
    url: str | None
    feed_identifier: str
    publisher: str | None = None
    published_at: datetime | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectResult:
    """A provider's output for one source: candidate items, the next cursor and provider-level counts."""

    items: list[RawItem]
    cursor: dict[str, Any]
    not_modified: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class NormalizedItem(BaseModel):
    """A validated item ready for the next stage. Mirrors the ingestion_items table."""

    model_config = ConfigDict(frozen=True)

    source_key: str
    source_type: IngestionSourceType
    title: str = Field(min_length=1, max_length=500)
    original_url: str
    canonical_url: str
    dedup_key: str = Field(min_length=64, max_length=64)
    publisher: str | None = Field(default=None, max_length=200)
    published_at: datetime | None = None
    excerpt: str | None = Field(default=None, max_length=2000)
    feed_identifier: str = Field(max_length=500)
    collected_at: datetime
    processing_status: IngestionItemStatus = IngestionItemStatus.PENDING
    extra: dict[str, Any] = Field(default_factory=dict)


class SourceOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class SourceReport:
    key: str
    type: str
    outcome: SourceOutcome
    reason: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    duration_ms: float = 0.0
    job_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.type,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "counts": self.counts,
            "durationMs": round(self.duration_ms, 1),
            "jobId": self.job_id,
        }


@dataclass
class RunReport:
    started_at: datetime
    finished_at: datetime | None = None
    dry_run: bool = False
    purged_items: int = 0
    purged_jobs: int = 0
    sources: list[SourceReport] = field(default_factory=list)

    @property
    def status(self) -> str:
        """succeeded: nothing failed. partial: some sources failed. failed: every attempted source failed."""
        attempted = [s for s in self.sources if s.outcome is not SourceOutcome.SKIPPED]
        failed = [s for s in attempted if s.outcome is SourceOutcome.FAILED]
        if not failed:
            return "succeeded"
        return "failed" if len(failed) == len(attempted) else "partial"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dryRun": self.dry_run,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "purgedItems": self.purged_items,
            "purgedJobs": self.purged_jobs,
            "sources": [s.as_dict() for s in self.sources],
        }
