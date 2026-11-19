"""Stage 3.4 persistence against the real schema (local test PostgreSQL; skipped without TEST_DATABASE_URL)."""

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.intelligence.quota import SqlUsageLedger
from app.intelligence.store import SqlAnalysisStore
from app.ranking.store import SchemaNotReady, SqlRankingStore
from tests.intelligence_helpers import INDIA, LAB_POST, PAPER, POLICY
from tests.processing_helpers import NOW
from tests.ranking_helpers import WEEK, FakeGemini, analysed, evaluator, make_ranker


@pytest.fixture
def db(engine):
    from app.db.session import Database
    from tests.conftest import make_settings

    database = Database(make_settings())
    yield database
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM story_verifications; DELETE FROM ranking_runs; DELETE FROM story_embeddings; "
                          "DELETE FROM story_analyses; DELETE FROM ai_usage_daily; "
                          "DELETE FROM ingestion_jobs WHERE source_key = 'stage-3.4-ranking'"))  # fmt: skip
    database.dispose()


def persisted(db, items):
    """Clusters and analyses, with the analyses saved in Postgres (story_embeddings references them)."""
    clusters, records = analysed(items)
    store = SqlAnalysisStore(db)
    for r in records:
        store.save(replace(r, job_id=None))
    return clusters, records


def test_tables_have_rls_and_no_policies(engine):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname, relrowsecurity FROM pg_class "
            "WHERE relname IN ('story_verifications', 'ranking_runs', 'story_embeddings') AND relkind = 'r'"
        )).all()  # fmt: skip
        policies = conn.scalar(text(
            "SELECT count(*) FROM pg_policies "
            "WHERE tablename IN ('story_verifications', 'ranking_runs', 'story_embeddings')"
        ))  # fmt: skip
    assert sorted(rows) == [("ranking_runs", True), ("story_embeddings", True), ("story_verifications", True)]
    assert policies == 0


def test_full_run_against_postgres_is_idempotent(db):
    clusters, records = persisted(db, [PAPER, INDIA, POLICY])
    store = SqlRankingStore(db)
    provider = FakeGemini(evaluator())
    runner, _ = make_ranker(clusters, records, store=store, provider=provider, ledger=SqlUsageLedger(db))
    first = runner.run(week=WEEK)
    assert first.status == "completed", first.reason
    calls = len(provider.requests)
    second = runner.run(week=WEEK)
    assert second.reused_run and second.run_id == first.run_id
    assert len(provider.requests) == calls  # cached evaluations reused from story_verifications
    assert provider.embedded_texts == 3  # cached embeddings reused from story_embeddings
    runs = store.list_runs(limit=10)
    assert len(runs) == 1 and runs[0].status == "completed"
    assert runs[0].shortlist_cluster_ids == first.result.ranked_candidate_ids
    rows = store.list_verifications(now=NOW, eligibility=None, limit=10)
    assert len(rows) == 3
    assert all(r.verification and r.dimension_scores and r.ranking_run_id is not None for r in rows)
    assert {r.evaluation_status for r in rows} <= {"cached", "completed"}
    latest = runner.latest(include_incomplete=False)
    assert latest.ranked_candidate_ids == first.result.ranked_candidate_ids
    assert SqlUsageLedger(db).snapshot("gemini-3.5-flash-lite", NOW).requests == calls
    assert SqlUsageLedger(db).snapshot("gemini-embedding-2", NOW).requests == 3
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
    clusters, records = persisted(db, [PAPER])
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


def test_similarity_search_runs_in_the_database(db):
    clusters, records = persisted(db, [LAB_POST, PAPER, INDIA])
    store = SqlRankingStore(db)
    same = FakeGemini(evaluator(), vector=lambda t: [1.0] * 768)
    runner, _ = make_ranker(clusters, records, store=store, provider=same)
    runner.run(week=WEEK)
    ids = [c.cluster_id for c in clusters]
    kwargs = dict(
        model="gemini-embedding-2", version="emb-1", window_start=NOW - timedelta(days=30),
        window_end=NOW + timedelta(days=1), min_similarity=0.5, limit=10,
    )  # fmt: skip
    hits = store.similar(ids[0], **kwargs)
    assert {h.cluster_id for h in hits} == set(ids[1:])  # never the story itself
    assert all(abs(h.similarity - 1.0) < 1e-6 for h in hits)
    assert len(store.similar(ids[0], **{**kwargs, "limit": 1})) == 1
    assert store.similar(ids[0], **{**kwargs, "model": "other-model"}) == []
    assert store.similar(ids[0], **{**kwargs, "window_end": NOW - timedelta(days=20)}) == []
    assert len(store.similar(ids[0], **{**kwargs, "limit": 10_000})) <= 50  # bounded in SQL


def test_embedding_column_and_dimension_check(db, engine):
    store = SqlRankingStore(db)
    store.check_schema(768)
    with pytest.raises(SchemaNotReady, match="EMBEDDING_DIMENSIONS=1536"):
        store.check_schema(1536)
    with engine.connect() as conn:
        column = conn.scalar(text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'story_embeddings'::regclass AND attname = 'embedding'"
        ))  # fmt: skip
    assert column.endswith("vector(768)")


def test_similarity_function_is_not_executable_by_public_roles(engine):
    with engine.connect() as conn:
        acl = conn.scalar(text(
            "SELECT proacl::text FROM pg_proc WHERE proname = 'match_story_embeddings'"
        ))  # fmt: skip
        definer = conn.scalar(text("SELECT prosecdef FROM pg_proc WHERE proname = 'match_story_embeddings'"))
    assert acl is not None and "=X/" not in acl.replace("tester=X/", "")  # no PUBLIC execute grant
    assert "anon" not in acl and "authenticated" not in acl
    assert definer is False  # SECURITY INVOKER: callers keep their own (RLS-restricted) rights


def test_similarity_parameters_are_bound_not_interpolated(db):
    store = SqlRankingStore(db)
    hits = store.similar(
        uuid.uuid4(), model="x'; DROP TABLE story_embeddings; --", version="emb-1",
        window_start=NOW - timedelta(days=1), window_end=NOW, min_similarity=0.5, limit=5,
    )  # fmt: skip
    assert hits == []
    store.check_schema(768)  # the table is still there


def test_embedding_rows_reject_wrong_dimensions(db):
    with pytest.raises(Exception), db.write_session() as s:  # noqa: B017 - any DB error
        s.execute(text(
            "INSERT INTO story_embeddings (cluster_id, cluster_revision, embedding, model, dimensions, "
            "embedding_version, content_hash, input_chars, first_collected_at, last_collected_at, embedded_at) "
            "VALUES (gen_random_uuid(), 'r', '[1,2,3]', 'm', 3, 'v', 'h', 1, now(), now(), now())"
        ))  # fmt: skip
