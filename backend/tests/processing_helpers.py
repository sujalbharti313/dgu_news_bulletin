"""Helpers for Stage 3.2 tests: synthetic staged items and an in-memory process → apply → cluster loop.

All articles are fictional test data on reserved example domains (organisation names are only used to
exercise the entity rules)."""

import uuid
from datetime import UTC, datetime, timedelta

from app.ingestion.store import MemoryIngestionStore
from app.processing.clusters import build_clusters
from app.processing.models import SourceInfo, StagedItem, Thresholds
from app.processing.pipeline import process_items
from app.processing.store import MemoryProcessingStore
from app.services.urls import canonicalize_url

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SOURCES = {
    "lab-blog": SourceInfo(name="Lab Blog", category="official"),
    "news-a": SourceInfo(name="News A", category="news"),
    "news-b": SourceInfo(name="News B", category="news"),
    "tldr-ai": SourceInfo(name="TLDR AI", category="newsletter"),
    "arxiv": SourceInfo(name="arXiv", category="research"),
}


def staged(
    title: str,
    url: str,
    excerpt: str | None = None,
    *,
    hours_ago: float | None = 1,
    source: str = "news-a",
    publisher: str | None = None,
    collected_hours_ago: float = 0,
    extra: dict | None = None,
    source_type: str = "rss",
) -> StagedItem:
    canonical = canonicalize_url(url)
    key = uuid.uuid5(uuid.NAMESPACE_URL, canonical).hex * 2
    return StagedItem(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"item:{canonical}"),
        dedup_key=key[:64],
        source_key=source,
        source_type=source_type,
        title=title,
        original_url=url,
        canonical_url=canonical,
        publisher=publisher,
        published_at=NOW - timedelta(hours=hours_ago) if hours_ago is not None else None,
        excerpt=excerpt,
        feed_identifier=f"https://{source}.example.com/feed.xml" if source_type == "rss" else "gmail:abc123",
        collected_at=NOW - timedelta(hours=collected_hours_ago),
        extra=extra or {},
    )


def run(items: list[StagedItem], thresholds: Thresholds | None = None, now: datetime = NOW):
    """Process, apply to an in-memory store, and return (result, store, clusters)."""
    thresholds = thresholds or Thresholds()
    store = MemoryProcessingStore(items)
    result = process_items(store.load_items(now, 10_000), thresholds=thresholds, now=now)
    store.apply(result.updates)
    clusters = build_clusters(store.load_items(now, 10_000), sources=SOURCES, thresholds=thresholds, now=now)
    return result, store, clusters


def decisions(result) -> dict[uuid.UUID, str]:
    return {u.id: u.processing.get("decision") for u in result.updates}


def cluster_of(clusters, item: StagedItem):
    for c in clusters:
        if any(ref.item_id == item.id for ref in c.sources):
            return c
    return None


def staged_from_memory(store: MemoryIngestionStore) -> list[StagedItem]:
    """Stage 3.1's in-memory staging table as Stage 3.2 input (what SqlProcessingStore reads from Postgres)."""
    out = []
    for entry in store.items.values():
        item = StagedItem.from_normalized(entry["item"])
        item.extra = dict(entry.get("extra") or item.extra)
        out.append(item)
    return out
