"""Processed staged items → `StoryCluster` records (the Stage 3.3 contract). Pure, deterministic, read-only.

Clusters are assembled on read from `ingestion_items`, so no separate cluster table exists and a cluster
disappears when its last item expires. Published stories carry their own attribution in `stories`.
"""

import hashlib
import uuid
from collections import Counter
from datetime import datetime
from typing import Any

from app.models.enums import DedupRelation, IngestionItemStatus
from app.processing.contract import (
    ClusterEntities,
    ProcessingMetadata,
    Representative,
    SourceReference,
    StoryCluster,
)
from app.processing.features import EVENT_LEXICON
from app.processing.models import ALGORITHM_VERSION, SourceInfo, StagedItem, Thresholds
from app.processing.pipeline import Prepared, prepare
from app.processing.text import normalize_datetime, normalize_publisher, site_of

_KIND_BY_CATEGORY = {
    "official": "official_channel",
    "research": "research_repository",
    "government": "government",
    "news": "news_outlet",
    "newsletter": "newsletter",
}
_METADATA_SKIP = {"also_seen", "newsletter_date"}
_DUPLICATE_RELATIONS = {DedupRelation.EXACT_DUPLICATE.value, DedupRelation.NEAR_DUPLICATE.value}
MAX_SIGHTINGS = 10


def source_kind(source_key: str, source_type: str, sources: dict[str, SourceInfo]) -> str:
    info = sources.get(source_key)
    if info is not None:
        return _KIND_BY_CATEGORY.get(info.category, "unknown")
    return "newsletter" if source_type == "gmail" else "unknown"


def _metadata(extra: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (extra or {}).items() if k not in _METADATA_SKIP}


def _reference(p: Prepared, sources: dict[str, SourceInfo]) -> SourceReference:
    item, details = p.item, p.item.processing or {}
    match = details.get("match") or {}
    info = sources.get(item.source_key)
    return SourceReference(
        item_id=item.id,
        ingestion_dedup_key=item.dedup_key,
        source_key=item.source_key,
        source_name=info.name if info else None,
        source_type=item.source_type,
        source_kind=source_kind(item.source_key, item.source_type, sources),
        feed_identifier=item.feed_identifier,
        title=p.title,
        excerpt=p.excerpt,
        publisher=p.publisher,
        site=p.site,
        original_url=item.original_url,
        canonical_url=item.canonical_url,
        published_at=p.published_at,
        collected_at=item.collected_at,
        newsletter_date=p.newsletter_date,
        relation=item.dedup_relation,
        duplicate_of=item.duplicate_of_id,
        match_score=match.get("score"),
        match_reasons=list(match.get("reasons") or []),
        identifiers=p.features.identifiers,
        content_flags=p.flags,
        metadata=_metadata(item.extra),
    )


def _sightings(p: Prepared, sources: dict[str, SourceInfo], now: datetime) -> list[SourceReference]:
    """Other sources that carried the same canonical URL (recorded by Stage 3.1 as `extra.also_seen`)."""
    refs = []
    for seen in ((p.item.extra or {}).get("also_seen") or [])[:MAX_SIGHTINGS]:
        if not isinstance(seen, dict) or not seen.get("source_key") or not seen.get("original_url"):
            continue
        source_key, source_type = str(seen["source_key"]), str(seen.get("source_type") or "rss")
        info = sources.get(source_key)
        refs.append(
            SourceReference(
                item_id=p.item.id,
                ingestion_dedup_key=p.item.dedup_key,
                source_key=source_key,
                source_name=info.name if info else None,
                source_type=source_type,
                source_kind=source_kind(source_key, source_type, sources),
                feed_identifier=str(seen.get("feed_identifier") or ""),
                title=str(seen.get("title") or p.title),
                excerpt=None,
                publisher=normalize_publisher(seen.get("publisher")),
                site=site_of(seen["original_url"]),
                original_url=str(seen["original_url"]),
                canonical_url=p.item.canonical_url,
                published_at=normalize_datetime(seen.get("published_at"), now=now),
                collected_at=normalize_datetime(seen.get("collected_at"), now=now) or p.item.collected_at,
                newsletter_date=normalize_datetime(seen.get("newsletter_date"), now=now),
                relation="same_url",
                duplicate_of=p.item.id,
                match_score=1.0,
                match_reasons=["same_canonical_url"],
                identifiers=p.features.identifiers,
            )
        )
    return refs


