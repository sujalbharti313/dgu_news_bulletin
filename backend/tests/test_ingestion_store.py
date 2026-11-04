"""SqlIngestionStore against the real schema (local test Postgres): dedup, concurrency guard, retention,
cursors, and that ingestion never modifies published content."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.session import Database
from app.ingestion.models import RawItem
from app.ingestion.normalize import normalize_item
from app.ingestion.runner import IngestionRunner
from app.ingestion.sources import RssSourceConfig
from app.ingestion.store import SqlIngestionStore
from app.models import IngestionItem, IngestionJob, Story
from tests import factories
from tests.conftest import TEST_DATABASE_URL, make_settings
from tests.ingestion_helpers import fixture, make_http

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")

NOW = datetime.now(UTC).replace(microsecond=0)


class SessionDatabase(Database):
    """Routes the store's write sessions into the test's rolled-back transaction (one savepoint each)."""

    def __init__(self, session):
        super().__init__(make_settings(), engine=session.get_bind().engine)
        self._session = session

    @contextmanager
    def write_session(self):
        with self._session.begin_nested():
            yield self._session


@pytest.fixture
def store(db_session):
    return SqlIngestionStore(SessionDatabase(db_session))


def item(url: str, title: str = "A fictional test item", key: str = "feed-a"):
    return normalize_item(
        RawItem(title=title, url=url, feed_identifier="https://feed.example.com/rss"),
        source_key=key,
        source_type="rss",
        collected_at=NOW,
    )


def test_save_items_dedups_within_batch_across_runs_and_against_stories(store, db_session):
    src, ed = factories.source(db_session), factories.edition(db_session)
    factories.story(
        db_session,
        edition=ed,
        source=src,
        original_url="https://news.example.com/published",
        canonical_url="https://news.example.com/published",
    )
    job = store.start_job("feed-a", "source_fetch", NOW)
    batch = [
        item("https://news.example.com/a?utm_source=x"),
        item("https://www.news.example.com/a/"),
        item("https://news.example.com/b"),
        item("https://news.example.com/published"),
    ]
    first = store.save_items(batch, job, NOW + timedelta(days=14))
    assert (first.inserted, first.duplicates, first.already_in_bulletin) == (2, 1, 1)
    second = store.save_items([item("https://news.example.com/b", key="feed-b")], None, NOW + timedelta(days=14))
    assert (second.inserted, second.duplicates) == (0, 1)
    rows = db_session.scalars(select(IngestionItem).order_by(IngestionItem.canonical_url)).all()
    assert [r.canonical_url for r in rows] == ["https://news.example.com/a", "https://news.example.com/b"]
    assert rows[0].original_url == "https://news.example.com/a?utm_source=x"  # original preserved
    assert rows[0].processing_status == "pending" and rows[0].job_id == job


def test_one_running_job_per_source(store, db_session):
    assert store.start_job("feed-a", "source_fetch", NOW) is not None
    assert store.start_job("feed-a", "source_fetch", NOW) is None
    assert store.start_job("feed-b", "source_fetch", NOW) is not None
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(IngestionJob(source_key="feed-b", job_type="source_fetch", status="running"))
        db_session.flush()


def test_cursor_comes_from_latest_success(store):
    first = store.start_job("feed-a", "source_fetch", NOW - timedelta(hours=2))
    store.finish_job(
        first, status="succeeded", now=NOW - timedelta(hours=2), attempts=1, details={"cursor": {"etag": "v1"}}
    )
    failed = store.start_job("feed-a", "source_fetch", NOW - timedelta(hours=1))
    store.finish_job(failed, status="failed", now=NOW - timedelta(hours=1), attempts=3, details={}, error="HTTP 500")
    assert store.last_cursor("feed-a") == {"etag": "v1"}
    assert store.last_cursor("unknown") is None
    status = store.source_statuses(["feed-a"])["feed-a"]
    assert status.last_status == "failed" and status.consecutive_failures == 1 and status.last_error == "HTTP 500"


def test_retention_purges_expired_items_old_jobs_but_keeps_latest_success(store, db_session):
    old = NOW - timedelta(days=200)
    keep = store.start_job("feed-a", "source_fetch", old)
    store.finish_job(keep, status="succeeded", now=old, attempts=1, details={"cursor": {"x": 1}})
    store.record_skipped("feed-a", "source_fetch", "disabled", old)
    db_session.execute(IngestionJob.__table__.update().values(created_at=old))
    store.save_items([item("https://news.example.com/old")], keep, NOW + timedelta(days=1))
    store.save_items([item("https://news.example.com/new")], keep, NOW + timedelta(days=30))
    later = NOW + timedelta(days=2)

    assert store.purge_expired_items(later) == 1
    assert store.purge_old_jobs(later, retention_days=90) == 1
    assert db_session.scalar(select(func.count()).select_from(IngestionItem)) == 1
    assert store.last_cursor("feed-a") == {"x": 1}


def test_stale_running_jobs_are_marked_failed(store, db_session):
    job = store.start_job("feed-a", "source_fetch", NOW - timedelta(hours=5))
    assert store.recover_stale_jobs(NOW, stale_minutes=60) == 1
    row = db_session.get(IngestionJob, job)
    db_session.refresh(row)
    assert row.status == "failed" and "Abandoned" in row.error_summary


def test_full_run_against_postgres_never_touches_stories(store, db_session):
    src, ed = factories.source(db_session), factories.edition(db_session)
    factories.story(db_session, edition=ed, source=src)
    before = [(s.id, s.updated_at, s.status) for s in db_session.scalars(select(Story))]
    source = RssSourceConfig(key="lab", type="rss", name="Lab", category="official", url="https://lab.example.com/rss")
    import httpx

    runner = IngestionRunner(
        make_settings(),
        store,
        sources=[source],
        http_factory=lambda: make_http(lambda r: httpx.Response(200, content=fixture("rss2.xml"))),
    )
    first = runner.run()
    second = runner.run()
    assert first.sources[0].counts["inserted"] == 2 and second.sources[0].counts["inserted"] == 0
    assert db_session.scalar(select(func.count()).select_from(IngestionItem)) == 2
    after = [(s.id, s.updated_at, s.status) for s in db_session.scalars(select(Story))]
    assert before == after
    jobs = db_session.scalars(select(IngestionJob).where(IngestionJob.source_key == "lab")).all()
    assert sorted(j.status for j in jobs) == ["succeeded", "succeeded"]
    assert all("cursor" in j.details and "counts" in j.details for j in jobs)
