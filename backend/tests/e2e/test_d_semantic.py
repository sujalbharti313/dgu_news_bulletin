"""Phase 4.2 Scenario D: embedding storage and semantic retrieval in pgvector (local PostgreSQL 16 + pgvector,
the same `match_story_embeddings` SQL function the ranking uses on Supabase).

Vectors are constructed so the expected cosine similarity of every pair is known exactly (cos θ), which makes
ordering, threshold and top-K checks deterministic. Whether Gemini's real embeddings put related news close
together is an integration question this file cannot answer offline (see the Stage 4.2 report, D-REAL).
"""

import math
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import StoryAnalysisRecord
from app.ranking.store import EmbeddingRecord, SqlRankingStore
from tests.conftest import TEST_DATABASE_URL, make_settings
from tests.edition_helpers import SavepointDatabase, pipeline

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")

DIMS = 768
START = datetime(2026, 11, 1, 18, 30, tzinfo=UTC)  # Monday 2 Nov 00:00 IST
END = START + timedelta(days=7)
MODEL, VERSION = "gemini-embedding-2", "v1"


def at_angle(degrees: float, axis: int = 1) -> list[float]:
    """Unit vector at `degrees` from e0 in the (e0, e_axis) plane: cosine with e0 is exactly cos(degrees)."""
    v = [0.0] * DIMS
    r = math.radians(degrees)
    v[0], v[axis] = math.cos(r), math.sin(r)
    return v


def analysis(db_session, status="completed"):
    rows, _ = pipeline()
    cid = uuid.uuid4()
    db_session.add(
        StoryAnalysisRecord(
            **{
                **rows[0],
                "cluster_id": cid,
                "status": status,
                "analysis": rows[0]["analysis"] if status in ("completed", "needs_review") else None,
                "expires_at": datetime(2027, 3, 1, tzinfo=UTC),
                "job_id": None,
            }
        )
    )
    db_session.flush()
    return cid


def embed(store, cid, vector, *, model=MODEL, version=VERSION, collected=START + timedelta(days=2), revision="r1"):
    store.save_embedding(
        EmbeddingRecord(
            cluster_id=cid,
            cluster_revision=revision,
            embedding=vector,
            model=model,
            dimensions=DIMS,
            embedding_version=version,
            content_hash=uuid.uuid4().hex * 2,
            input_chars=500,
            first_collected_at=collected,
            last_collected_at=collected,
            embedded_at=collected + timedelta(hours=1),
        )
    )


def make_store(db_session):
    settings = make_settings()
    return SqlRankingStore(SavepointDatabase(settings, db_session))


def similar(store, cid, threshold=0.0, limit=10, **kw):
    return store.similar(
        cid,
        model=MODEL,
        version=VERSION,
        window_start=kw.get("start", START - timedelta(days=28)),
        window_end=kw.get("end", END),
        min_similarity=threshold,
        limit=limit,
    )


def test_similarity_order_threshold_and_top_k(db_session):
    """E2E-D-01: neighbours come back most similar first with the exact cosine value, the story itself is never
    returned, the threshold drops weak matches and the limit caps the result."""
    store = make_store(db_session)
    query = analysis(db_session)
    embed(store, query, at_angle(0))
    angles = {10: None, 25: None, 40: None, 60: None, 85: None}
    for a in angles:
        angles[a] = analysis(db_session)
        embed(store, angles[a], at_angle(a, axis=1 + a % 5))
    got = similar(store, query)
    assert [s.cluster_id for s in got] == [angles[a] for a in sorted(angles)]
    for s, a in zip(got, sorted(angles), strict=True):
        assert s.similarity == pytest.approx(math.cos(math.radians(a)), abs=1e-5)
    assert query not in {s.cluster_id for s in got}
    above = similar(store, query, threshold=0.8)  # cos 40° = 0.766 is below
    assert [s.cluster_id for s in above] == [angles[10], angles[25]]
    assert len(similar(store, query, limit=2)) == 2
    assert len(similar(store, query, limit=10_000)) == 5  # the SQL function caps at 50 regardless


def test_retrieval_respects_model_version_window_and_analysis_status(db_session):
    """E2E-D-02: embeddings of another model or format version, stories outside the window and stories whose
    analysis is not usable (failed, quota_limited) are never retrieved as context."""
    store = make_store(db_session)
    q = analysis(db_session)
    embed(store, q, at_angle(0))
    other_model = analysis(db_session)
    embed(store, other_model, at_angle(5), model="text-embedding-other")
    other_version = analysis(db_session)
    embed(store, other_version, at_angle(5), version="v0")
    too_old = analysis(db_session)
    embed(store, too_old, at_angle(5), collected=START - timedelta(days=60))
    too_new = analysis(db_session)
    embed(store, too_new, at_angle(5), collected=END + timedelta(days=1))
    failed = analysis(db_session, status="failed")
    embed(store, failed, at_angle(5))
    ok = analysis(db_session)
    embed(store, ok, at_angle(5))
    assert [s.cluster_id for s in similar(store, q)] == [ok]


