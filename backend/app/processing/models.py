"""Records used inside Stage 3.2, and the thresholds that control its decisions."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import Settings
from app.ingestion.models import NormalizedItem

# Bump when a change to the rules would make earlier decisions incomparable. Stored with every decision.
ALGORITHM_VERSION = "3.2.0"
# Namespace for deterministic ids (dry-run item ids and cluster ids).
ID_NAMESPACE = uuid.UUID("6f1d3c2a-9b0e-4c55-8a51-3a2f0c7d9e10")


@dataclass(frozen=True)
class Thresholds:
    """Decision thresholds. Configurable through PROCESSING_* settings; documented in the README."""

    near_duplicate_text: float = 0.5
    near_duplicate_title: float = 0.4
    title_only: float = 0.85
    title_only_window_hours: int = 48
    near_duplicate_window_hours: int = 168
    story: float = 0.3
    story_window_hours: int = 96
    min_body_tokens: int = 15
    # Extra score needed when a side has no recognised entity, or no recognised event type.
    no_entity_penalty: float = 0.15
    unknown_event_penalty: float = 0.1
    # Weights of the story score.
    weight_title: float = 0.4
    weight_text: float = 0.35
    weight_entities: float = 0.25

    @classmethod
    def from_settings(cls, settings: Settings) -> "Thresholds":
        return cls(
            near_duplicate_text=settings.processing_near_duplicate_text_threshold,
            near_duplicate_title=settings.processing_near_duplicate_title_threshold,
            title_only=settings.processing_title_only_threshold,
            title_only_window_hours=settings.processing_title_only_window_hours,
            near_duplicate_window_hours=settings.processing_near_duplicate_window_hours,
            story=settings.processing_story_threshold,
            story_window_hours=settings.processing_story_window_hours,
            min_body_tokens=settings.processing_min_body_tokens,
        )

    def as_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


@dataclass
class StagedItem:
    """One row of `ingestion_items` as Stage 3.2 sees it: Stage 3.1's fields plus earlier processing results."""

    id: uuid.UUID
    dedup_key: str
    source_key: str
    source_type: str
    title: str
    original_url: str
    canonical_url: str
    publisher: str | None
    published_at: datetime | None
    excerpt: str | None
    feed_identifier: str
    collected_at: datetime
    processing_status: str = "pending"
    extra: dict[str, Any] = field(default_factory=dict)
    # Set by earlier processing runs.
    cluster_id: uuid.UUID | None = None
    dedup_relation: str | None = None
    duplicate_of_id: uuid.UUID | None = None
    content_hash: str | None = None
    processed_at: datetime | None = None
    processing: dict[str, Any] | None = None

    @classmethod
    def from_normalized(cls, item: NormalizedItem) -> "StagedItem":
        """For dry runs: a Stage 3.1 item that was never stored, with an id derived from its dedup key."""
        return cls(
            id=uuid.uuid5(ID_NAMESPACE, f"item:{item.dedup_key}"),
            dedup_key=item.dedup_key,
            source_key=item.source_key,
            source_type=item.source_type.value,
            title=item.title,
            original_url=item.original_url,
            canonical_url=item.canonical_url,
            publisher=item.publisher,
            published_at=item.published_at,
            excerpt=item.excerpt,
            feed_identifier=item.feed_identifier,
            collected_at=item.collected_at,
            processing_status=item.processing_status.value,
            extra=dict(item.extra),
        )


@dataclass(frozen=True)
class SourceInfo:
    """What the source configuration says about a source key (name and category), when it is still configured."""

    name: str
    category: str


@dataclass
class ItemUpdate:
    """The processing result for one staged item, written back by the store."""

    id: uuid.UUID
    processing_status: str
    processed_at: datetime
    processing: dict[str, Any]
    cluster_id: uuid.UUID | None = None
    dedup_relation: str | None = None
    duplicate_of_id: uuid.UUID | None = None
    content_hash: str | None = None


@dataclass
class ProcessingReport:
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "succeeded"  # succeeded | skipped | failed
    reason: str | None = None
    dry_run: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    job_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "dryRun": self.dry_run,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "counts": self.counts,
            "jobId": self.job_id,
        }