def _representative(members: list[Prepared], sources: dict[str, SourceInfo]) -> tuple[Prepared, list[str]]:
    """The best-documented non-duplicate article. Explicitly not a claim of being the original report."""
    candidates = [m for m in members if m.item.dedup_relation not in _DUPLICATE_RELATIONS] or members

    def rank(m: Prepared):
        official = source_kind(m.item.source_key, m.item.source_type, sources) in ("official_channel", "government")
        return (
            not official,
            m.publisher is None,
            m.published_at is None,
            -len(m.excerpt or ""),
            m.features.event_time,
            m.item.dedup_key,
        )

    best = min(candidates, key=rank)
    reasons = []
    if source_kind(best.item.source_key, best.item.source_type, sources) in ("official_channel", "government"):
        reasons.append("official_channel")
    if best.publisher:
        reasons.append("has_publisher")
    if best.published_at:
        reasons.append("has_publication_date")
    if best.excerpt:
        reasons.append(
            "longest_excerpt" if len(best.excerpt) == max(len(m.excerpt or "") for m in candidates) else "has_excerpt"
        )
    return best, reasons or ["only_candidate"]


def _event_type(members: list[Prepared]) -> str | None:
    votes = Counter(m.features.event_type for m in members if m.features.event_type)
    if not votes:
        return None
    top = max(votes.values())
    return next(name for name in EVENT_LEXICON if votes.get(name) == top)


def _revision(members: list[Prepared], refs: list[SourceReference]) -> str:
    parts = sorted(f"{r.item_id}|{r.relation}|{r.source_key}|{r.feed_identifier}" for r in refs)
    parts += sorted(f"{m.item.id}|{m.item.processing_status}" for m in members)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def build_cluster(
    cluster_id: uuid.UUID,
    members: list[Prepared],
    sources: dict[str, SourceInfo],
    thresholds: Thresholds,
    now: datetime,
) -> StoryCluster:
    members = sorted(members, key=lambda m: (m.features.event_time, m.item.collected_at, m.item.dedup_key))
    refs: list[SourceReference] = []
    for m in members:
        refs.append(_reference(m, sources))
        refs.extend(_sightings(m, sources, now))
    rep, rep_reasons = _representative(members, sources)
    non_duplicates = [m for m in members if m.item.dedup_relation not in _DUPLICATE_RELATIONS] or members
    longest = max(non_duplicates, key=lambda m: (len(m.excerpt or ""), m is rep))
    published = [r.published_at for r in refs if r.published_at]
    collected = [r.collected_at for r in refs]
    flags = sorted({flag for r in refs for flag in r.content_flags})
    return StoryCluster(
        cluster_id=cluster_id,
        title=rep.title,
        description=rep.excerpt,
        excerpt=longest.excerpt,
        representative=Representative(
            item_id=rep.item.id,
            title=rep.title,
            original_url=rep.item.original_url,
            canonical_url=rep.item.canonical_url,
            publisher=rep.publisher,
            site=rep.site,
            published_at=rep.published_at,
            selection_reasons=rep_reasons,
        ),
        event_type=_event_type(members),
        entities=ClusterEntities(
            organisations=sorted({o for m in members for o in m.features.organisations}),
            products=sorted({p for m in members for p in m.features.products}),
            benchmarks=sorted({b for m in members for b in m.features.benchmarks}),
        ),
        earliest_published_at=min(published) if published else None,
        latest_published_at=max(published) if published else None,
        first_collected_at=min(collected),
        last_collected_at=max(collected),
        article_count=len(members),
        duplicate_count=sum(m.item.dedup_relation in _DUPLICATE_RELATIONS for m in members)
        + sum(r.relation == "same_url" for r in refs),
        source_count=len(refs),
        publisher_count=len({(r.publisher or r.site or r.source_key).casefold() for r in refs}),
        sources=refs,
        content_flags=flags,
        processing=ProcessingMetadata(
            algorithm_version=ALGORITHM_VERSION,
            processed_at=max((m.item.processed_at or now) for m in members),
            revision=_revision(members, refs),
            time_basis=sorted({m.features.time_basis for m in members}),
            thresholds=thresholds.as_dict(),
        ),
    )


def build_clusters(
    items: list[StagedItem], *, sources: dict[str, SourceInfo], thresholds: Thresholds, now: datetime
) -> list[StoryCluster]:
    """Every cluster among processed items, most recently active first."""
    groups: dict[uuid.UUID, list[Prepared]] = {}
    for item in items:
        if item.processing_status != IngestionItemStatus.PROCESSED.value or not item.cluster_id:
            continue
        try:
            p = prepare(item, now=now, thresholds=thresholds)
        except Exception:  # noqa: BLE001, S112 - a row that no longer validates is left out, not guessed
            continue
        groups.setdefault(item.cluster_id, []).append(p)
    clusters = [build_cluster(cid, members, sources, thresholds, now) for cid, members in groups.items()]
    clusters.sort(key=lambda c: (c.last_collected_at, str(c.cluster_id)), reverse=True)
    return clusters
