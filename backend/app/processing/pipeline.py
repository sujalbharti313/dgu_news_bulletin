"""Stage 3.2 core: staged items in, processing decisions out. Pure and deterministic (no I/O).

`process_items` takes every unexpired staged item. Items processed in an earlier run keep their cluster
and relation (stable cluster ids, idempotent re-runs); `pending` and `failed` items are prepared,
validated and assigned one by one, in a fixed order (event time, collection time, dedup key):

    prepare  → normalise and validate (invalid → `discarded`, with a reason; nothing is invented)
    exact    → same arXiv id / DOI / content hash as an earlier item → `exact_duplicate`
    near     → near-duplicate of an earlier item                      → `near_duplicate`
    story    → same event as an existing cluster                      → `related_report`
    else     → a new cluster                                          → `primary`

Clusters are never merged after the fact and an item is never moved between clusters, so a cluster id
that Stage 3.3 has seen stays valid until its items expire. An exception while handling one item marks
only that item `failed`; it is retried on the next run.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.ingestion.errors import UnsafeUrlError
from app.ingestion.url_safety import validate_url
from app.models.enums import DedupRelation, IngestionItemStatus
from app.processing.features import Features, build_features
from app.processing.matching import Match, exact_match_keys, near_duplicate, story_match, vetoes
from app.processing.models import ALGORITHM_VERSION, ID_NAMESPACE, ItemUpdate, StagedItem, Thresholds
from app.processing.text import (
    InvalidField,
    instruction_flags,
    neutralize_special_tokens,
    normalize_body,
    normalize_datetime,
    normalize_publisher,
    normalize_title,
    site_of,
)

logger = logging.getLogger("app.processing")

MAX_EXCERPT = 2000
_DUPLICATE_RELATIONS = {DedupRelation.EXACT_DUPLICATE, DedupRelation.NEAR_DUPLICATE}
# Statuses that Stage 3.2 (re)processes. `processed` items are context; `discarded` ones are ignored.
TO_PROCESS = {IngestionItemStatus.PENDING.value, IngestionItemStatus.FAILED.value, IngestionItemStatus.PROCESSING.value}


@dataclass
class Prepared:
    """A validated, normalised article plus its features."""

    item: StagedItem
    title: str
    excerpt: str | None
    publisher: str | None
    site: str | None
    published_at: datetime | None
    newsletter_date: datetime | None
    features: Features
    flags: list[str]
    cluster_id: uuid.UUID | None = None
    relation: str | None = None
    duplicate_of: uuid.UUID | None = None


def cluster_id_for(founding_item: StagedItem) -> uuid.UUID:
    return uuid.uuid5(ID_NAMESPACE, f"cluster:{founding_item.dedup_key}")


def prepare(item: StagedItem, *, now: datetime, thresholds: Thresholds) -> Prepared:
    """Normalise and validate one staged item. Raises InvalidField with a short reason code."""
    flags = instruction_flags(item.title, item.excerpt)
    title = neutralize_special_tokens(normalize_title(item.title)).strip()
    if not title:
        raise InvalidField("missing_title")
    try:
        # Stage 3.1's SSRF-aware validator; article links are stored, not fetched, so http is accepted.
        validate_url(item.original_url, allow_http=True)
        validate_url(item.canonical_url, allow_http=True)
    except UnsafeUrlError as exc:
        raise InvalidField("unsafe_or_malformed_url") from exc
    excerpt = normalize_body(item.excerpt, max_length=MAX_EXCERPT)
    if excerpt:
        excerpt = neutralize_special_tokens(excerpt).strip() or None
    published_at = normalize_datetime(item.published_at, now=now)
    newsletter_date = normalize_datetime((item.extra or {}).get("newsletter_date"), now=now)
    collected_at = normalize_datetime(item.collected_at, now=now) or now
    if published_at:
        event_time, basis = published_at, "published"
    elif newsletter_date:
        event_time, basis = newsletter_date, "newsletter"
    else:
        event_time, basis = collected_at, "collected"
    site = site_of(item.original_url)
    features = build_features(
        title=title,
        body=excerpt,
        url=item.original_url,
        site=site,
        event_time=event_time,
        time_basis=basis,
        min_body_tokens=thresholds.min_body_tokens,
    )
    return Prepared(
        item=item,
        title=title,
        excerpt=excerpt,
        publisher=normalize_publisher(item.publisher),
        site=site,
        published_at=published_at,
        newsletter_date=newsletter_date,
        features=features,
        flags=flags,
    )


def _details(p: Prepared, *, match: Match | None, matched: Prepared | None, decision: str) -> dict[str, Any]:
    f = p.features
    details: dict[str, Any] = {
        "version": ALGORITHM_VERSION,
        "decision": decision,
        "event_type": f.event_type,
        "event_scores": f.event_scores,
        "organisations": sorted(f.organisations),
        "products": sorted(f.products),
        "benchmarks": sorted(f.benchmarks),
        "identifiers": f.identifiers,
        "site": p.site,
        "time_basis": f.time_basis,
        "event_time": f.event_time.isoformat(),
        "flags": p.flags,
    }
    if match is not None and matched is not None:
        details["match"] = {"item_id": str(matched.item.id), "score": match.score, "reasons": match.reasons}
    return details


@dataclass
class ProcessResult:
    updates: list[ItemUpdate] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


class _State:
    """Processed articles seen so far, indexed for the exact layer, and clusters in creation order."""

    def __init__(self) -> None:
        self.items: list[Prepared] = []
        self.by_id: dict[uuid.UUID, Prepared] = {}
        self.exact: dict[str, Prepared] = {}
        self.clusters: dict[uuid.UUID, list[Prepared]] = {}
        # Blocking index: only articles sharing a headline word, an entity or a text shingle can match
        # (every rule below requires one of these), so most pairs are never compared.
        self.postings: dict[str, list[Prepared]] = {}
        self.position: dict[uuid.UUID, int] = {}

    @staticmethod
    def _keys(p: Prepared) -> set[str]:
        f = p.features
        return (
            {f"t:{w}" for w in f.title_tokens} | {f"e:{e}" for e in f.entity_keys} | {f"s:{x}" for x in f.body_shingles}
        )

    def add(self, p: Prepared) -> None:
        self.position[p.item.id] = len(self.items)
        self.items.append(p)
        self.by_id[p.item.id] = p
        for key in exact_match_keys(p.features):
            self.exact.setdefault(key, p)
        self.clusters.setdefault(p.cluster_id, []).append(p)
        for key in self._keys(p):
            self.postings.setdefault(key, []).append(p)

    def candidates(self, p: Prepared) -> list[Prepared]:
        """Earlier articles that share at least one blocking key with p, in insertion order."""
        seen: dict[uuid.UUID, Prepared] = {}
        for key in self._keys(p):
            for other in self.postings.get(key, ()):
                seen.setdefault(other.item.id, other)
        return sorted(seen.values(), key=lambda o: self.position[o.item.id])

    def root(self, p: Prepared) -> Prepared:
        """The article a duplicate ultimately duplicates (duplicates always point at a non-duplicate)."""
        seen = set()
        while p.duplicate_of is not None and p.duplicate_of in self.by_id and p.item.id not in seen:
            seen.add(p.item.id)
            p = self.by_id[p.duplicate_of]
        return p


def _assign(p: Prepared, state: _State, t: Thresholds) -> tuple[str, Match | None, Prepared | None]:
    # 1. exact duplicates: deterministic identity keys
    for key in exact_match_keys(p.features):
        hit = state.exact.get(key)
        if hit is not None:
            root = state.root(hit)
            p.cluster_id, p.relation, p.duplicate_of = root.cluster_id, DedupRelation.EXACT_DUPLICATE, root.item.id
            reason = "same_identifier:" + key[3:] if key.startswith("id:") else "same_content_hash"
            return "exact_duplicate", Match(1.0, [reason]), root
    # 2. near duplicates: the best-scoring earlier article
    candidates = state.candidates(p)
    best: tuple[Match, Prepared] | None = None
    for other in candidates:
        match = near_duplicate(p.features, other.features, t)
        if match and (best is None or match.score > best[0].score):
            best = (match, other)
    if best is not None:
        root = state.root(best[1])
        p.cluster_id, p.relation, p.duplicate_of = root.cluster_id, DedupRelation.NEAR_DUPLICATE, root.item.id
        return "near_duplicate", best[0], best[1]
    # 3. same story. To stop chaining (A~B, B~C, so A~C drifts), an article joins a cluster only if it
    # matches the cluster's founding article or at least half of its distinct reports, and no member vetoes it.
    best_cluster: tuple[Match, Prepared, uuid.UUID] | None = None
    candidate_clusters = list(dict.fromkeys(c.cluster_id for c in candidates))
    for cluster_id in candidate_clusters:
        members = state.clusters[cluster_id]
        reports = [m for m in members if m.relation not in _DUPLICATE_RELATIONS]
        founder = next((m for m in reports if m.relation == DedupRelation.PRIMARY), reports[0] if reports else None)
        if founder is None or abs((p.features.event_time - founder.features.event_time).total_seconds()) > (
            t.story_window_hours * 3600
        ):
            continue  # the story window is counted from the cluster's first report
        if any(vetoes(p.features, m.features) for m in members):
            continue
        matches = [(story_match(p.features, m.features, t), m) for m in reports]
        hits = [(match, m) for match, m in matches if match is not None]
        if not hits:
            continue
        founder_hit = any(m is founder for _, m in hits)
        if not founder_hit and len(hits) * 2 < len(reports):
            continue
        match, member = max(hits, key=lambda h: h[0].score)
        if best_cluster is None or match.score > best_cluster[0].score:
            best_cluster = (match, member, cluster_id)
    if best_cluster is not None:
        p.cluster_id, p.relation = best_cluster[2], DedupRelation.RELATED_REPORT
        return "related_report", best_cluster[0], best_cluster[1]
    # 4. a new story
    p.cluster_id, p.relation = cluster_id_for(p.item), DedupRelation.PRIMARY
    return "primary", None, None


def process_items(items: list[StagedItem], *, thresholds: Thresholds, now: datetime) -> ProcessResult:
    result = ProcessResult()
    counts = dict.fromkeys(
        ["loaded", "already_processed", "to_process", "processed", "discarded", "failed", "exact_duplicate",
         "near_duplicate", "related_report", "primary", "kept_assignment", "flagged"],
        0,
    )  # fmt: skip
    counts["loaded"] = len(items)
    state = _State()

    done = [i for i in items if i.processing_status == IngestionItemStatus.PROCESSED.value and i.cluster_id]
    todo = [
        i
        for i in items
        if i.processing_status in TO_PROCESS or (i.processing_status == "processed" and not i.cluster_id)
    ]
    counts["already_processed"], counts["to_process"] = len(done), len(todo)

    # Context: earlier decisions, in a deterministic order. They are never revised.
    for item in sorted(done, key=lambda i: (i.processed_at or i.collected_at, i.collected_at, i.dedup_key)):
        try:
            p = prepare(item, now=now, thresholds=thresholds)
        except Exception:  # noqa: BLE001, S112 - an old row that no longer validates is not context
            continue
        p.cluster_id, p.relation, p.duplicate_of = item.cluster_id, item.dedup_relation, item.duplicate_of_id
        state.add(p)

    prepared: list[Prepared] = []
    for item in todo:
        try:
            prepared.append(prepare(item, now=now, thresholds=thresholds))
        except InvalidField as exc:
            counts["discarded"] += 1
            result.updates.append(
                ItemUpdate(
                    id=item.id,
                    processing_status=IngestionItemStatus.DISCARDED.value,
                    processed_at=now,
                    processing={"version": ALGORITHM_VERSION, "decision": "discarded", "reason": str(exc)},
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad item never stops the run
            counts["failed"] += 1
            logger.exception("processing failed for one staged item", extra={"source_key": item.source_key})
            result.updates.append(_failed(item, exc, now))

    prepared.sort(key=lambda p: (p.features.event_time, p.item.collected_at, p.item.dedup_key))
    for p in prepared:
        try:
            if p.item.cluster_id and p.item.dedup_relation:
                # Re-queued (for example a new sighting): keep the earlier, stable assignment.
                p.cluster_id, p.relation, p.duplicate_of = (
                    p.item.cluster_id,
                    p.item.dedup_relation,
                    p.item.duplicate_of_id,
                )
                decision, match, matched = "kept_assignment", None, None
            else:
                decision, match, matched = _assign(p, state, thresholds)
            state.add(p)
            counts[decision] += 1
            counts["processed"] += 1
            counts["flagged"] += bool(p.flags)
            result.updates.append(
                ItemUpdate(
                    id=p.item.id,
                    processing_status=IngestionItemStatus.PROCESSED.value,
                    processed_at=now,
                    processing=_details(p, match=match, matched=matched, decision=decision),
                    cluster_id=p.cluster_id,
                    dedup_relation=str(p.relation),
                    duplicate_of_id=p.duplicate_of,
                    content_hash=p.features.content_hash,
                )
            )
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            logger.exception("processing failed for one staged item", extra={"source_key": p.item.source_key})
            result.updates.append(_failed(p.item, exc, now))
    counts["clusters_touched"] = len({u.cluster_id for u in result.updates if u.cluster_id})
    counts["clusters_new"] = counts["primary"]
    result.counts = counts
    return result


def _failed(item: StagedItem, exc: BaseException, now: datetime) -> ItemUpdate:
    return ItemUpdate(
        id=item.id,
        processing_status=IngestionItemStatus.FAILED.value,
        processed_at=now,
        processing={"version": ALGORITHM_VERSION, "decision": "failed", "error_type": type(exc).__name__},
    )
