"""Persistence for Stage 3.3: one `story_analyses` row per cluster, written as each story finishes.

The run lock is an `ingestion_jobs` row (job_type `summarisation`, source_key `stage-3.3-analysis`) using the
existing one-running-job-per-source unique index, exactly like Stage 3.2. Nothing here deletes source records.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.session import Database
from app.ingestion.store import SqlIngestionStore
from app.models.enums import IngestionJobStatus, IngestionJobType
from app.models.intelligence import StoryAnalysisRecord

ANALYSIS_JOB_KEY = "stage-3.3-analysis"


class SchemaNotReady(RuntimeError):
    safe_message = "Database schema is not up to date: apply migration 0005 (alembic upgrade head)."


@dataclass
class AnalysisRecord:
    cluster_id: uuid.UUID
    cluster_revision: str
    engine_key: str
    status: str
    expires_at: datetime
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    analysed_at: datetime | None = None
    model_id: str | None = None
    primary_category: str | None = None
    priority_score: int | None = None
    priority_level: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    analysis: dict | None = None
    errors: list | None = None
    review_flags: list[str] = field(default_factory=list)
    job_id: uuid.UUID | None = None
    updated_at: datetime | None = None


_FIELDS = [
    "cluster_id",
    "cluster_revision",
    "engine_key",
    "status",
    "expires_at",
    "attempt_count",
    "last_attempt_at",
    "next_attempt_at",
    "analysed_at",
    "model_id",
    "primary_category",
    "priority_score",
    "priority_level",
    "input_tokens",
    "output_tokens",
    "analysis",
    "errors",
    "review_flags",
    "job_id",
]


def _from_row(row: StoryAnalysisRecord) -> AnalysisRecord:
    values = {f: getattr(row, f) for f in _FIELDS}
    values["review_flags"] = list(values["review_flags"] or [])
    return AnalysisRecord(**values, updated_at=row.updated_at)


class AnalysisStore(Protocol):
    def check_schema(self) -> None: ...
    def start(self, now: datetime) -> uuid.UUID | None: ...
    def finish(
        self, job_id: uuid.UUID, *, status: str, now: datetime, details: dict, error: str | None = None
    ) -> None: ...
    def record_skipped(self, reason: str, now: datetime) -> None: ...
    def recover_stale(self, now: datetime, stale_minutes: int) -> int: ...
    def load_states(self, cluster_ids: list[uuid.UUID]) -> dict[uuid.UUID, AnalysisRecord]: ...
    def save(self, record: AnalysisRecord) -> None: ...
    def list(
        self, *, now: datetime, statuses: list[str] | None, limit: int, updated_since: datetime | None = None
    ) -> list[AnalysisRecord]: ...


class SqlAnalysisStore:
    def __init__(self, db: Database):
        self.db = db
        self.jobs = SqlIngestionStore(db)

    def check_schema(self) -> None:
        with self.db.write_session() as s:
            present = s.scalar(
                text("SELECT to_regclass('story_analyses') IS NOT NULL AND to_regclass('ai_usage_daily') IS NOT NULL")
            )
        if not present:
            raise SchemaNotReady(SchemaNotReady.safe_message)

    def start(self, now):
        return self.jobs.start_job(ANALYSIS_JOB_KEY, IngestionJobType.SUMMARISATION, now)

    def finish(self, job_id, *, status, now, details, error=None):
        self.jobs.finish_job(job_id, status=status, now=now, attempts=1, details=details, error=error)

    def record_skipped(self, reason, now):
        self.jobs.record_skipped(ANALYSIS_JOB_KEY, IngestionJobType.SUMMARISATION, reason, now)

    def recover_stale(self, now, stale_minutes):
        return self.jobs.recover_stale_jobs(now, stale_minutes)

    def load_states(self, cluster_ids):
        if not cluster_ids:
            return {}
        out: dict[uuid.UUID, AnalysisRecord] = {}
        with self.db.write_session() as s:
            for i in range(0, len(cluster_ids), 500):
                rows = s.scalars(
                    select(StoryAnalysisRecord).where(StoryAnalysisRecord.cluster_id.in_(cluster_ids[i : i + 500]))
                ).all()
                out.update({r.cluster_id: _from_row(r) for r in rows})
        return out

    def save(self, record):
        values = {f: getattr(record, f) for f in _FIELDS}
        stmt = insert(StoryAnalysisRecord).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[StoryAnalysisRecord.cluster_id],
            set_={f: stmt.excluded[f] for f in _FIELDS if f != "cluster_id"},
        )
        with self.db.write_session() as s:
            s.execute(stmt)

    def list(self, *, now, statuses, limit, updated_since=None):
        q = select(StoryAnalysisRecord).where(StoryAnalysisRecord.expires_at > now)
        if statuses:
            q = q.where(StoryAnalysisRecord.status.in_(statuses))
        if updated_since is not None:
            q = q.where(StoryAnalysisRecord.updated_at >= updated_since)
        q = q.order_by(StoryAnalysisRecord.updated_at.desc(), StoryAnalysisRecord.cluster_id).limit(limit)
        with self.db.write_session() as s:
            return [_from_row(r) for r in s.scalars(q).all()]


class MemoryAnalysisStore:
    """Same behaviour in memory: dry runs and tests."""

    def __init__(self):
        self.records: dict[uuid.UUID, AnalysisRecord] = {}
        self.jobs: dict[uuid.UUID, dict] = {}
        self.skipped: list[str] = []
        self.saves = 0

    def check_schema(self):
        return None

    def start(self, now):
        if any(j["status"] == IngestionJobStatus.RUNNING for j in self.jobs.values()):
            return None
        job_id = uuid.uuid4()
        self.jobs[job_id] = {"status": IngestionJobStatus.RUNNING, "started_at": now, "details": None}
        return job_id

    def finish(self, job_id, *, status, now, details, error=None):
        self.jobs[job_id].update(status=status, completed_at=now, details=details, error=error)

    def record_skipped(self, reason, now):
        self.skipped.append(reason)

    def recover_stale(self, now, stale_minutes):
        return 0

    def load_states(self, cluster_ids):
        return {cid: replace(self.records[cid]) for cid in cluster_ids if cid in self.records}

    def save(self, record):
        self.saves += 1
        self.records[record.cluster_id] = replace(record, updated_at=record.last_attempt_at)

    def list(self, *, now, statuses, limit, updated_since=None):
        rows = [
            r
            for r in self.records.values()
            if r.expires_at > now
            and (not statuses or r.status in statuses)
            and (updated_since is None or (r.updated_at and r.updated_at >= updated_since))
        ]
        rows.sort(key=lambda r: (r.updated_at or now, str(r.cluster_id)), reverse=True)
        return [replace(r) for r in rows[:limit]]
