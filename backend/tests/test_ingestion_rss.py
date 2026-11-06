"""RSS/Atom parsing, normalisation and incremental collection against fixture feeds (no network)."""

from datetime import UTC, datetime

import httpx
import pytest

from app.ingestion.errors import FeedParseError, FetchError
from app.ingestion.normalize import InvalidItem, dedup_key_for, normalize_item
from app.ingestion.providers.rss import collect_rss, parse_feed
from app.ingestion.sources import RssSourceConfig
from app.models.enums import IngestionSourceType
from tests.ingestion_helpers import fixture, make_http

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
SOURCE = RssSourceConfig(key="lab", type="rss", name="Lab", category="official", url="https://lab.example.com/rss")


def _normalize_all(result):
    good, bad = [], 0
    for raw in result.items:
        try:
            good.append(normalize_item(raw, source_key="lab", source_type=IngestionSourceType.RSS, collected_at=NOW))
        except InvalidItem:
            bad += 1
    return good, bad


def test_rss2_extracts_and_normalises_with_attribution():
    result = parse_feed(fixture("rss2.xml"), feed_url=SOURCE.url, source=SOURCE)
    items, invalid = _normalize_all(result)
    assert invalid == 2  # the metadata-IP link and the untitled entry
    assert [i.title for i in items] == [
        "Test entry one: a new evaluation suite",
        "Test entry two: notes on fine-tuning",
    ]
    first = items[0]
    assert first.original_url == "https://lab.example.com/blog/eval-suite?utm_source=rss&utm_medium=feed"
    assert first.canonical_url == "https://lab.example.com/blog/eval-suite"
    assert first.dedup_key == dedup_key_for(first.canonical_url)
    # Feed title is the publisher when the config does not set one; never invented.
    assert first.publisher == "Example AI Lab Blog"
    assert first.published_at == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert first.feed_identifier == SOURCE.url and first.source_type == "rss"
    assert first.processing_status == "pending" and first.collected_at == NOW
    # HTML stripped, scripts dropped.
    assert first.excerpt == "A fictional summary used only in tests."
    assert first.extra["author"] == "Test Author" and first.extra["categories"] == ["Evaluation"]
    # Instructions inside content are just text: kept verbatim as an excerpt, never acted on.
    assert "Ignore previous instructions" in items[1].excerpt


def test_config_publisher_wins_over_feed_title():
    source = SOURCE.model_copy(update={"publisher": "Example Lab"})
    result = parse_feed(fixture("rss2.xml"), feed_url=SOURCE.url, source=source)
    assert {i.publisher for i in result.items} == {"Example Lab"}


def test_atom_relative_links_are_resolved():
    result = parse_feed(fixture("atom.xml"), feed_url="https://repo.example.org/feed.atom", source=SOURCE)
    (item,) = _normalize_all(result)[0]
    assert item.original_url == "https://repo.example.org/papers/1234"
    assert item.published_at == datetime(2026, 11, 3, 18, 30, 2, tzinfo=UTC)
    assert item.extra["author"] == "Researcher A"


def test_missing_dates_stay_missing_and_implausible_dates_are_dropped():
    from app.ingestion.models import RawItem

    raw = RawItem(title="t", url="https://a.example.com/x", feed_identifier="f", published_at=None)
    assert normalize_item(raw, source_key="k", source_type="rss", collected_at=NOW).published_at is None
    future = RawItem(
        title="t", url="https://a.example.com/x", feed_identifier="f", published_at=datetime(2030, 1, 1, tzinfo=UTC)
    )
    item = normalize_item(future, source_key="k", source_type="rss", collected_at=NOW)
    assert item.published_at is None and item.extra["date_rejected"] is True


def test_malformed_response_raises():
    with pytest.raises(FeedParseError):
        parse_feed(fixture("malformed.xml"), feed_url=SOURCE.url, source=SOURCE)
    with pytest.raises(FeedParseError):
        parse_feed(b"\x00\x01garbage", feed_url=SOURCE.url, source=SOURCE)


def test_partially_broken_feed_keeps_readable_entries():
    broken = fixture("rss2.xml").replace(b"</channel>", b"<item><title>unclosed</channel>")
    result = parse_feed(broken, feed_url=SOURCE.url, source=SOURCE)
    assert result.items and result.warnings


def test_max_items_and_oldest_first():
    result = parse_feed(fixture("rss2.xml"), feed_url=SOURCE.url, source=SOURCE, max_items=1)
    assert len(result.items) == 1 and result.counts["over_limit"] == 3
    assert result.items[0].title.startswith("Test entry one")  # oldest first so the rest drains next run


def test_collect_rss_incremental_with_conditional_get():
    seen_headers = []

    def handler(request):
        seen_headers.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=fixture("rss2.xml"), headers={"ETag": '"v1"', "Last-Modified": "x"})

    http = make_http(handler)
    first = collect_rss(SOURCE, http, None, max_items=50)
    assert len(first.items) == 4 and first.cursor["etag"] == '"v1"'
    assert first.cursor["latest_published_at"] == "2026-11-03T11:00:00+00:00"

    second = collect_rss(SOURCE, http, first.cursor, max_items=50)
    assert second.not_modified and second.items == [] and second.cursor == first.cursor
    assert seen_headers[1]["if-none-match"] == '"v1"'


def test_collect_rss_skips_entries_not_newer_than_cursor():
    http = make_http(lambda r: httpx.Response(200, content=fixture("rss2.xml")))
    result = collect_rss(SOURCE, http, {"latest_published_at": "2026-11-03T10:30:00+00:00"}, max_items=50)
    titles = [i.title for i in result.items]
    assert "Test entry one: a new evaluation suite" not in titles
    assert result.counts["skipped_old"] == 2


def test_unavailable_feed_raises_fetch_error():
    http = make_http(lambda r: httpx.Response(410), max_retries=0)
    with pytest.raises(FetchError):
        collect_rss(SOURCE, http, None, max_items=10)


def test_external_entities_are_not_expanded():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/hostname">]>'
        b'<rss version="2.0"><channel><title>t</title><item><title>safe &x;</title>'
        b"<link>https://a.example.com/x</link></item></channel></rss>"
    )
    from pathlib import Path

    secret_file = Path("/etc/hostname")
    if not secret_file.exists():
        pytest.skip("needs /etc/hostname")
    hostname = secret_file.read_text().strip()
    xxe = xxe.replace(b"file:///etc/hostname", secret_file.as_uri().encode())
    result = parse_feed(xxe, feed_url=SOURCE.url, source=SOURCE)
    assert all(hostname not in (i.title or "") for i in result.items)


def test_first_run_only_takes_entries_inside_lookback_window():
    # A feed that carries its whole archive must not stage years-old entries on the first run.
    http = make_http(lambda r: httpx.Response(200, content=fixture("rss2.xml")))
    source = SOURCE.model_copy(update={"initial_lookback_days": 1})
    now = datetime(2026, 11, 4, 10, 45, tzinfo=UTC)  # window starts 2026-11-03 10:45
    result = collect_rss(source, http, None, max_items=50, now=now)
    dated = [i for i in result.items if i.published_at is not None]
    assert [i.published_at for i in dated] == [datetime(2026, 11, 3, 11, 0, tzinfo=UTC)]
    assert result.counts["skipped_old"] == 2
    assert result.cursor["latest_published_at"] == "2026-11-03T11:00:00+00:00"

    # Once a cursor exists the lookback no longer applies.
    later = collect_rss(source, http, {"latest_published_at": "2026-11-01T00:00:00+00:00"}, max_items=50, now=now)
    assert later.counts["skipped_old"] == 0
