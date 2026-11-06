"""Stage 3.1 → Stage 3.2 end to end: mocked feeds, real ingestion runner and stores, processing, clusters.

In-memory tests always run; the Postgres tests need TEST_DATABASE_URL (local throwaway database)."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.session import Database
from app.ingestion.models import RawItem
from app.ingestion.normalize import normalize_item
from app.ingestion.runner import IngestionRunner
from app.ingestion.sources import RssSourceConfig
from app.ingestion.store import MemoryIngestionStore, SqlIngestionStore
from app.models import IngestionItem, IngestionJob
from app.processing.models import SourceInfo
from app.processing.runner import ProcessingRunner
from app.processing.store import PROCESSING_JOB_KEY, MemoryProcessingStore, SqlProcessingStore
from tests.conftest import TEST_DATABASE_URL, make_settings
from tests.ingestion_helpers import make_http
from tests.processing_helpers import staged_from_memory

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "processing"
SOURCES = [
    RssSourceConfig(key="nimbus-blog", type="rss", name="Nimbus Blog", category="official", url="https://nimbus.example.com/feed.xml"),
    RssSourceConfig(key="tech-news", type="rss", name="Tech News", category="news", url="https://technews.example.org/feed.xml"),
]  # fmt: skip
INFO = {s.key: SourceInfo(name=s.name, category=s.category) for s in SOURCES}


def handler(request: httpx.Request) -> httpx.Response:
    name = "lab_feed.xml" if request.url.host == "nimbus.example.com" else "news_feed.xml"
    return httpx.Response(200, content=(FIXTURES / name).read_bytes())


def ingest(store) -> None:
    runner = IngestionRunner(
        make_settings(database_url=None),
        store,
        sources=SOURCES,
        http_factory=lambda: make_http(handler),
        clock=lambda: NOW,
    )
    report = runner.run()
    assert report.status == "succeeded", report.as_dict()


def check_clusters(clusters) -> None:
    by_title = {c.title: c for c in clusters}
    launch = by_title["Introducing Nimbus 2.5, our most capable model"]
    # The lab's post, the news report of it, and the news feed's link back to the lab post (same URL).
    assert sorted(r.relation for r in launch.sources) == ["primary", "related_report", "same_url"]
    assert launch.article_count == 2 and launch.source_count == 3 and launch.duplicate_count == 1
    assert launch.representative.publisher == "Nimbus Labs Blog"  # "- RSS" suffix removed
    assert {r.source_kind for r in launch.sources} == {"official_channel", "news_outlet"}
    assert "71.2% on SWE-bench Verified" in launch.description
    assert '"the model can still produce incorrect citations"' in launch.description
    assert "nimbus 2.5" in launch.entities.products and "SWE-bench" in launch.entities.benchmarks
    sighting = next(r for r in launch.sources if r.relation == "same_url")
    assert sighting.source_key == "tech-news" and "utm_campaign" in sighting.original_url
    # The pricing change and the safety report about the same model are separate stories.
    assert by_title["Nimbus Labs cuts API prices for Nimbus 2.5 by half"].event_type == "pricing"
    assert by_title["Nimbus 2.5 safety report and system card"].event_type == "safety"
    assert len(clusters) == 3


def test_stage_31_items_flow_into_processing_in_memory():
    ingestion_store = MemoryIngestionStore()
    ingest(ingestion_store)
    ingest(ingestion_store)  # a second run re-collects the same feeds: nothing new, no new sightings
    items = staged_from_memory(ingestion_store)
    assert len(items) == 4
    runner = ProcessingRunner(
        make_settings(database_url=None), MemoryProcessingStore(items), sources=INFO, clock=lambda: NOW
    )
    report = runner.run()
    assert report.status == "succeeded" and report.counts["processed"] == 4
    check_clusters(runner.clusters())
    assert runner.run().counts["to_process"] == 0  # idempotent


def test_processing_lock_skips_a_concurrent_run():
    store = MemoryProcessingStore([])
    store.start(NOW)
    report = ProcessingRunner(make_settings(database_url=None), store, sources=INFO, clock=lambda: NOW).run()
    assert report.status == "skipped" and report.reason == "already running in another process"


def test_memory_ingestion_store_records_other_sources_as_sightings():
    store = MemoryIngestionStore()

    def item(key, feed, url="https://news.example.com/a?utm_source=x"):
        raw = RawItem(title="A fictional item", url=url, feed_identifier=feed)
        return normalize_item(raw, source_key=key, source_type="rss", collected_at=NOW)

    store.save_items([item("feed-a", "https://a.example.com/rss")], None, NOW + timedelta(days=1))
    again = store.save_items([item("feed-a", "https://a.example.com/rss")], None, NOW + timedelta(days=1))
    other = store.save_items(
        [
            item("feed-b", "https://b.example.com/rss"),
            item("feed-b", "https://b.example.com/rss", "https://news.example.com/a"),
        ],
        None,
        NOW + timedelta(days=1),
    )
    assert (again.duplicates, again.sightings) == (1, 0)
    assert (other.duplicates, other.sightings) == (2, 1)
    (entry,) = store.items.values()
    assert [s["source_key"] for s in entry["extra"]["also_seen"]] == ["feed-b"]


# --- Postgres ----------------------------------------------------------------------------------------

db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")


class SessionDatabase(Database):
    def __init__(self, session):
        super().__init__(make_settings(), engine=session.get_bind().engine)
        self._session = session

    @contextmanager
    def write_session(self):
        with self._session.begin_nested():
            yield self._session


@db
def test_stage_31_to_32_against_postgres(db_session):
    database = SessionDatabase(db_session)
    ingest(SqlIngestionStore(database))
    ingest(SqlIngestionStore(database))
    rows = db_session.scalars(select(IngestionItem)).all()
    assert len(rows) == 4
    lab = next(r for r in rows if r.canonical_url == "https://nimbus.example.com/blog/nimbus-2-5")
    assert [s["source_key"] for s in lab.extra["also_seen"]] == ["tech-news"]  # recorded once, not per run

    runner = ProcessingRunner(make_settings(), SqlProcessingStore(database), sources=INFO, clock=lambda: NOW)
    report = runner.run()
    assert report.status == "succeeded" and report.counts["written"] == 4
    check_clusters(runner.clusters())

    for row in db_session.scalars(select(IngestionItem)):
        db_session.refresh(row)
        assert row.processing_status == "processed" and row.cluster_id and row.dedup_relation
        assert "excerpt" not in (row.processing or {})  # decisions only, no article text
    job = db_session.scalars(select(IngestionJob).where(IngestionJob.source_key == PROCESSING_JOB_KEY)).one()
    assert job.job_type == "story_processing" and job.status == "succeeded"

    again = runner.run()
    assert again.counts["to_process"] == 0 and again.counts["written"] == 0


@db
def test_sql_lock_and_processed_rows_need_a_cluster(db_session):
    from sqlalchemy.exc import IntegrityError

    database = SessionDatabase(db_session)
    store = SqlProcessingStore(database)
    assert store.start(NOW) is not None
    assert store.start(NOW) is None  # one processing run at a time, across processes
    ingest(SqlIngestionStore(database))
    row = db_session.scalars(select(IngestionItem)).first()
    with pytest.raises(IntegrityError), db_session.begin_nested():
        row.processing_status = "processed"
        db_session.flush()


@db
def test_schema_check_reports_missing_migration(db_session, monkeypatch):
    import app.processing.store as store_module

    monkeypatch.setattr(store_module, "_REQUIRED_COLUMNS", {"cluster_id", "not_a_column"})
    report = ProcessingRunner(make_settings(), SqlProcessingStore(SessionDatabase(db_session)), sources=INFO).run()
    assert report.status == "failed" and "migration 0003" in report.reason


def test_cli_dry_run_needs_no_database(monkeypatch, capsys):
    import scripts.process as cli

    def fake_runner(settings, store, dry_run):
        return IngestionRunner(
            settings,
            store,
            sources=SOURCES,
            http_factory=lambda: make_http(handler),
            clock=lambda: NOW,
            dry_run=dry_run,
        )

    monkeypatch.setattr(cli, "IngestionRunner", fake_runner)
    monkeypatch.setattr(cli, "Settings", lambda: make_settings(database_url=None))
    assert cli.main(["dry-run", "--show", "5"]) == 0
    out = capsys.readouterr().out
    assert "3 story cluster(s)" in out and "Introducing Nimbus 2.5" in out and "[related_report]" in out


def test_threshold_settings_are_validated():
    from pydantic import ValidationError

    from app.processing.models import Thresholds

    t = Thresholds.from_settings(make_settings(processing_story_threshold=0.5, processing_story_window_hours=24))
    assert (t.story, t.story_window_hours) == (0.5, 24)
    for bad in ({"processing_story_threshold": 0}, {"processing_title_only_threshold": 1.5}):
        with pytest.raises(ValidationError):
            make_settings(**bad)
