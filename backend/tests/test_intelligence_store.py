"""Stage 3.3 persistence against the real schema (local test PostgreSQL; skipped without TEST_DATABASE_URL)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.intelligence.quota import SqlUsageLedger, next_reset
from app.intelligence.store import AnalysisRecord, SqlAnalysisStore
from tests.intelligence_helpers import PAPER, FakeProvider, clusters, grounded, make_runner
from tests.processing_helpers import NOW

MODEL = "gemini-3.5-flash-lite"


@pytest.fixture
def db(engine):
    """A Database bound to the test engine; rows are cleaned up after each test."""
    from app.db.session import Database
    from tests.conftest import make_settings

    database = Database(make_settings())
    yield database
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM story_analyses; DELETE FROM ai_usage_daily; "
                          "DELETE FROM ingestion_jobs WHERE source_key = 'stage-3.3-analysis'"))  # fmt: skip
    database.dispose()


def test_tables_have_rls_and_no_policies(engine):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN ('story_analyses', 'ai_usage_daily')"
        )).all()  # fmt: skip
        policies = conn.scalar(text(
            "SELECT count(*) FROM pg_policies WHERE tablename IN ('story_analyses', 'ai_usage_daily')"
        ))  # fmt: skip
    assert sorted(rows) == [("ai_usage_daily", True), ("story_analyses", True)]
    assert policies == 0


def test_full_run_against_postgres_and_idempotent_second_run(db):
    store = SqlAnalysisStore(db)
    provider = FakeProvider(grounded)
    runner, _ = make_runner(clusters([PAPER]), provider, store=store, ledger=SqlUsageLedger(db))
    assert runner.run().counts["completed"] == 1
    assert runner.run().counts["up_to_date"] == 1
    assert len(provider.requests) == 1
    [analysis] = runner.list()
    assert analysis.sources[0].original_url == PAPER.original_url
    rows = store.list(now=NOW, statuses=None, limit=10)
    assert rows[0].priority_level == analysis.priority_assessment.priority_level
    snap = SqlUsageLedger(db).snapshot(MODEL, NOW)
    assert snap.requests == 1 and snap.output_tokens == 400


def test_status_and_analysis_must_agree(db):
    store = SqlAnalysisStore(db)
    bad = AnalysisRecord(cluster_id=uuid.uuid4(), cluster_revision="r", engine_key="k", status="failed",
                         expires_at=NOW + timedelta(days=1), analysis={"x": 1})  # fmt: skip
    with pytest.raises(IntegrityError):
        store.save(bad)
    missing = AnalysisRecord(cluster_id=uuid.uuid4(), cluster_revision="r", engine_key="k", status="completed",
                             expires_at=NOW + timedelta(days=1))  # fmt: skip
    with pytest.raises(IntegrityError):
        store.save(missing)


def test_daily_ledger_reserves_atomically_up_to_the_limit(db):
    ledger = SqlUsageLedger(db)
    granted = [ledger.reserve(MODEL, NOW, requests_per_day=3, tokens_per_day=0) for _ in range(5)]
    assert granted == [True, True, True, False, False]
    assert ledger.snapshot(MODEL, NOW).requests == 3
    ledger.record(MODEL, NOW, input_tokens=900, output_tokens=200, outcome="ok")
    assert not ledger.reserve(MODEL, NOW, requests_per_day=0, tokens_per_day=1000)
    # A new quota day starts from zero.
    tomorrow = next_reset(NOW) + timedelta(minutes=1)
    assert ledger.reserve(MODEL, tomorrow, requests_per_day=3, tokens_per_day=1000)


def test_quota_exhaustion_is_remembered_until_the_reset(db):
    ledger = SqlUsageLedger(db)
    until = next_reset(NOW)
    ledger.mark_exhausted(MODEL, NOW, until)
    assert ledger.snapshot(MODEL, NOW + timedelta(hours=1)).quota_exhausted_until == until
    assert ledger.snapshot(MODEL, until + timedelta(seconds=1)).quota_exhausted_until is None