def test_missing_embedding_returns_no_context(db_session):
    """E2E-D-03: a story without an embedding yields no related stories (it is retried, not guessed)."""
    store = make_store(db_session)
    q = analysis(db_session)
    other = analysis(db_session)
    embed(store, other, at_angle(5))
    assert similar(store, q) == []


def test_one_embedding_per_story_and_regeneration_replaces_it(db_session):
    """E2E-D-04: saving a story's embedding again (changed text or model) replaces the row instead of adding a
    second one; the new vector is the one searched."""
    store = make_store(db_session)
    q, n = analysis(db_session), analysis(db_session)
    embed(store, q, at_angle(0))
    embed(store, n, at_angle(80))
    assert similar(store, q)[0].similarity == pytest.approx(math.cos(math.radians(80)), abs=1e-5)
    embed(store, n, at_angle(20), revision="r2")
    count = db_session.scalar(text("SELECT count(*) FROM story_embeddings WHERE cluster_id = :c"), {"c": n})
    assert count == 1
    assert similar(store, q)[0].similarity == pytest.approx(math.cos(math.radians(20)), abs=1e-5)


def test_dimension_mismatch_and_orphan_embeddings_are_refused_by_the_database(db_session):
    """E2E-D-05: a vector of the wrong size and an embedding for a story that has no analysis are rejected by the
    schema itself (vector(768) and the foreign key), so the table can never hold inconsistent vectors."""
    store = make_store(db_session)
    q = analysis(db_session)
    with pytest.raises((DBAPIError, IntegrityError)), db_session.begin_nested():
        embed(store, q, [0.1] * 512)
    with pytest.raises((DBAPIError, IntegrityError)), db_session.begin_nested():
        embed(store, uuid.uuid4(), at_angle(0))


def test_high_similarity_alone_never_merges_two_events(db_session):
    """E2E-D-06: two stories with near-identical embeddings (cos 0.999) are retrieved as related context, and
    nothing in storage or retrieval merges or deletes either: deciding "same event" is left to the ranking gate,
    which also requires the model's semantic judgement (tested in E2E-E-05)."""
    store = make_store(db_session)
    a, b = analysis(db_session), analysis(db_session)
    embed(store, a, at_angle(0))
    embed(store, b, at_angle(2.5))
    got = similar(store, a, threshold=0.99)
    assert [s.cluster_id for s in got] == [b]
    assert (
        db_session.scalar(text("SELECT count(*) FROM story_analyses WHERE cluster_id IN (:a, :b)"), {"a": a, "b": b})
        == 2
    )


def test_retrieval_latency_with_a_full_semester_of_embeddings(db_session):
    """E2E-D-07 (performance): with 3,000 stored embeddings (about a year of intake at today's rate, exact scan,
    no ANN index) a top-10 similarity query stays well under a second on the test machine."""
    store = make_store(db_session)
    q = analysis(db_session)
    embed(store, q, at_angle(0))
    rows, _ = pipeline()
    import random

    rnd = random.Random(42)  # noqa: S311 - test vectors, not cryptography
    ids = [uuid.uuid4() for _ in range(3000)]
    db_session.execute(
        StoryAnalysisRecord.__table__.insert(),
        [
            {
                **{k: v for k, v in rows[0].items()},
                "cluster_id": c,
                "expires_at": datetime(2027, 3, 1, tzinfo=UTC),
                "job_id": None,
            }
            for c in ids
        ],
    )
    vec = lambda: [rnd.gauss(0, 1) for _ in range(DIMS)]  # noqa: E731
    from app.models import StoryEmbedding

    db_session.execute(
        StoryEmbedding.__table__.insert(),
        [
            {
                "cluster_id": c,
                "cluster_revision": "r1",
                "embedding": vec(),
                "model": MODEL,
                "dimensions": DIMS,
                "embedding_version": VERSION,
                "content_hash": "h" * 64,
                "input_chars": 100,
                "first_collected_at": START + timedelta(days=1),
                "last_collected_at": START + timedelta(days=1),
                "embedded_at": START + timedelta(days=1),
            }
            for c in ids
        ],
    )
    similar(store, q)  # warm up
    timings = []
    for _ in range(5):
        t = time.perf_counter()
        got = similar(store, q, limit=10)
        timings.append((time.perf_counter() - t) * 1000)
    assert len(got) == 10 and all(got[i].similarity >= got[i + 1].similarity for i in range(9))
    median = sorted(timings)[2]
    print(
        f"\nE2E-D-07 similarity query, 3001 embeddings, top-10: median {median:.1f} ms, "
        f"runs {[round(t, 1) for t in timings]}"
    )
    assert median < 1000
