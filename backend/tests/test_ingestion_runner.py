"""Orchestration: per-source isolation, outcomes, idempotency, dedup, retention and dry runs (in memory)."""

from datetime import UTC, datetime, timedelta

import httpx

from app.ingestion.models import SourceOutcome
from app.ingestion.runner import IngestionRunner
from app.ingestion.sources import GmailSourceConfig, RssSourceConfig
from app.ingestion.store import MemoryIngestionStore
from app.models.enums import IngestionJobStatus
from tests.conftest import make_settings
from tests.ingestion_helpers import fixture, make_http
from tests.test_ingestion_gmail import FakeGoogle, message

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)


def rss(key, host, **kw):
    return RssSourceConfig(key=key, type="rss", name=key, category="news", url=f"https://{host}/rss", **kw)


GMAIL = GmailSourceConfig(
    key="tldr-ai",
    type="gmail",
    name="TLDR AI",
    category="newsletter",
    query="from:dan@tldrnewsletter.com",
    allowed_senders=["dan@tldrnewsletter.com"],
)


def feed_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "down.example.com":
        return httpx.Response(503)
    if host == "html.example.com":
        return httpx.Response(200, content=fixture("malformed.xml"))
    return httpx.Response(200, content=fixture("rss2.xml"))


def runner(sources, store=None, handler=feed_handler, clock=lambda: NOW, **settings):
    values = {"ingestion_max_retries": 1, **settings}
    return IngestionRunner(
        make_settings(**values),
        store if store is not None else MemoryIngestionStore(),
        sources=sources,
        http_factory=lambda: make_http(handler, max_retries=values["ingestion_max_retries"]),
        clock=clock,
    )


def test_failures_are_isolated_and_outcomes_distinguished():
    store = MemoryIngestionStore()
    sources = [
        rss("down", "down.example.com"),
        rss("good", "good.example.com"),
        rss("broken", "html.example.com"),
        rss("off", "off.example.com", enabled=False),
        GMAIL,
    ]
    report = runner(sources, store).run()
    outcomes = {s.key: s.outcome for s in report.sources}
    assert outcomes == {
        "down": SourceOutcome.FAILED,
        "good": SourceOutcome.SUCCEEDED,
        "broken": SourceOutcome.FAILED,
        "off": SourceOutcome.SKIPPED,
        "tldr-ai": SourceOutcome.SKIPPED,
    }
    assert report.status == "partial"
    by_key = {s.key: s for s in report.sources}
    assert by_key["down"].reason == "HTTP 503" and "Gmail is not configured" in by_key["tldr-ai"].reason
    assert by_key["good"].counts["inserted"] == 2 and by_key["good"].counts["invalid"] == 2
    jobs = {j["source_key"]: j for j in store.jobs.values()}
    assert jobs["down"]["status"] == IngestionJobStatus.FAILED and jobs["down"]["attempt_count"] == 2
    assert jobs["good"]["status"] == IngestionJobStatus.SUCCEEDED
    assert jobs["tldr-ai"]["status"] == IngestionJobStatus.SKIPPED
    assert "off" not in jobs  # disabled sources leave no rows behind
    assert len(store.items) == 2


def test_all_failed_run_status():
    assert runner([rss("down", "down.example.com")]).run().status == "failed"


def test_reruns_are_idempotent_and_use_the_cursor():
    store = MemoryIngestionStore()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=fixture("rss2.xml"))

    first = runner([rss("good", "good.example.com")], store, handler).run()
    second = runner([rss("good", "good.example.com")], store, handler, clock=lambda: NOW + timedelta(hours=6)).run()
    assert first.sources[0].counts["inserted"] == 2
    counts = second.sources[0].counts
    # Entries not newer than the cursor are skipped before normalisation; nothing is staged twice.
    assert counts["inserted"] == 0 and counts["skipped_old"] == 4 - counts["candidates"]
    assert len(store.items) == 2


def test_same_article_from_two_sources_is_staged_once():
    store = MemoryIngestionStore()
    report = runner([rss("one", "a.example.com"), rss("two", "b.example.com")], store).run()
    assert [s.counts["inserted"] for s in report.sources] == [2, 0]
    assert report.sources[1].counts["duplicates"] == 2 and len(store.items) == 2


def test_articles_already_in_the_bulletin_are_not_restaged():
    store = MemoryIngestionStore(published_canonical_urls={"https://lab.example.com/blog/eval-suite"})
    counts = runner([rss("good", "good.example.com")], store).run().sources[0].counts
    assert counts["inserted"] == 1 and counts["already_in_bulletin"] == 1


def test_unexpected_errors_are_contained_and_not_echoed():
    def handler(request):
        raise RuntimeError("secret-token-abc in a stack trace")

    store = MemoryIngestionStore()
    report = runner(
        [rss("bug", "bug.example.com"), rss("good", "good.example.com")],
        store,
        handler=lambda r: handler(r) if r.url.host == "bug.example.com" else feed_handler(r),
    ).run()
    bug = report.sources[0]
    assert bug.outcome is SourceOutcome.FAILED and "secret-token-abc" not in bug.reason
    assert bug.reason.startswith("Unexpected error (RuntimeError)")
    assert report.sources[1].outcome is SourceOutcome.SUCCEEDED


def test_source_already_running_elsewhere_is_skipped():
    store = MemoryIngestionStore()
    store.start_job("good", "source_fetch", NOW)
    report = runner([rss("good", "good.example.com")], store).run()
    assert report.sources[0].outcome is SourceOutcome.SKIPPED
    assert report.sources[0].reason == "already running in another process"


def test_stale_running_jobs_are_recovered_then_collected():
    store = MemoryIngestionStore()
    store.start_job("good", "source_fetch", NOW - timedelta(hours=3))
    report = runner([rss("good", "good.example.com")], store).run()
    assert report.sources[0].outcome is SourceOutcome.SUCCEEDED


def test_expired_items_and_old_jobs_are_purged_but_latest_success_kept():
    store = MemoryIngestionStore()
    old = NOW - timedelta(days=120)
    runner([rss("good", "good.example.com")], store, clock=lambda: old).run()
    assert len(store.items) == 2
    report = runner([rss("down", "down.example.com")], store).run()
    assert report.purged_items == 2 and store.items == {}
    # The old success is the cursor for "good" and survives job retention.
    assert store.last_cursor("good") is not None


def test_dry_run_writes_nothing():
    store = MemoryIngestionStore()
    r = runner([rss("good", "good.example.com")], store)
    r.dry_run = True
    report = r.run()
    assert report.dry_run and report.sources[0].counts["would_stage"] == 2
    assert store.items == {} and store.jobs == {}
    assert len(r.dry_run_items["good"]) == 2


def test_gmail_source_runs_when_configured():
    fake = FakeGoogle({"18c1": message("18c1", "dan@tldrnewsletter.com", 1_793_650_000_000)})
    store = MemoryIngestionStore()
    report = runner(
        [GMAIL],
        store,
        handler=fake,
        gmail_client_id="cid",
        gmail_client_secret="csecret",
        gmail_refresh_token="test-refresh",
    ).run()
    source = report.sources[0]
    assert source.outcome is SourceOutcome.SUCCEEDED and source.counts["inserted"] == 3
    staged = [e["item"] for e in store.items.values()]
    assert {i.source_type for i in staged} == {"gmail"} and all(i.feed_identifier == "gmail:18c1" for i in staged)
    assert store.last_cursor("tldr-ai")["processed_ids"] == ["18c1"]
