"""Persistence for ingestion: temporary staged items and per-source job records.

`SqlIngestionStore` writes to Postgres (Supabase) through the existing Database class. Each operation is
its own short transaction, so a failing source never holds locks or rolls back another source's work.
`MemoryIngestionStore` implements the same interface in memory, for dry runs and tests.

What is stored, and for how long:
- ingestion_items: title, links, publisher, dates, a <=600-char excerpt and small metadata of items waiting
  for the next stage. Deleted automatically once `expires_at` passes (default 14 days), at the start of
  every run and by `scripts.ingest purge`.
- ingestion_jobs: one row per source per run with counts and the incremental cursor. No content. Rows
  older than INGESTION_JOB_RETENTION_DAYS are deleted, except each source's latest success (its cursor).
Ingestion never updates or deletes stories, editions or sources; it only reads stories.canonical_url to
avoid re-staging an article that is already in the bulletin.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.db.session import Database
from app.ingestion.models import NormalizedItem
from app.models.enums import IngestionItemStatus, IngestionJobStatus
from app.models.ingestion import IngestionItem, IngestionJob
from app.models.story import Story

ERROR_SUMMARY_LIMIT = 2000


@dataclass
class SaveResult:
    inserted: int = 0
    duplicates: int = 0
    already_in_bulletin: int = 0


@dataclass
class SourceStatus:
    source_key: str
    last_status: str | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error: str | None = None
    last_counts: dict[str, Any] = field(default_factory=dict)
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    pending_items: int = 0


class IngestionStore(Protocol):
    def purge_expired_items(self, now: datetime) -> int: ...
    def purge_old_jobs(self, now: datetime, retention_days: int) -> int: ...
    def recover_stale_jobs(self, now: datetime, stale_minutes: int) -> int: ...
    def last_cursor(self, source_key: str) -> dict | None: ...
    def start_job(self, source_key: str, job_type: str, now: datetime) -> uuid.UUID | None: ...
    def finish_job(
        self, job_id: uuid.UUID, *, status: str, now: datetime, attempts: int, details: dict, error: str | None = None
    ) -> None: ...
    def record_skipped(self, source_key: str, job_type: str, reason: str, now: datetime) -> None: ...
    def save_items(self, items: list[NormalizedItem], job_id: uuid.UUID | None, expires_at: datetime) -> SaveResult: ...
    def source_statuses(self, keys: list[str]) -> dict[str, SourceStatus]: ...


def _truncate(message: str | None) -> str | None:
    if message is None:
        return None
    return message[: ERROR_SUMMARY_LIMIT - 1] + "…" if len(message) > ERROR_SUMMARY_LIMIT else message


def _unique_by_key(items: list[NormalizedItem]) -> tuple[list[NormalizedItem], int]:
    seen: set[str] = set()
    unique = []
    for item in items:
        if item.dedup_key not in seen:
            seen.add(item.dedup_key)
            unique.append(item)
    return unique, len(items) - len(unique)


class SqlIngestionStore:
    def __init__(self, db: Database):
        self.db = db

    def purge_expired_items(self, now: datetime) -> int:
        with self.db.write_session() as s:
            return s.execute(delete(IngestionItem).where(IngestionItem.expires_at <= now)).rowcount or 0

    def purge_old_jobs(self, now: datetime, retention_days: int) -> int:
        cutoff = now - timedelta(days=retention_days)
        latest_success = (
            select(IngestionJob.id)
            .where(IngestionJob.status == IngestionJobStatus.SUCCEEDED, IngestionJob.source_key.is_not(None))
            .ext(distinct_on(IngestionJob.source_key))
            .order_by(IngestionJob.source_key, IngestionJob.completed_at.desc())
        )
        with self.db.write_session() as s:
            return (
                s.execute(
                    delete(IngestionJob).where(
                        IngestionJob.created_at < cutoff,
                        IngestionJob.status != IngestionJobStatus.RUNNING,
                        IngestionJob.source_key.is_not(None),
                        IngestionJob.id.not_in(latest_success),
                    )
                ).rowcount
                or 0
            )

    def recover_stale_jobs(self, now: datetime, stale_minutes: int) -> int:
        cutoff = now - timedelta(minutes=stale_minutes)
        with self.db.write_session() as s:
            return (
                s.execute(
                    update(IngestionJob)
                    .where(IngestionJob.status == IngestionJobStatus.RUNNING, IngestionJob.started_at < cutoff)
                    .values(
                        status=IngestionJobStatus.FAILED,
                        completed_at=now,
                        error_summary=f"Abandoned: still running after {stale_minutes} minutes "
                        "(the process probably crashed or was stopped).",
                    )
                ).rowcount
                or 0
            )

    def last_cursor(self, source_key: str) -> dict | None:
        with self.db.write_session() as s:
            details = s.scalar(
                select(IngestionJob.details)
                .where(IngestionJob.source_key == source_key, IngestionJob.status == IngestionJobStatus.SUCCEEDED)
                .order_by(IngestionJob.completed_at.desc())
                .limit(1)
            )
        cursor = (details or {}).get("cursor")
        return cursor if isinstance(cursor, dict) else None

    def start_job(self, source_key: str, job_type: str, now: datetime) -> uuid.UUID | None:
        job_id = uuid.uuid4()
        try:
            with self.db.write_session() as s:
                s.execute(
                    insert(IngestionJob).values(
                        id=job_id,
                        source_key=source_key,
                        job_type=job_type,
                        status=IngestionJobStatus.RUNNING,
                        started_at=now,
                        attempt_count=0,
                    )
                )
        except IntegrityError:
            return None  # another process is already collecting this source
        return job_id

    def finish_job(self, job_id, *, status, now, attempts, details, error=None) -> None:
        with self.db.write_session() as s:
            s.execute(
                update(IngestionJob)
                .where(IngestionJob.id == job_id)
                .values(
                    status=status,
                    completed_at=now,
                    attempt_count=attempts,
                    details=details,
                    error_summary=_truncate(error),
                )
            )

    def record_skipped(self, source_key, job_type, reason, now) -> None:
        with self.db.write_session() as s:
            s.execute(
                insert(IngestionJob).values(
                    source_key=source_key,
                    job_type=job_type,
                    status=IngestionJobStatus.SKIPPED,
                    started_at=now,
                    completed_at=now,
                    error_summary=_truncate(reason),
                )
            )

    def save_items(self, items, job_id, expires_at) -> SaveResult:
        result = SaveResult()
        items, in_batch_duplicates = _unique_by_key(items)
        result.duplicates += in_batch_duplicates
        if not items:
            return result
        with self.db.write_session() as s:
            published = set(
                s.scalars(select(Story.canonical_url).where(Story.canonical_url.in_([i.canonical_url for i in items])))
            )
            fresh = [i for i in items if i.canonical_url not in published]
            result.already_in_bulletin = len(items) - len(fresh)
            if fresh:
                rows = [
                    {
                        **item.model_dump(mode="python"),
                        "source_type": item.source_type.value,
                        "processing_status": item.processing_status.value,
                        "job_id": job_id,
                        "expires_at": expires_at,
                    }
                    for item in fresh
                ]
                inserted = s.scalars(
                    pg_insert(IngestionItem)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["dedup_key"])
                    .returning(IngestionItem.dedup_key)
                ).all()
                result.inserted = len(inserted)
                result.duplicates += len(fresh) - len(inserted)
        return result

    def source_statuses(self, keys: list[str]) -> dict[str, SourceStatus]:
        statuses = {key: SourceStatus(source_key=key) for key in keys}
        if not keys:
            return statuses
        with self.db.write_session() as s:
            recent = s.execute(
                select(IngestionJob)
                .where(IngestionJob.source_key.in_(keys), IngestionJob.status != IngestionJobStatus.SKIPPED)
                .order_by(IngestionJob.source_key, IngestionJob.created_at.desc())
            ).scalars()
            for job in recent:
                status = statuses[job.source_key]
                if status.last_status is None:
                    status.last_status = job.status
                    status.last_started_at = job.started_at
                    status.last_completed_at = job.completed_at
                    status.last_error = job.error_summary
                    status.last_counts = (job.details or {}).get("counts", {})
                if job.status == IngestionJobStatus.SUCCEEDED and status.last_success_at is None:
                    status.last_success_at = job.completed_at
                if status.last_success_at is None and job.status == IngestionJobStatus.FAILED:
                    status.consecutive_failures += 1
            pending = s.execute(
                select(IngestionItem.source_key, func.count())
                .where(
                    and_(
                        IngestionItem.source_key.in_(keys),
                        IngestionItem.processing_status == IngestionItemStatus.PENDING,
                    )
                )
                .group_by(IngestionItem.source_key)
            )
            for key, count in pending:
                statuses[key].pending_items = count
        return statuses


class MemoryIngestionStore:
    """In-memory store with the same behaviour. Used by `--dry-run` and the test suite."""

    def __init__(self, published_canonical_urls: set[str] | None = None):
        self.items: dict[str, dict] = {}
        self.jobs: dict[uuid.UUID, dict] = {}
        self.published_canonical_urls = published_canonical_urls or set()

    def purge_expired_items(self, now):
        expired = [k for k, v in self.items.items() if v["expires_at"] <= now]
        for key in expired:
            del self.items[key]
        return len(expired)

    def purge_old_jobs(self, now, retention_days):
        cutoff = now - timedelta(days=retention_days)
        keep = set()
        for key in {j["source_key"] for j in self.jobs.values()}:
            successes = [
                (j["completed_at"], i)
                for i, j in self.jobs.items()
                if j["source_key"] == key and j["status"] == IngestionJobStatus.SUCCEEDED
            ]
            if successes:
                keep.add(max(successes)[1])
        old = [
            i
            for i, j in self.jobs.items()
            if j["created_at"] < cutoff and j["status"] != IngestionJobStatus.RUNNING and i not in keep
        ]
        for job_id in old:
            del self.jobs[job_id]
        return len(old)

    def recover_stale_jobs(self, now, stale_minutes):
        cutoff = now - timedelta(minutes=stale_minutes)
        count = 0
        for job in self.jobs.values():
            if job["status"] == IngestionJobStatus.RUNNING and job["started_at"] < cutoff:
                job.update(status=IngestionJobStatus.FAILED, completed_at=now, error_summary="Abandoned")
                count += 1
        return count

    def last_cursor(self, source_key):
        successes = [
            j
            for j in self.jobs.values()
            if j["source_key"] == source_key and j["status"] == IngestionJobStatus.SUCCEEDED
        ]
        if not successes:
            return None
        return (max(successes, key=lambda j: j["completed_at"])["details"] or {}).get("cursor")

    def start_job(self, source_key, job_type, now):
        if any(j["source_key"] == source_key and j["status"] == IngestionJobStatus.RUNNING for j in self.jobs.values()):
            return None
        job_id = uuid.uuid4()
        self.jobs[job_id] = {
            "source_key": source_key,
            "job_type": job_type,
            "status": IngestionJobStatus.RUNNING,
            "started_at": now,
            "created_at": now,
            "completed_at": None,
            "details": None,
            "attempt_count": 0,
            "error_summary": None,
        }
        return job_id

    def finish_job(self, job_id, *, status, now, attempts, details, error=None):
        self.jobs[job_id].update(
            status=status, completed_at=now, attempt_count=attempts, details=details, error_summary=_truncate(error)
        )

    def record_skipped(self, source_key, job_type, reason, now):
        self.jobs[uuid.uuid4()] = {
            "source_key": source_key,
            "job_type": job_type,
            "status": IngestionJobStatus.SKIPPED,
            "started_at": now,
            "created_at": now,
            "completed_at": now,
            "details": None,
            "attempt_count": 0,
            "error_summary": reason,
        }

    def save_items(self, items, job_id, expires_at):
        result = SaveResult()
        items, result.duplicates = _unique_by_key(items)
        for item in items:
            if item.canonical_url in self.published_canonical_urls:
                result.already_in_bulletin += 1
            elif item.dedup_key in self.items:
                result.duplicates += 1
            else:
                self.items[item.dedup_key] = {"item": item, "job_id": job_id, "expires_at": expires_at}
                result.inserted += 1
        return result

    def source_statuses(self, keys):
        statuses = {key: SourceStatus(source_key=key) for key in keys}
        for job in sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True):
            status = statuses.get(job["source_key"])
            if status is None or job["status"] == IngestionJobStatus.SKIPPED:
                continue
            if status.last_status is None:
                status.last_status = job["status"]
                status.last_started_at = job["started_at"]
                status.last_completed_at = job["completed_at"]
                status.last_error = job["error_summary"]
                status.last_counts = (job["details"] or {}).get("counts", {})
            if job["status"] == IngestionJobStatus.SUCCEEDED and status.last_success_at is None:
                status.last_success_at = job["completed_at"]
            if status.last_success_at is None and job["status"] == IngestionJobStatus.FAILED:
                status.consecutive_failures += 1
        for entry in self.items.values():
            if entry["item"].source_key in statuses:
                statuses[entry["item"].source_key].pending_items += 1
        return statuses
