"""Stage 3.4 persistence against the real schema (local test PostgreSQL; skipped without TEST_DATABASE_URL)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.intelligence.quota import SqlUsageLedger
from app.ranking.store import SqlRankingStore
from tests.intelligence_helpers import INDIA, PAPER, POLICY, FakeProvider
from tests.processing_helpers import NOW
from tests.ranking_helpers import WEEK, analysed, check_reply, make_ranker, ranking_settings


@pytest.fixture
def db(engine):
    from app.db.session import Database
    from tests.conftest import make_settings

    database = Database(make_settings())
    yield database
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM story_verifications; DELETE FROM ranking_runs; DELETE FROM ai_usage_daily; "
                          "DELETE FROM ingestion_jobs WHERE source_key = 'stage-3.4-ranking'"))  # fmt: skip
    database.dispose()


def test_tables_have_rls_and_no_policies(engine):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN ('story_verifications', 'ranking_runs')"
        )).all()  # fmt: skip
        policies = conn.scalar(text(
            "SELECT count(*) FROM pg_policies WHERE tablename IN ('story_verifications', 'ranking_runs')"
        ))  # fmt: skip
    assert sorted(rows) == [("ranking_runs", True), ("story_verifications", True)]
    assert policies == 0


def test_full_run_against_postgres_is_idempotent(db):
    clusters, records = analysed([PAPER, INDIA, POLICY])
    store = SqlRankingStore(db)
    provider = FakeProvider(check_reply())
    runner, _ = make_ranker(
        clusters,
        records,
        store=store,
        provider=provider,
        ledger=SqlUsageLedger(db),
        settings=ranking_settings(ranking_ai_check=True),
    )
    first = runner.run(week=WEEK)
    assert first.status == "completed", first.reason
    calls = len(provider.requests)
    second = runner.run(week=WEEK)
    assert second.reused_run and second.run_id == first.run_id
    assert len(provider.requests) == calls  # cached checks reused from story_verifications
    runs = store.list_runs(limit=10)
    assert len(runs) == 1 and runs[0].status == "completed"
    assert runs[0].shortlist_cluster_ids == first.result.ranked_candidate_ids
    rows = store.list_verifications(now=NOW, eligibility=None, limit=10)
    assert len(rows) == 3
    assert all(r.verification and r.dimension_scores and r.ranking_run_id is not None for r in rows)
    assert {r.ai_check_status for r in rows} <= {"cached", "completed", "not_selected"}
    latest = runner.latest(include_incomplete=False)
    assert latest.ranked_candidate_ids == first.result.ranked_candidate_ids
    assert SqlUsageLedger(db).snapshot("gemini-3.5-flash-lite", NOW).requests == calls
    with db.write_session() as s:
        job = s.execute(text(
            "SELECT job_type, status FROM ingestion_jobs "
            "WHERE source_key = 'stage-3.4-ranking' ORDER BY started_at LIMIT 1"
        )).one()  # fmt: skip
    assert tuple(job) == ("verification", "succeeded")


def test_one_running_job_at_a_time(db):
    store = SqlRankingStore(db)
    assert store.start(NOW) is not None
    assert store.start(NOW) is None


def test_constraints_reject_out_of_range_scores_and_unknown_statuses(db):
    clusters, records = analysed([PAPER])
    store = SqlRankingStore(db)
    runner, _ = make_ranker(clusters, records, store=store)
    runner.run(week=WEEK)
    for sql in (
        "UPDATE story_verifications SET evidence_quality_score = 101",
        "UPDATE story_verifications SET final_priority_score = 100.5",
        "UPDATE story_verifications SET eligibility = 'published'",
        "UPDATE story_verifications SET verification_status = 'verified'",
        "UPDATE ranking_runs SET status = 'published'",
        "UPDATE ranking_runs SET edition_end = edition_start + 3",
    ):
        with pytest.raises(IntegrityError), db.write_session() as s:
            s.execute(text(sql))
