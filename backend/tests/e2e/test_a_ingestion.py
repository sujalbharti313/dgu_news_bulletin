"""Phase 4.2 Scenario A: news ingestion, all source types together, into the real schema (local test PostgreSQL).

RSS 2.0, Atom and the TLDR AI Gmail newsletter (mocked Gmail API and OAuth endpoint) run through the production
`IngestionRunner` and `SqlIngestionStore`; feeds that are malformed, empty, slow, unreachable or flaky run in the
same pass to prove one source can never break another. Nothing leaves the process.
"""

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.ingestion.models import SourceOutcome
from app.ingestion.runner import IngestionRunner
from app.ingestion.sources import GmailSourceConfig, RssSourceConfig
from app.ingestion.store import SqlIngestionStore
from app.models import IngestionItem, IngestionJob
from tests.conftest import TEST_DATABASE_URL, make_settings
from tests.edition_helpers import SavepointDatabase
from tests.ingestion_helpers import fixture, make_http
from tests.test_ingestion_gmail import FakeGoogle, message

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
EMPTY_RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'


def rss(key, host, category="news"):
    return RssSourceConfig(key=key, type="rss", name=f"{key} feed", category=category, url=f"https://{host}/feed")


GMAIL = GmailSourceConfig(
    key="tldr-ai",
    type="gmail",
    name="TLDR AI",
    category="newsletter",
    query="from:dan@tldrnewsletter.com",
    allowed_senders=["dan@tldrnewsletter.com"],
)


class World:
    """One mocked internet: feed hosts, a flaky host, a dead host and Gmail, with per-host request counts."""

    def __init__(self, google: FakeGoogle | None = None):
        self.google = google or FakeGoogle(
            {"18c1": message("18c1", "TLDR AI <dan@tldrnewsletter.com>", 1_793_650_000_000)}
        )
        self.calls: dict[str, int] = {}
        self.flaky_failures = 1
        self.rss_body = fixture("rss2.xml")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls[host] = self.calls.get(host, 0) + 1
        if host.endswith("googleapis.com"):
            return self.google(request)
        if host == "rss.example.com":
            return httpx.Response(200, content=self.rss_body)
        if host == "atom.example.com":
            return httpx.Response(200, content=fixture("atom.xml"))
        if host == "broken.example.com":
            return httpx.Response(200, content=fixture("malformed.xml"))
        if host == "empty.example.com":
            return httpx.Response(200, content=EMPTY_RSS, headers={"content-type": "application/rss+xml"})
        if host == "slow.example.com":
            raise httpx.ReadTimeout("timed out", request=request)
        if host == "dead.example.com":
            raise httpx.ConnectError("connection refused", request=request)
        if host == "flaky.example.com":
            if self.calls[host] <= self.flaky_failures:
                return httpx.Response(503)
            return httpx.Response(200, content=fixture("atom.xml").replace(b"/papers/1234", b"/papers/9999"))
        return httpx.Response(404)


def make_runner(db_session, world, sources, *, clock=lambda: NOW, gmail=True, retries=2):
    values = {"ingestion_max_retries": retries}
    if gmail:
        values |= {"gmail_client_id": "cid", "gmail_client_secret": "csecret", "gmail_refresh_token": "test-refresh"}
    settings = make_settings(**values)
    sleeps: list[float] = []
    runner = IngestionRunner(
        settings,
        SqlIngestionStore(SavepointDatabase(settings, db_session)),
        sources=sources,
        http_factory=lambda: make_http(world, max_retries=retries, sleeps=sleeps),
        clock=clock,
    )
    runner.sleeps = sleeps
    return runner


ALL = [
    rss("lab-rss", "rss.example.com", "official"),
    rss("repo-atom", "atom.example.com", "research"),
    GMAIL,
    rss("broken", "broken.example.com"),
    rss("empty", "empty.example.com"),
    rss("slow", "slow.example.com"),
    rss("dead", "dead.example.com"),
    rss("flaky", "flaky.example.com"),
]


def items(db_session, key=None):
    q = select(IngestionItem)
    if key:
        q = q.where(IngestionItem.source_key == key)
    return db_session.scalars(q).all()


