"""Persistence for Stage 3.4: `story_verifications` (one row per cluster, upserted) and `ranking_runs` (one row per
distinct ranking, upserted by `run_key`). Nothing here deletes news, analyses or earlier runs.

The run lock is an `ingestion_jobs` row (job_type `verification`, source_key `stage-3.4-ranking`) using the
existing one-running-job-per-source unique index, exactly like Stages 3.2 and 3.3.
"""

import uuid
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.session import Database
from app.ingestion.store import SqlIngestionStore
from app.models.enums import IngestionJobStatus, IngestionJobType
from app.models.ranking import RankingRun, StoryVerificationRecord

RANKING_JOB_KEY = "stage-3.4-ranking"


class SchemaNotReady(RuntimeError):
    safe_message = "Database schema is not up to date: apply migration 0006 (alembic upgrade head)."


@dataclass
class VerificationRecord:
    cluster_id: uuid.UUID
    cluster_revision: str
    input_key: str
    ranking_config_version: str
    verification_config_version: str
    edition_start: date
    verification_status: str
    eligibility: str
    evaluated_at: datetime
    expires_at: datetime
    analysis_generated_at: datetime | None = None
    source_credibility_score: int | None = None
    source_credibility_level: str | None = None
    evidence_quality_score: int | None = None
    claim_consistency_score: int | None = None
    technical_substance_score: int | None = None
    corroboration_level: str | None = None
    source_count: int = 0
    independent_source_count: int | None = None
    final_priority_score: float | None = None
    priority_classification: str | None = None
    dimension_scores: dict | None = None
    verification: dict | None = None
    review_flags: list[str] = field(default_factory=list)
    ranking_explanation: str | None = None
    rank: int | None = None
    shortlist_rank: int | None = None
    ai_check_status: str = "disabled"
    ai_check_key: str | None = None
    ai_check: dict | None = None
    ai_check_attempts: int = 0
    ai_check_next_attempt_at: datetime | None = None
    errors: list | None = None
    ranking_run_id: uuid.UUID | None = None


@dataclass
class RunRecord:
    run_key: str
    edition_start: date
    edition_end: date
    timezone: str
    status: str
    ranking_config_version: str
    verification_config_version: str
    processed_at: datetime
    result: dict
    status_reason: str | None = None
    total_evaluated: int = 0
    total_eligible: int = 0
    total_review_required: int = 0
    total_ineligible: int = 0
    total_pending: int = 0
    total_failed: int = 0
    shortlist_cluster_ids: list[uuid.UUID] = field(default_factory=list)
    errors: list | None = None
    job_id: uuid.UUID | None = None
    id: uuid.UUID | None = None


_V_FIELDS = [f.name for f in fields(VerificationRecord)]
_R_FIELDS = [f.name for f in fields(RunRecord) if f.name != "id"]


def _v_from_row(row: StoryVerificationRecord) -> VerificationRecord:
    values = {f: getattr(row, f) for f in _V_FIELDS}
    values["review_flags"] = list(values["review_flags"] or [])
    if isinstance(values["final_priority_score"], Decimal):
        values["final_priority_score"] = float(values["final_priority_score"])
    return VerificationRecord(**values)


def _r_from_row(row: RankingRun) -> RunRecord:
    values = {f: getattr(row, f) for f in _R_FIELDS}
    values["shortlist_cluster_ids"] = list(values["shortlist_cluster_ids"] or [])
    return RunRecord(**values, id=row.id)


class RankingStore(Protocol):
    def check_schema(self) -> None: ...
    def start(self, now: datetime) -> uuid.UUID | None: ...
    def finish(
        self, job_id: uuid.UUID, *, status: str, now: datetime, details: dict, error: str | None = None
    ) -> None: ...
    def recover_stale(self, now: datetime, stale_minutes: int) -> int: ...
    def load_verifications(self, cluster_ids: list[uuid.UUID]) -> dict[uuid.UUID, VerificationRecord]: ...
    def save_verification(self, record: VerificationRecord) -> None: ...
    def save_run(self, run: RunRecord) -> uuid.UUID: ...
    def find_run(self, run_key: str) -> RunRecord | None: ...
    def latest_run(
        self, *, edition_start: date | None = None, statuses: list[str] | None = None
    ) -> RunRecord | None: ...
    def list_runs(self, *, limit: int) -> list[RunRecord]: ...
    def list_verifications(
        self, *, now: datetime, eligibility: list[str] | None, limit: int
    ) -> list[VerificationRecord]: ...


