"""Stage 3.1 → 3.2 → 3.3 against a local throwaway Postgres (TEST_DATABASE_URL): the table, constraints,
upserts, the run lock, idempotency and the Stage 3.4 read path. Never touches Supabase."""

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.ingestion.store import SqlIngestionStore
from app.intelligence.runner import IntelligenceRunner
from app.intelligence.service import list_story_intelligence
from app.intelligence.store import INTELLIGENCE_JOB_KEY, IntelligenceRecord, SchemaNotReady, SqlIntelligenceStore
from app.models import IngestionJob, StoryIntelligenceRecord
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore
from tests.conftest import TEST_DATABASE_URL
from tests.intelligence_helpers import CATEGORIES, FakeProvider, ai_settings, grounded
from tests.processing_helpers import NOW
from tests.test_processing_integration import INFO, SessionDatabase, ingest

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")


def settings(**kw):
    return ai_settings(database_url=TEST_DATABASE_URL, ai_max_concurrency=1, ai_min_evidence_words=0, **kw)


def pipeline(db_session, provider):
    database = SessionDatabase(db_session)
    ingest(SqlIngestionStore(database))
    processing = ProcessingRunner(settings(), SqlProcessingStore(database), sources=INFO, clock=lambda: NOW)
    assert processing.run().status == "succeeded"
    runner = IntelligenceRunner(
        settings(),
        SqlIntelligenceStore(database),
        processing.clusters,
        provider_factory=lambda s: provider,
        categories=CATEGORIES,
        clock=lambda: NOW,
    )
    return database, processing, runner


def test_stage_31_to_33_against_postgres(db_session, monkeypatch):
    provider = FakeProvider(grounded)
    database, processing, runner = pipeline(db_session, provider)
    report = runner.run()
    assert report.status == "succeeded" and report.counts["new"] == 3, report.as_dict()
    assert len(provider.requests) == 3

    rows = db_session.scalars(select(StoryIntelligenceRecord)).all()
    assert len(rows) == 3
    clusters = {c.cluster_id: c for c in processing.clusters()}
    for row in rows:
        db_session.refresh(row)
        assert row.cluster_id in clusters and row.cluster_revision == clusters[row.cluster_id].processing.revision
        assert row.status in ("analysed", "needs_review") and row.attempt_count == 1
        assert row.input_tokens == 1000 and row.output_tokens == 400 and row.model == "test-model-2026"
        assert row.expires_at == NOW + timedelta(days=30) and row.job_id is not None
        assert "excerpt" not in str(row.intelligence)  # no article text persisted
    job = db_session.scalars(select(IngestionJob).where(IngestionJob.source_key == INTELLIGENCE_JOB_KEY)).one()
    assert job.job_type == "summarisation" and job.status == "succeeded" and job.details["usage"]["calls"] == 3

    # Idempotent: a second run makes no calls and rewrites nothing.
    assert runner.run().counts["up_to_date"] == 3 and len(provider.requests) == 3

    # Stage 3.4 reads validated contracts through the service.
    monkeypatch.setattr("app.intelligence.runner.datetime", _FrozenDatetime)
    for_34 = list_story_intelligence(settings(), database)
    assert {i.cluster_id for i in for_34} == set(clusters)


class _FrozenDatetime:
    @staticmethod
    def now(tz=None):
        return NOW


def record(cluster_id, **kw):
    values = dict(
        cluster_id=cluster_id,
        cluster_revision="r1",
        engine_key="k",
        status="failed",
        expires_at=NOW + timedelta(days=1),
        last_attempt_at=NOW,
    )
    values.update(kw)
    return IntelligenceRecord(**values)


def test_upsert_constraints_and_lock(db_session):
    import uuid

    store = SqlIntelligenceStore(SessionDatabase(db_session))
    store.check_schema()
    cid = uuid.uuid4()
    store.save(record(cid, attempt_count=1, errors=[{"code": "provider_timeout"}]))
    store.save(record(cid, attempt_count=2, cluster_revision="r2"))
    (row,) = db_session.scalars(select(StoryIntelligenceRecord)).all()
    db_session.refresh(row)
    assert (row.attempt_count, row.cluster_revision) == (2, "r2")  # one row per cluster
    assert store.load_states([cid])[cid].attempt_count == 2

    for bad in (
        record(uuid.uuid4(), status="analysed"),  # analysed without an analysis
        record(uuid.uuid4(), status="failed", intelligence={"x": 1}),  # failure with content
        record(uuid.uuid4(), status="published"),
    ):
        with pytest.raises(IntegrityError), db_session.begin_nested():
            store.save(bad)

    first = store.start(NOW)
    assert first is not None and store.start(NOW) is None  # one run at a time, across processes
    store.finish(first, status="succeeded", now=NOW, details={})
    assert store.list(now=NOW, statuses=["failed"], limit=10)[0].cluster_id == cid
    assert store.list(now=NOW + timedelta(days=2), statuses=None, limit=10) == []  # expired rows are not served


def test_table_has_row_level_security(db_session):
    enabled = db_session.scalar(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'story_intelligence'"))
    assert enabled is True


def test_schema_check_reports_missing_migration(db_session):
    db_session.execute(text("ALTER TABLE story_intelligence RENAME TO story_intelligence_old"))
    store = SqlIntelligenceStore(SessionDatabase(db_session))
    with pytest.raises(SchemaNotReady):
        store.check_schema()
    report = IntelligenceRunner(settings(), store, list, provider_factory=lambda s: FakeProvider()).run()
    assert report.status == "failed" and "0004" in report.reason
