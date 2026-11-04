"""RSS 0.9x/1.0/2.0 and Atom feeds.

The feed is downloaded by SafeHttpClient (feedparser is given bytes and never fetches anything itself).
Incremental collection: conditional GET with the stored ETag/Last-Modified (a 304 means nothing new), and
entries not newer than the newest date already collected are skipped. Dedup in the store catches the rest.
"""

import calendar
import logging
from datetime import UTC, datetime

import feedparser

from app.ingestion.errors import FeedParseError
from app.ingestion.http import SafeHttpClient
from app.ingestion.models import CollectResult, RawItem
from app.ingestion.sources import RssSourceConfig

logger = logging.getLogger("app.ingestion.rss")

ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1"


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                # feedparser normalises to UTC struct_time.
                return datetime.fromtimestamp(calendar.timegm(value), UTC)
            except (OverflowError, ValueError, TypeError):
                return None
    return None


def _entry_link(entry) -> str | None:
    link = entry.get("link")
    if link:
        return link
    for candidate in entry.get("links") or []:
        if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
            return candidate["href"]
    guid = entry.get("id")
    if entry.get("guidislink") and guid:
        return guid
    return None


def _entry_description(entry) -> str | None:
    summary = entry.get("summary")
    if summary:
        return summary
    content = entry.get("content") or []
    if content and content[0].get("value"):
        return content[0]["value"]
    return None


def parse_feed(
    content: bytes,
    *,
    feed_url: str,
    source: RssSourceConfig,
    content_type: str | None = None,
    since: datetime | None = None,
    max_items: int = 100,
) -> CollectResult:
    headers = {"content-location": feed_url}
    if content_type:
        headers["content-type"] = content_type
    parsed = feedparser.parse(content, response_headers=headers, sanitize_html=True, resolve_relative_uris=True)
    entries = parsed.get("entries") or []
    warnings: list[str] = []
    if parsed.get("bozo"):
        problem = type(parsed.get("bozo_exception")).__name__
        if not entries:
            raise FeedParseError(f"Feed could not be parsed ({problem}).")
        warnings.append(f"feed is not well-formed ({problem}); {len(entries)} entries were still readable")
    if not parsed.get("version") and not entries:
        raise FeedParseError("Response is not an RSS or Atom feed.")

    feed_title = (parsed.get("feed") or {}).get("title")
    publisher = source.publisher or feed_title or None

    items: list[RawItem] = []
    counts = {"entries": len(entries), "skipped_old": 0, "over_limit": 0}
    newest = since
    # Oldest first, so when a feed has more new entries than max_items the newer remainder is still
    # newer than the cursor and is collected on the next run instead of being skipped for good.
    dated = sorted(
        ((entry, _entry_datetime(entry)) for entry in entries),
        key=lambda pair: (pair[1] is None, pair[1] or datetime.min.replace(tzinfo=UTC)),
    )
    for entry, published in dated:
        if since is not None and published is not None and published <= since:
            counts["skipped_old"] += 1
            continue
        if len(items) >= max_items:
            counts["over_limit"] += 1
            continue
        if published is not None and (newest is None or published > newest):
            newest = published
        tags = [t.get("term") for t in entry.get("tags") or [] if t.get("term")]
        extra = {"feed_title": feed_title, "author": entry.get("author"), "categories": tags, "guid": entry.get("id")}
        items.append(
            RawItem(
                title=entry.get("title"),
                url=_entry_link(entry),
                feed_identifier=feed_url,
                publisher=publisher,
                published_at=published,
                description=_entry_description(entry),
                extra={k: v for k, v in extra.items() if v},
            )
        )
    cursor = {"latest_published_at": newest.isoformat() if newest else None}
    return CollectResult(items=items, cursor=cursor, counts=counts, warnings=warnings)


def collect_rss(source: RssSourceConfig, http: SafeHttpClient, cursor: dict | None, *, max_items: int) -> CollectResult:
    cursor = cursor or {}
    headers = {"Accept": ACCEPT}
    if cursor.get("etag"):
        headers["If-None-Match"] = cursor["etag"]
    if cursor.get("last_modified"):
        headers["If-Modified-Since"] = cursor["last_modified"]

    response = http.get(source.url, headers=headers, allow_http=source.allow_http)
    if response.status_code == 304:
        return CollectResult(items=[], cursor=dict(cursor), not_modified=True)

    since = None
    if cursor.get("latest_published_at"):
        try:
            since = datetime.fromisoformat(cursor["latest_published_at"])
        except ValueError:
            since = None
    result = parse_feed(
        response.content,
        feed_url=source.url,
        source=source,
        content_type=response.headers.get("content-type"),
        since=since,
        max_items=max_items,
    )
    # Validator headers are short opaque strings; bound them anyway since they come from the server.
    etag = response.headers.get("etag")
    last_modified = response.headers.get("last-modified")
    result.cursor.update(
        {
            "etag": etag[:200] if etag else None,
            "last_modified": last_modified[:100] if last_modified else None,
        }
    )
    return result