class SqlRankingStore:
    def __init__(self, db: Database):
        self.db = db
        self.jobs = SqlIngestionStore(db)

    def check_schema(self) -> None:
        with self.db.write_session() as s:
            present = s.scalar(
                text(
                    "SELECT to_regclass('story_verifications') IS NOT NULL AND to_regclass('ranking_runs') IS NOT NULL "
                    "AND to_regclass('story_analyses') IS NOT NULL"
                )
            )
        if not present:
            raise SchemaNotReady(SchemaNotReady.safe_message)

    def start(self, now):
        return self.jobs.start_job(RANKING_JOB_KEY, IngestionJobType.VERIFICATION, now)

    def finish(self, job_id, *, status, now, details, error=None):
        self.jobs.finish_job(job_id, status=status, now=now, attempts=1, details=details, error=error)

    def recover_stale(self, now, stale_minutes):
        return self.jobs.recover_stale_jobs(now, stale_minutes)

    def load_verifications(self, cluster_ids):
        out: dict[uuid.UUID, VerificationRecord] = {}
        if not cluster_ids:
            return out
        with self.db.write_session() as s:
            for i in range(0, len(cluster_ids), 500):
                rows = s.scalars(
                    select(StoryVerificationRecord).where(
                        StoryVerificationRecord.cluster_id.in_(cluster_ids[i : i + 500])
                    )
                ).all()
                out.update({r.cluster_id: _v_from_row(r) for r in rows})
        return out

    def save_verification(self, record):
        values = {f: getattr(record, f) for f in _V_FIELDS}
        stmt = insert(StoryVerificationRecord).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[StoryVerificationRecord.cluster_id],
            set_={f: stmt.excluded[f] for f in _V_FIELDS if f != "cluster_id"},
        )
        with self.db.write_session() as s:
            s.execute(stmt)

    def save_run(self, run):
        values = {f: getattr(run, f) for f in _R_FIELDS}
        stmt = insert(RankingRun).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[RankingRun.run_key],
            set_={f: stmt.excluded[f] for f in _R_FIELDS if f != "run_key"},
        ).returning(RankingRun.id)
        with self.db.write_session() as s:
            return s.execute(stmt).scalar_one()

    def find_run(self, run_key):
        with self.db.write_session() as s:
            row = s.scalar(select(RankingRun).where(RankingRun.run_key == run_key))
            return _r_from_row(row) if row else None

    def latest_run(self, *, edition_start=None, statuses=None):
        q = select(RankingRun)
        if edition_start is not None:
            q = q.where(RankingRun.edition_start == edition_start)
        if statuses:
            q = q.where(RankingRun.status.in_(statuses))
        q = q.order_by(RankingRun.processed_at.desc(), RankingRun.id).limit(1)
        with self.db.write_session() as s:
            row = s.scalar(q)
            return _r_from_row(row) if row else None

    def list_runs(self, *, limit):
        q = select(RankingRun).order_by(RankingRun.processed_at.desc(), RankingRun.id).limit(limit)
        with self.db.write_session() as s:
            return [_r_from_row(r) for r in s.scalars(q).all()]

    def list_verifications(self, *, now, eligibility, limit):
        q = select(StoryVerificationRecord).where(StoryVerificationRecord.expires_at > now)
        if eligibility:
            q = q.where(StoryVerificationRecord.eligibility.in_(eligibility))
        q = q.order_by(
            StoryVerificationRecord.final_priority_score.desc().nulls_last(), StoryVerificationRecord.cluster_id
        ).limit(limit)
        with self.db.write_session() as s:
            return [_v_from_row(r) for r in s.scalars(q).all()]


class MemoryRankingStore:
    """Same behaviour in memory: dry runs and tests."""

    def __init__(self):
        self.verifications: dict[uuid.UUID, VerificationRecord] = {}
        self.runs: dict[str, RunRecord] = {}
        self.jobs: dict[uuid.UUID, dict] = {}
        self.verification_saves = 0
        self.fail_save_for: set[uuid.UUID] = set()  # tests: simulate a per-story write failure

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

    def load_verifications(self, cluster_ids):
        return {c: replace(self.verifications[c]) for c in cluster_ids if c in self.verifications}

    def save_verification(self, record):
        if record.cluster_id in self.fail_save_for:
            raise RuntimeError("simulated write failure")
        self.verification_saves += 1
        self.verifications[record.cluster_id] = replace(record)

    def save_run(self, run):
        existing = self.runs.get(run.run_key)
        run_id = existing.id if existing and existing.id else uuid.uuid4()
        self.runs[run.run_key] = replace(run, id=run_id)
        return run_id

    def find_run(self, run_key):
        run = self.runs.get(run_key)
        return replace(run) if run else None

    def latest_run(self, *, edition_start=None, statuses=None):
        runs = [
            r
            for r in self.runs.values()
            if (edition_start is None or r.edition_start == edition_start) and (not statuses or r.status in statuses)
        ]
        return replace(max(runs, key=lambda r: r.processed_at)) if runs else None

    def list_runs(self, *, limit):
        return [replace(r) for r in sorted(self.runs.values(), key=lambda r: r.processed_at, reverse=True)[:limit]]

    def list_verifications(self, *, now, eligibility, limit):
        rows = [
            r
            for r in self.verifications.values()
            if r.expires_at > now and (not eligibility or r.eligibility in eligibility)
        ]
        rows.sort(key=lambda r: (-(r.final_priority_score or -1), str(r.cluster_id)))
        return [replace(r) for r in rows[:limit]]
