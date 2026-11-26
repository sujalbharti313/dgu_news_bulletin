"""Phase 4.1 audit: the whole pipeline, stage to stage, on the real schema (local test PostgreSQL only).

Unlike the per-stage tests (which start each stage from prepared fixtures), every stage here reads exactly what the
previous stage wrote to the database, through the same production wiring the scheduler uses
(`app.intelligence.service.build_runner`, `app.ranking.service.build_runner`, `app.editions.build_service`):

    RSS feeds (mocked HTTP) -> 3.1 ingestion -> 3.2 normalisation, dedup, clustering -> 3.3 analysis (fake Gemini)
    -> 3.4 embeddings in pgvector + semantic retrieval + scoring + shortlist (fake Gemini) -> 3.5 edition
    -> admin review, approval, publication over the admin API -> public API -> 3.6 cleanup -> archive intact.

No network call, no API key and no quota: the model and embeddings are test doubles. Everything runs inside one
transaction that is rolled back, so nothing is kept, and it never touches Supabase (see conftest.py).
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.editions import build_service
from app.editions.auth import VerifiedUser
from app.editions.service import Actor
from app.ingestion.runner import IngestionRunner
from app.ingestion.sources import RssSourceConfig
from app.ingestion.store import SqlIngestionStore
from app.intelligence.service import build_runner as build_analysis_runner
from app.main import create_app
from app.models import Edition, EditionItem, IngestionItem, RankingRun, Story, StoryAnalysisRecord, StoryEmbedding
from app.processing.models import SourceInfo
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore
from app.ranking.config import SourceRegistry
from app.ranking.service import build_runner as build_ranking_runner
from tests.cleanup_helpers import AFTER_HOLD, LATER, Clock, make_cleanup
from tests.conftest import TEST_DATABASE_URL
from tests.edition_helpers import ADMIN_ID, SavepointDatabase, edition_config, seed_admins
from tests.intelligence_helpers import CATEGORIES, FakeProvider, NoSleep, ai_settings, grounded
from tests.ranking_helpers import CONFIG, REGISTRY, FakeGemini, ranking_settings

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "processing"
SOURCES = [
    RssSourceConfig(key="nimbus-blog", type="rss", name="Nimbus Blog", category="official", url="https://nimbus.example.com/feed.xml"),
    RssSourceConfig(key="tech-news", type="rss", name="Tech News", category="news", url="https://technews.example.org/feed.xml"),
]  # fmt: skip
INFO = {s.key: SourceInfo(name=s.name, category=s.category) for s in SOURCES}
COLLECTED = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)  # Friday of the week Mon 2 - Sun 8 Nov 2026 (Asia/Kolkata)
WEEK_START = date(2026, 11, 2)
MONDAY = datetime(2026, 11, 9, 1, 30, tzinfo=UTC)  # Monday 07:00 IST: the week is over, its edition is due
TOKEN = "e2e-admin-token.header.signature"
# The two fictional test sites, recorded in the registry the way an editor would after reviewing them.
TEST_REGISTRY = SourceRegistry.model_validate({
    **REGISTRY.model_dump(),
    "version": "e2e",
    "sources": [*REGISTRY.model_dump()["sources"],
                {"domain": "nimbus.example.com", "kind": "official_channel", "level": "primary",
                 "reviewed_on": "2026-11-26", "notes": "Fictional test lab."},
                {"domain": "technews.example.org", "kind": "news_outlet", "level": "established",
                 "reviewed_on": "2026-11-26", "notes": "Fictional test outlet."}],
})  # fmt: skip


def feeds(request: httpx.Request) -> httpx.Response:
    name = "lab_feed.xml" if request.url.host == "nimbus.example.com" else "news_feed.xml"
    return httpx.Response(200, content=(FIXTURES / name).read_bytes())


class Verifier:
    def verify(self, token):
        return VerifiedUser(ADMIN_ID, "editor@example.com") if token == TOKEN else None


def test_news_flows_from_feed_to_published_archive_and_survives_cleanup(db_session):
    # Staged items are kept 90 days so they are still "live" when this test runs long after the fixture dates.
    settings = ranking_settings(database_url=TEST_DATABASE_URL, supabase_url="https://project.supabase.co",
                                ingestion_item_retention_days=90,
                                supabase_publishable_key="sb_publishable_test")  # fmt: skip
    db = SavepointDatabase(settings, db_session)

    # 1. News enters the system (Stage 3.1): two feeds, one item appears in both.
    from tests.ingestion_helpers import make_http

    ingest = IngestionRunner(
        settings, SqlIngestionStore(db), sources=SOURCES, http_factory=lambda: make_http(feeds), clock=lambda: COLLECTED
    )
    assert ingest.run().status == "succeeded"
    staged = db_session.scalar(select(func.count()).select_from(IngestionItem))
    assert staged >= 4
    assert ingest.run().sources[0].counts["inserted"] == 0  # incremental: a second run adds nothing

    # 2. Normalised, deduplicated and clustered (Stage 3.2), read back from the staged rows.
    processing = ProcessingRunner(settings, SqlProcessingStore(db), sources=INFO, clock=lambda: COLLECTED)
    report = processing.run()
    assert report.status == "succeeded", report.as_dict()
    clusters = processing.clusters(limit=100)
    launch = next(c for c in clusters if c.title == "Introducing Nimbus 2.5, our most capable model")
    assert launch.duplicate_count == 1 and len(clusters) < staged  # the same-URL copy is merged, not counted twice
    assert all(r.original_url.startswith("https://") for r in launch.sources)  # every original link is kept

    # 3. Analysed (Stage 3.3) through the production wiring, with a fake Gemini; idempotent on a rerun.
    analysis_provider = FakeProvider(grounded)
    analysis = build_analysis_runner(
        settings,
        db,
        provider_factory=lambda s: analysis_provider,
        categories=CATEGORIES,
        clock=lambda: COLLECTED,
        sleep=NoSleep(),
        alert_hook=lambda alert: None,
    )
    first = analysis.run()
    assert first.counts.get("completed", 0) >= 1, first.as_dict()
    calls = len(analysis_provider.requests)
    assert analysis.run().counts.get("up_to_date", 0) >= 1 and len(analysis_provider.requests) == calls
    analysed = db_session.scalar(select(func.count()).select_from(StoryAnalysisRecord))
    assert analysed == len(clusters) - first.counts.get("insufficient_evidence", 0)

    # 4-6. Embedded into pgvector, related stories retrieved semantically, scored and ranked (Stage 3.4).
    ranking_provider = FakeGemini()
    ranker = build_ranking_runner(
        settings,
        db,
        provider_factory=lambda s: ranking_provider,
        config=CONFIG,
        registry=TEST_REGISTRY,
        clock=lambda: MONDAY,
        sleep=NoSleep(),
        alert_hook=lambda alert: None,
    )
    ranking = ranker.run(week="previous")
    assert ranking.status == "completed", ranking.as_dict()
    assert db_session.scalar(select(func.count()).select_from(StoryEmbedding)) >= analysed
    run = db_session.scalar(select(RankingRun).order_by(RankingRun.created_at.desc()))
    assert run.status == "completed" and run.edition_start == WEEK_START
    assert ranking_provider.embedded_texts >= analysed
    shortlist = [uuid.UUID(str(c)) for c in run.shortlist_cluster_ids]
    assert shortlist, [(c.get("storyClusterId"), c.get("eligibilityStatus")) for c in run.result["candidates"]]

    # 7. The weekly edition is generated from that ranking (Stage 3.5), no more than the shortlist, no padding.
    seed_admins(db_session)
    editions = build_service(
        settings, db, config=edition_config(), composer_factory=lambda: None, rank_week=None, clock=lambda: MONDAY
    )
    generated = editions.generate_week(WEEK_START, actor=Actor.scheduler())
    assert generated.status == "generated" and generated.edition_status in ("review_required", "draft"), generated
    again = editions.generate_week(WEEK_START, actor=Actor.scheduler())  # idempotent: the same edition, no copy
    assert again.edition_id == generated.edition_id
    assert db_session.scalar(select(func.count()).select_from(Edition).where(Edition.is_demo.is_(False))) == 1
    eid = str(generated.edition_id)

    # 8-10. An admin reviews, approves the exact revision and publishes it, over the admin API.
    app = create_app(settings, database=db)
    app.state.admin_token_verifier = Verifier()
    app.state.edition_service_factory = lambda s, d: editions
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get(f"/api/v1/admin/editions/{eid}").status_code == 401  # signed out: refused
    detail = client.get(f"/api/v1/admin/editions/{eid}", headers=auth).json()
    stories = detail["content"]["stories"]
    assert 1 <= len(stories) <= min(10, len(shortlist))
    assert {uuid.UUID(s["cluster_id"]) for s in stories} <= set(shortlist)
    if detail["status"] == "draft":
        assert client.post(f"/api/v1/admin/editions/{eid}/submit", headers=auth,
                           json={"expectedRevision": detail["currentRevision"]}).status_code == 200  # fmt: skip
        detail = client.get(f"/api/v1/admin/editions/{eid}", headers=auth).json()
    assert client.get("/api/v1/editions/current").status_code == 404  # nothing public before publication
    revision = detail["currentRevision"]
    approved = client.post(f"/api/v1/admin/editions/{eid}/approve", headers=auth,
                           json={"expectedRevision": revision, "contentHash": detail["contentHash"]})  # fmt: skip
    assert approved.status_code == 200, approved.json()
    published = client.post(f"/api/v1/admin/editions/{eid}/publish", headers=auth, json={"expectedRevision": revision})
    assert published.status_code == 200, published.json()
    repeat = client.post(f"/api/v1/admin/editions/{eid}/publish", headers=auth, json={"expectedRevision": revision})
    assert repeat.json().get("alreadyPublished") is True  # a double click publishes nothing twice

    # 11. Public users see the published edition, with the original source links.
    public = client.get("/api/v1/editions/current")
    assert public.status_code == 200
    body = public.json()
    source_urls = {u for c in clusters for u in (r.original_url for r in c.sources)}
    assert len(body["stories"]) == len(stories)
    assert all(s["sourceUrl"] in source_urls for s in body["stories"])

    # 12. Cleanup runs (Stage 3.6) after its safety delay and manifest hold. By then Stage 3.1 has purged the staged
    # items (14-day retention in production; this test kept them longer), so unselected stories are no longer
    # "still in the pipeline" and their working data may go.
    selected = set(db_session.scalars(select(EditionItem.cluster_id).where(EditionItem.edition_id == uuid.UUID(eid))))
    unselected = set(db_session.scalars(select(StoryAnalysisRecord.cluster_id))) - selected
    assert unselected  # at least one analysed story was not selected
    db_session.execute(IngestionItem.__table__.delete())
    db_session.execute(Edition.__table__.update().where(Edition.id == uuid.UUID(eid)).values(
        published_at=MONDAY))  # fmt: skip
    cleanup = make_cleanup(db_session, clock=Clock(LATER))
    cleanup.scheduled_run()
    cleanup.test_clock.now = AFTER_HOLD
    cleaned = cleanup.scheduled_run()
    assert cleaned.status == "ok", cleaned.as_dict()
    assert cleaned.deleted >= len(unselected)
    assert not set(db_session.scalars(select(StoryAnalysisRecord.cluster_id))) & unselected
    assert not set(db_session.scalars(select(StoryEmbedding.cluster_id))) & unselected

    # 13. The published archive is intact: same public edition, stories, links and selection.
    assert client.get("/api/v1/editions/current").json() == body
    story_rows = db_session.scalars(select(Story).where(Story.edition_id == uuid.UUID(eid))).all()
    assert len(story_rows) == len(stories) and all(s.original_url for s in story_rows)
    assert set(db_session.scalars(select(EditionItem.cluster_id).where(EditionItem.edition_id == uuid.UUID(eid)))) == (
        selected
    )
    assert selected <= set(db_session.scalars(select(StoryAnalysisRecord.cluster_id)))  # selected analyses kept
    assert db_session.get(RankingRun, run.id) is not None


def test_settings_helper_is_offline():
    """Guard for the test above: its settings never carry a real key or a remote database."""
    s = ai_settings()
    assert s.gemini_api_key.get_secret_value().startswith("AIza-test") and s.database_url is None
    assert MONDAY - timedelta(days=7) < COLLECTED < MONDAY
