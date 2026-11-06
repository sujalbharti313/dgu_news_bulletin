"""Persistence for Stage 3.2: reads staged items, writes processing results back onto the same rows.

No new table. Results live in columns of the temporary `ingestion_items` table (migration 0003) and
are deleted with the row when it expires (Stage 3.1's retention, default 14 days). One processing run
at a time is enforced through `ingestion_jobs` (job_type `story_processing`), using the existing
one-running-job-per-source unique index, which also works through Supabase's transaction pooler.
"""

import uuid
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from sqlalchemy import select, text, update

from app.db.session import Database
from app.ingestion.store import SqlIngestionStore
from app.models.enums import IngestionItemStatus, IngestionJobStatus, IngestionJobType
from app.models.ingestion import IngestionItem
from app.processing.models import ItemUpdate, StagedItem

# The `source_key` under which processing runs are recorded in ingestion_jobs (never a configured source).
PROCESSING_JOB_KEY = "stage-3.2-processing"
_REQUIRED_COLUMNS = {"content_hash", "cluster_id", "dedup_relation", "duplicate_of_id", "processed_at", "processing"}


class SchemaNotReady(RuntimeError):
    safe_message = "Database schema is not up to date: apply migration 0003 (alembic upgrade head)."


class ProcessingStore(Protocol):
    def check_schema(self) -> None: ...
    def start(self, now: datetime) -> uuid.UUID | None: ...
    def finish(
        self, job_id: uuid.UUID, *, status: str, now: datetime, details: dict, error: str | None = None
    ) -> None: ...
    def recover_stale(self, now: datetime, stale_minutes: int) -> int: ...
    def load_items(self, now: datetime, limit: int) -> list[StagedItem]: ...
    def apply(self, updates: list[ItemUpdate]) -> int: ...


def _row_to_item(row: IngestionItem) -> StagedItem:
    return StagedItem(
        id=row.id,
        dedup_key=row.dedup_key,
        source_key=row.source_key,
        source_type=row.source_type,
        title=row.title,
        original_url=row.original_url,
        canonical_url=row.canonical_url,
        publisher=row.publisher,
        published_at=row.published_at,
        excerpt=row.excerpt,
        feed_identifier=row.feed_identifier,
        collected_at=row.collected_at,
        processing_status=row.processing_status,
        extra=dict(row.extra or {}),
        cluster_id=row.cluster_id,
        dedup_relation=row.dedup_relation,
        duplicate_of_id=row.duplicate_of_id,
        content_hash=row.content_hash,
        processed_at=row.processed_at,
        processing=row.processing,
    )


class SqlProcessingStore:
    def __init__(self, db: Database):
        self.db = db
        self.jobs = SqlIngestionStore(db)

    def check_schema(self) -> None:
        with self.db.write_session() as s:
            present = set(
                s.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'ingestion_items'"
                    )
                )
            )
        if not _REQUIRED_COLUMNS <= present:
            raise SchemaNotReady(SchemaNotReady.safe_message)

    def start(self, now):
        return self.jobs.start_job(PROCESSING_JOB_KEY, IngestionJobType.STORY_PROCESSING, now)

    def finish(self, job_id, *, status, now, details, error=None):
        self.jobs.finish_job(job_id, status=status, now=now, attempts=1, details=details, error=error)

    def recover_stale(self, now, stale_minutes):
        return self.jobs.recover_stale_jobs(now, stale_minutes)

    def load_items(self, now, limit):
        with self.db.write_session() as s:
            rows = s.scalars(
                select(IngestionItem)
                .where(
                    IngestionItem.expires_at > now,
                    IngestionItem.processing_status != IngestionItemStatus.DISCARDED,
                )
                .order_by(IngestionItem.collected_at.desc(), IngestionItem.dedup_key)
                .limit(limit)
            ).all()
            return [_row_to_item(r) for r in rows]

    def apply(self, updates):
        if not updates:
            return 0
        rows = [
            {
                "id": u.id,
                "processing_status": u.processing_status,
                "processed_at": u.processed_at,
                "processing": u.processing,
                "cluster_id": u.cluster_id,
                "dedup_relation": u.dedup_relation,
                "duplicate_of_id": u.duplicate_of_id,
                "content_hash": u.content_hash,
            }
            for u in updates
        ]
        # One transaction: a run's decisions are saved together or not at all.
        with self.db.write_session() as s:
            s.execute(update(IngestionItem), rows)
        return len(rows)


class MemoryProcessingStore:
    """Same behaviour in memory: dry runs (straight from a Stage 3.1 dry run) and tests."""

    def __init__(self, items: list[StagedItem] | None = None, *, expires: dict[uuid.UUID, datetime] | None = None):
        # Copies, so applying results never mutates the caller's objects.
        self.items: dict[uuid.UUID, StagedItem] = {i.id: _copy(i) for i in items or []}
        self.expires = expires or {}
        self.jobs: dict[uuid.UUID, dict] = {}

    def add(self, item: StagedItem) -> None:
        self.items.setdefault(item.id, _copy(item))

    def check_schema(self):
        return None

    def start(self, now):
        if any(j["status"] == IngestionJobStatus.RUNNING for j in self.jobs.values()):
            return None
        job_id = uuid.uuid4()
        self.jobs[job_id] = {"status": IngestionJobStatus.RUNNING, "started_at": now, "details": None, "error": None}
        return job_id

    def finish(self, job_id, *, status, now, details, error=None):
        self.jobs[job_id].update(status=status, completed_at=now, details=details, error=error)

    def recover_stale(self, now, stale_minutes):
        return 0

    def load_items(self, now, limit):
        live = [
            i
            for i in self.items.values()
            if i.processing_status != IngestionItemStatus.DISCARDED and self.expires.get(i.id, now) >= now
        ]
        live.sort(key=lambda i: (i.collected_at, i.dedup_key), reverse=True)
        return [_copy(i) for i in live[:limit]]

    def apply(self, updates):
        for u in updates:
            item = self.items[u.id]
            item.processing_status = u.processing_status
            item.processed_at = u.processed_at
            item.processing = u.processing
            item.cluster_id = u.cluster_id
            item.dedup_relation = u.dedup_relation
            item.duplicate_of_id = u.duplicate_of_id
            item.content_hash = u.content_hash
        return len(updates)


def _copy(item: StagedItem) -> StagedItem:
    return replace(item, extra=dict(item.extra))
