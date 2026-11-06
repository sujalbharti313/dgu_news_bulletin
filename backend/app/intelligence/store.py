"""Persistence for Stage 3.3: one `story_intelligence` row per cluster, written as each story finishes.

The run lock is an `ingestion_jobs` row (job_type `summarisation`, source_key `stage-3.3-intelligence`)
using the existing one-running-job-per-source unique index, exactly like Stage 3.2.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.session import Database
from app.ingestion.store import SqlIngestionStore
from app.models.enums import IngestionJobStatus, IngestionJobType
from app.models.intelligence import StoryIntelligenceRecord

INTELLIGENCE_JOB_KEY = "stage-3.3-intelligence"


class SchemaNotReady(RuntimeError):
    safe_message = "Database schema is not up to date: apply migration 0004 (alembic upgrade head)."


@dataclass
class IntelligenceRecord:
    cluster_id: uuid.UUID
    cluster_revision: str
    engine_key: str
    status: str
    expires_at: datetime
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    analysed_at: datetime | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
    intelligence: dict | None = None
    errors: list | None = None
    review_flags: list[str] = field(default_factory=list)
    job_id: uuid.UUID | None = None
    updated_at: datetime | None = None


_FIELDS = [
    "cluster_id", "cluster_revision", "engine_key", "status", "expires_at", "attempt_count", "last_attempt_at",
    "next_attempt_at", "analysed_at", "provider", "model", "input_tokens", "output_tokens", "estimated_cost_usd",
    "intelligence", "errors", "review_flags", "job_id",
]  # fmt: skip


def _from_row(row: StoryIntelligenceRecord) -> IntelligenceRecord:
    values = {f: getattr(row, f) for f in _FIELDS}
    if isinstance(values["estimated_cost_usd"], Decimal):
        values["estimated_cost_usd"] = float(values["estimated_cost_usd"])
    values["review_flags"] = list(values["review_flags"] or [])
    return IntelligenceRecord(**values, updated_at=row.updated_at)


class IntelligenceStore(Protocol):
    def check_schema(self) -> None: ...
    def start(self, now: datetime) -> uuid.UUID | None: ...
    def finish(
        self, job_id: uuid.UUID, *, status: str, now: datetime, details: dict, error: str | None = None
    ) -> None: ...
    def recover_stale(self, now: datetime, stale_minutes: int) -> int: ...
    def load_states(self, cluster_ids: list[uuid.UUID]) -> dict[uuid.UUID, IntelligenceRecord]: ...
    def save(self, record: IntelligenceRecord) -> None: ...
    def list(
        self, *, now: datetime, statuses: list[str] | None, limit: int, updated_since: datetime | None = None
    ) -> list[IntelligenceRecord]: ...


class SqlIntelligenceStore:
    def __init__(self, db: Database):
        self.db = db
        self.jobs = SqlIngestionStore(db)

    def check_schema(self) -> None:
        with self.db.write_session() as s:
            present = s.scalar(text("SELECT to_regclass('story_intelligence') IS NOT NULL"))
        if not present:
            raise SchemaNotReady(SchemaNotReady.safe_message)

    def start(self, now):
        return self.jobs.start_job(INTELLIGENCE_JOB_KEY, IngestionJobType.SUMMARISATION, now)

    def finish(self, job_id, *, status, now, details, error=None):
        self.jobs.finish_job(job_id, status=status, now=now, attempts=1, details=details, error=error)

    def recover_stale(self, now, stale_minutes):
        return self.jobs.recover_stale_jobs(now, stale_minutes)

    def load_states(self, cluster_ids):
        if not cluster_ids:
            return {}
        out: dict[uuid.UUID, IntelligenceRecord] = {}
        with self.db.write_session() as s:
            for i in range(0, len(cluster_ids), 500):
                rows = s.scalars(
                    select(StoryIntelligenceRecord).where(
                        StoryIntelligenceRecord.cluster_id.in_(cluster_ids[i : i + 500])
                    )
                ).all()
                out.update({r.cluster_id: _from_row(r) for r in rows})
        return out

    def save(self, record):
        values = {f: getattr(record, f) for f in _FIELDS}
        stmt = insert(StoryIntelligenceRecord).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[StoryIntelligenceRecord.cluster_id],
            set_={f: stmt.excluded[f] for f in _FIELDS if f != "cluster_id"},
        )
        with self.db.write_session() as s:
            s.execute(stmt)

    def list(self, *, now, statuses, limit, updated_since=None):
        q = select(StoryIntelligenceRecord).where(StoryIntelligenceRecord.expires_at > now)
        if statuses:
            q = q.where(StoryIntelligenceRecord.status.in_(statuses))
        if updated_since is not None:
            q = q.where(StoryIntelligenceRecord.updated_at >= updated_since)
        q = q.order_by(StoryIntelligenceRecord.updated_at.desc(), StoryIntelligenceRecord.cluster_id).limit(limit)
        with self.db.write_session() as s:
            return [_from_row(r) for r in s.scalars(q).all()]


class MemoryIntelligenceStore:
    """Same behaviour in memory: dry runs and tests."""

    def __init__(self):
        self.records: dict[uuid.UUID, IntelligenceRecord] = {}
        self.jobs: dict[uuid.UUID, dict] = {}
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
