"""Persistence for Stage 3.4: `story_embeddings` (one vector per analysed cluster, upserted by content hash),
`story_verifications` (one row per cluster, upserted) and `ranking_runs` (one row per distinct ranking, upserted
by `run_key`). Nothing here deletes news, analyses or earlier runs.

Similarity search runs in PostgreSQL through `match_story_embeddings` (pgvector cosine distance, bounded by
threshold, window and limit); vectors are never loaded into the application to compare them. All SQL uses bound
parameters.

The run lock is an `ingestion_jobs` row (job_type `verification`, source_key `stage-3.4-ranking`) using the
existing one-running-job-per-source unique index, exactly like Stages 3.2 and 3.3.
"""

import math
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
from app.models.ranking import RankingRun, StoryEmbedding, StoryVerificationRecord

RANKING_JOB_KEY = "stage-3.4-ranking"


class SchemaNotReady(RuntimeError):
    safe_message = "Database schema is not up to date: apply migration 0006 (alembic upgrade head)."

    def __init__(self, message: str | None = None):
        super().__init__(message or self.safe_message)
        if message:
            self.safe_message = message


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
    technical_significance_score: int | None = None
    global_impact_score: int | None = None
    student_relevance_score: int | None = None
    timeliness_score: int | None = None
    corroboration_level: str | None = None
    source_count: int = 0
    independent_source_count: int | None = None
    final_priority_score: float | None = None
    priority_classification: str | None = None
    dimension_scores: dict | None = None
    verification: dict | None = None
    related: list | None = None
    review_flags: list[str] = field(default_factory=list)
    ranking_explanation: str | None = None
    rank: int | None = None
    shortlist_rank: int | None = None
    embedding_status: str = "pending"
    embedding_attempts: int = 0
    embedding_next_attempt_at: datetime | None = None
    evaluation_status: str = "pending"
    evaluation_key: str | None = None
    evaluation: dict | None = None
    evaluation_model: str | None = None
    evaluation_attempts: int = 0
    evaluation_next_attempt_at: datetime | None = None
    errors: list | None = None
    ranking_run_id: uuid.UUID | None = None


@dataclass
class EmbeddingRecord:
    cluster_id: uuid.UUID
    cluster_revision: str
    embedding: list[float] | None
    model: str
    dimensions: int
    embedding_version: str
    content_hash: str
    input_chars: int
    first_collected_at: datetime
    last_collected_at: datetime
    embedded_at: datetime


@dataclass(frozen=True)
class SimilarStory:
    cluster_id: uuid.UUID
    similarity: float
    first_collected_at: datetime
    last_collected_at: datetime


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


_E_FIELDS = [f.name for f in fields(EmbeddingRecord)]
_EMBEDDING_KEYS = (
    "cluster_id",
    "cluster_revision",
    "model",
    "dimensions",
    "embedding_version",
    "content_hash",
    "input_chars",
    "first_collected_at",
    "last_collected_at",
    "embedded_at",
)