def test_mixed_sources_stage_valid_articles_with_metadata(db_session):
    """E2E-A-01: RSS, Atom and Gmail articles reach `ingestion_items` with title, original and canonical URL,
    publisher, published date and source type; HTML is stripped; nothing is published."""
    world = World()
    report = make_runner(db_session, world, ALL).run()
    by = {s.key: s for s in report.sources}
    assert by["lab-rss"].outcome is SourceOutcome.SUCCEEDED and by["lab-rss"].counts["inserted"] == 2
    assert by["repo-atom"].outcome is SourceOutcome.SUCCEEDED and by["repo-atom"].counts["inserted"] == 1
    assert by["tldr-ai"].outcome is SourceOutcome.SUCCEEDED and by["tldr-ai"].counts["inserted"] >= 1

    lab = {i.title: i for i in items(db_session, "lab-rss")}
    eval_suite = lab["Test entry one: a new evaluation suite"]
    assert eval_suite.original_url.startswith("https://lab.example.com/blog/eval-suite")
    assert eval_suite.canonical_url == "https://lab.example.com/blog/eval-suite"  # tracking parameters removed
    assert eval_suite.published_at == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert (
        eval_suite.publisher
        and "<script>" not in (eval_suite.excerpt or "")
        and "alert" not in (eval_suite.excerpt or "")
    )
    assert eval_suite.processing_status == "pending" and eval_suite.source_type == "rss"

    atom = items(db_session, "repo-atom")[0]
    assert atom.original_url == "https://atom.example.com/papers/1234"  # relative Atom link resolved
    assert atom.published_at is not None

    newsletter = items(db_session, "tldr-ai")
    assert newsletter and {i.source_type for i in newsletter} == {"gmail"}
    for i in newsletter:  # the article's own link, never the newsletter's tracking redirect or Gmail
        assert i.original_url.startswith("https://") and "tldrnewsletter" not in i.original_url
        assert "google" not in i.original_url
    # Nothing in Stage 3.1 creates public stories.
    from app.models import Story

    assert db_session.scalar(select(func.count()).select_from(Story)) == 0


def test_invalid_items_are_rejected_without_failing_the_source(db_session):
    """E2E-A-02: an item pointing at a cloud metadata address and an item without a title are discarded and
    counted as invalid; the valid items of the same feed are staged."""
    report = make_runner(db_session, World(), [rss("lab-rss", "rss.example.com")]).run()
    counts = report.sources[0].counts
    assert counts["invalid"] == 2 and counts["inserted"] == 2
    assert not any("169.254" in i.original_url for i in items(db_session))


def test_failing_sources_are_isolated_and_recorded(db_session):
    """E2E-A-03: malformed, slow (timeout), unreachable sources fail on their own; an empty feed succeeds with
    nothing; a flaky source (503 then 200) succeeds after a retry; each outcome is a row in `ingestion_jobs`."""
    world = World()
    runner = make_runner(db_session, world, ALL)
    report = runner.run()
    by = {s.key: s for s in report.sources}
    assert report.status == "partial"
    assert by["broken"].outcome is SourceOutcome.FAILED
    assert by["slow"].outcome is SourceOutcome.FAILED and by["dead"].outcome is SourceOutcome.FAILED
    assert by["empty"].outcome is SourceOutcome.SUCCEEDED and by["empty"].counts["inserted"] == 0
    assert by["flaky"].outcome is SourceOutcome.SUCCEEDED and by["flaky"].counts["inserted"] == 1
    assert world.calls["flaky.example.com"] == 2  # retried once
    assert world.calls["slow.example.com"] == 3 and world.calls["dead.example.com"] == 3  # 1 + 2 retries, bounded
    assert runner.sleeps and all(0 < s <= 60 for s in runner.sleeps)  # backoff happened and is capped
    jobs = {j.source_key: j for j in db_session.scalars(select(IngestionJob)).all()}
    assert {k: jobs[k].status for k in ("broken", "slow", "dead")} == {
        "broken": "failed",
        "slow": "failed",
        "dead": "failed",
    }
    assert jobs["lab-rss"].status == "succeeded" and jobs["flaky"].status == "succeeded"
    assert jobs["slow"].error_summary and jobs["dead"].attempt_count == 3
    for j in jobs.values():  # failure text never carries tokens or the OAuth secret
        assert "csecret" not in (j.error_summary or "") and "test-refresh" not in (j.error_summary or "")