class RankingStore(Protocol):
    def check_schema(self, dimensions: int) -> None: ...
    def load_embeddings(self, cluster_ids: list[uuid.UUID]) -> dict[uuid.UUID, EmbeddingRecord]: ...
    def save_embedding(self, record: EmbeddingRecord) -> None: ...
    def similar(
        self,
        cluster_id: uuid.UUID,
        *,
        model: str,
        version: str,
        window_start: datetime,
        window_end: datetime,
        min_similarity: float,
        limit: int,
    ) -> list[SimilarStory]: ...
    def shortlisted_before(self, edition_start: date) -> set[uuid.UUID]: ...
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

    def check_schema(self, dimensions: int) -> None:
        with self.db.write_session() as s:
            present = s.scalar(
                text(
                    "SELECT to_regclass('story_verifications') IS NOT NULL AND to_regclass('ranking_runs') IS NOT NULL "
                    "AND to_regclass('story_analyses') IS NOT NULL AND to_regclass('story_embeddings') IS NOT NULL "
                    "AND to_regprocedure('match_story_embeddings(uuid, text, text, timestamptz, timestamptz, "
                    "double precision, integer)') IS NOT NULL"
                )
            )
            if not present:
                raise SchemaNotReady()
            column = s.scalar(
                text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = 'story_embeddings'::regclass AND attname = 'embedding'"
                )
            )
        if not column or not column.endswith(f"vector({dimensions})"):
            raise SchemaNotReady(
                f"EMBEDDING_DIMENSIONS={dimensions} does not match the database column ({column}). Set "
                "EMBEDDING_DIMENSIONS to the column size, or add a migration that changes the column and "
                "re-embeds every story."
            )

    def load_embeddings(self, cluster_ids):
        out: dict[uuid.UUID, EmbeddingRecord] = {}
        cols = [getattr(StoryEmbedding, k) for k in _EMBEDDING_KEYS]
        with self.db.write_session() as s:
            for i in range(0, len(cluster_ids), 500):
                rows = s.execute(select(*cols).where(StoryEmbedding.cluster_id.in_(cluster_ids[i : i + 500])))
                for row in rows:
                    values = dict(zip(_EMBEDDING_KEYS, row, strict=True))
                    out[values["cluster_id"]] = EmbeddingRecord(embedding=None, **values)
        return out

    def save_embedding(self, record):
        values = {f: getattr(record, f) for f in _E_FIELDS}
        stmt = insert(StoryEmbedding).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[StoryEmbedding.cluster_id],
            set_={f: stmt.excluded[f] for f in _E_FIELDS if f != "cluster_id"},
        )
        with self.db.write_session() as s:
            s.execute(stmt)

    def similar(self, cluster_id, *, model, version, window_start, window_end, min_similarity, limit):
        with self.db.write_session() as s:
            rows = s.execute(
                text(
                    "SELECT cluster_id, similarity, first_collected_at, last_collected_at FROM "
                    "match_story_embeddings(:cid, :model, :version, :start, :end, :threshold, :limit)"
                ),
                {
                    "cid": cluster_id,
                    "model": model,
                    "version": version,
                    "start": window_start,
                    "end": window_end,
                    "threshold": float(min_similarity),
                    "limit": int(limit),
                },
            ).all()
        return [SimilarStory(r[0], float(r[1]), r[2], r[3]) for r in rows]

    def shortlisted_before(self, edition_start):
        with self.db.write_session() as s:
            rows = s.execute(
                text(
                    "SELECT DISTINCT unnest(shortlist_cluster_ids) FROM ranking_runs "
                    "WHERE status = 'completed' AND edition_start < :start"
                ),
                {"start": edition_start},
            ).all()
        return {r[0] for r in rows}

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
            # The run's result document is large; give this one transaction a longer statement timeout than the
            # default (transaction-local, so it never affects other pooler clients).
            timeout = self.db.settings.db_large_write_timeout_ms
            s.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(timeout)})
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
        self.embeddings: dict[uuid.UUID, EmbeddingRecord] = {}
        self.unusable: set[uuid.UUID] = set()  # tests: analyses that retrieval must skip (invalid/deleted)
        self.dimensions = 768

    def check_schema(self, dimensions):
        if dimensions != self.dimensions:
            raise SchemaNotReady(f"EMBEDDING_DIMENSIONS={dimensions} does not match the database column")

    def load_embeddings(self, cluster_ids):
        return {c: replace(self.embeddings[c]) for c in cluster_ids if c in self.embeddings}

    def save_embedding(self, record):
        if record.embedding is None or len(record.embedding) != self.dimensions:
            raise ValueError("embedding dimension does not match the column")
        self.embeddings[record.cluster_id] = replace(record)

    def similar(self, cluster_id, *, model, version, window_start, window_end, min_similarity, limit):
        """Same rules as the SQL function (exact cosine over the filtered rows); test and dry-run use only."""
        q = self.embeddings.get(cluster_id)
        if q is None or q.model != model or q.embedding_version != version:
            return []
        out = []
        for e in self.embeddings.values():
            if e.cluster_id == cluster_id or e.cluster_id in self.unusable:
                continue
            if (e.model, e.embedding_version, e.dimensions) != (q.model, q.embedding_version, q.dimensions):
                continue
            if not (e.first_collected_at < window_end and e.last_collected_at >= window_start):
                continue
            sim = _cosine(q.embedding, e.embedding)
            if sim >= min_similarity:
                out.append(SimilarStory(e.cluster_id, sim, e.first_collected_at, e.last_collected_at))
        out.sort(key=lambda x: (-x.similarity, str(x.cluster_id)))
        return out[: max(1, min(limit, 50))]

    def shortlisted_before(self, edition_start):
        return {
            c
            for r in self.runs.values()
            if r.status == "completed" and r.edition_start < edition_start
            for c in r.shortlist_cluster_ids
        }

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


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class DryRunStore(MemoryRankingStore):
    """`scripts.rank dry-run` over Supabase: reads earlier results and embeddings from the database and keeps
    every ranking result in memory. Embeddings are the one thing it writes (a cache keyed by content hash, not an
    editorial result), so the free embedding quota a dry run spends is not spent again by the real run."""

    def __init__(self, sql: SqlRankingStore):
        super().__init__()
        self.sql = sql

    def check_schema(self, dimensions):
        self.sql.check_schema(dimensions)

    def load_embeddings(self, cluster_ids):
        return self.sql.load_embeddings(cluster_ids)

    def save_embedding(self, record):
        self.sql.save_embedding(record)

    def similar(self, cluster_id, **kwargs):
        return self.sql.similar(cluster_id, **kwargs)

    def shortlisted_before(self, edition_start):
        return self.sql.shortlisted_before(edition_start)

    def load_verifications(self, cluster_ids):
        return self.sql.load_verifications(cluster_ids)