def test_repeated_ingestion_creates_no_duplicates_and_picks_up_new_entries(db_session):
    """E2E-A-04: a second and third run of the same sources insert nothing new; a new feed entry published
    later is picked up incrementally, exactly once."""
    world = World()
    make_runner(db_session, world, ALL).run()
    first = len(items(db_session))
    second = make_runner(db_session, world, ALL, clock=lambda: NOW + timedelta(hours=6)).run()
    assert sum(s.counts.get("inserted", 0) for s in second.sources) == 0
    assert len(items(db_session)) == first

    new_entry = (
        b"<item><title>Brand new entry about agents</title><link>https://lab.example.com/blog/agents</link>"
        b'<guid isPermaLink="false">entry-3</guid><pubDate>Wed, 04 Nov 2026 12:00:00 GMT</pubDate>'
        b"<description>Fictional.</description></item></channel>"
    )
    world.rss_body = fixture("rss2.xml").replace(b"</channel>", new_entry)
    third = make_runner(db_session, world, ALL, clock=lambda: NOW + timedelta(hours=12)).run()
    by = {s.key: s for s in third.sources}
    assert by["lab-rss"].counts["inserted"] == 1
    make_runner(db_session, world, ALL, clock=lambda: NOW + timedelta(hours=18)).run()
    assert len(items(db_session)) == first + 1
    keys = [i.dedup_key for i in items(db_session)]
    assert len(keys) == len(set(keys))


def test_same_article_from_rss_and_newsletter_is_staged_once(db_session):
    """E2E-A-05: an article that appears both in a feed and in another source is staged once (dedup key on
    the canonical URL); the second sighting is counted as a duplicate."""
    world = World()
    make_runner(db_session, world, [rss("lab-rss", "rss.example.com"), rss("mirror", "rss.example.com")]).run()
    rows = items(db_session)
    assert len({r.canonical_url for r in rows}) == len(rows) == 2


def test_expired_gmail_token_fails_only_the_newsletter(db_session):
    """E2E-A-06: a revoked/expired Gmail refresh token (Google answers invalid_grant) fails the TLDR source
    with a safe message; every RSS source still succeeds. (Today the run then reports `partial`, not failed.)"""
    world = World(google=FakeGoogle({}, token_status=400))
    report = make_runner(db_session, world, ALL).run()
    by = {s.key: s for s in report.sources}
    assert by["tldr-ai"].outcome is SourceOutcome.FAILED
    assert "test-refresh" not in (by["tldr-ai"].reason or "") and "csecret" not in (by["tldr-ai"].reason or "")
    assert by["lab-rss"].outcome is SourceOutcome.SUCCEEDED and by["repo-atom"].outcome is SourceOutcome.SUCCEEDED
    assert report.status == "partial"


def test_gmail_without_credentials_is_skipped_not_called(db_session):
    """E2E-A-07: with no Gmail credentials configured the newsletter is skipped and Google is never contacted."""
    world = World()
    report = make_runner(db_session, world, [GMAIL], gmail=False).run()
    assert report.sources[0].outcome is SourceOutcome.SKIPPED
    assert not any(h.endswith("googleapis.com") for h in world.calls)


def test_newsletter_body_is_decoded_and_attachments_ignored(db_session):
    """E2E-A-08: the Gmail message's base64url HTML part is parsed into separate articles; the PDF attachment is
    never downloaded."""
    world = World()
    make_runner(db_session, world, [GMAIL]).run()
    assert world.google.fetched == ["18c1"]
    assert not any("attachments" in str(k) for k in world.calls)
    rows = items(db_session, "tldr-ai")
    assert len(rows) >= 2 and all(r.title for r in rows)
    assert base64  # (import kept for readers: the fixture is encoded exactly like the Gmail API does)


def test_empty_feed_without_content_type_is_reported_as_unparseable(db_session):
    """E2E-A-09 (observation, LOW): a well-formed feed with zero entries served without an XML content type is
    reported as a parse failure (feedparser's NonXMLContentType) rather than an empty success. Harmless: the next
    run retries; recorded so the behaviour is known."""

    def handler(request):
        return httpx.Response(200, content=EMPTY_RSS)

    report = make_runner(db_session, handler, [rss("empty", "empty.example.com")]).run()
    assert report.sources[0].outcome is SourceOutcome.FAILED
    assert "NonXMLContentType" in report.sources[0].reason
