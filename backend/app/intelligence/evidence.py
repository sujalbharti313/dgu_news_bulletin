"""The evidence pack: what the model is allowed to see about a cluster, plus the facts code needs to check it.

Built only from the Stage 3.2 `StoryCluster`. Each source reference gets an opaque id (S1, S2, ...) in the
cluster's own order, so citations can be checked and mapped back without the model ever writing a URL.
Duplicate copies (exact, near, same URL) are grouped with the article they copy: they share one
`report_group`, so syndication never counts as independent confirmation.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from app.intelligence.contract import IntelligenceSource
from app.processing.contract import SourceReference, StoryCluster

_DUPLICATE_RELATIONS = {"exact_duplicate", "near_duplicate", "same_url"}
_OFFICIAL_KINDS = {"official_channel", "government"}
_WORD = re.compile(r"\w+", re.UNICODE)
# Numbers as written in prose: 2, 80%, 1.5, 1,000, 2M. Captures the digits only.
_NUMBER = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")
# Source citations written inline ("(S2)") are ids, not numbers.
_SOURCE_ID_TOKEN = re.compile(r"\bS\d{1,3}\b")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")
EXCERPT_CHARS = 1500


def normalise_number(raw: str) -> str:
    value = raw.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.lstrip("0") or "0"


def numbers_in(text: str | None) -> set[str]:
    return {normalise_number(m) for m in _NUMBER.findall(_SOURCE_ID_TOKEN.sub(" ", text or ""))}


def urls_in(text: str | None) -> list[str]:
    return [u.rstrip(".,;:") for u in _URL.findall(text or "")]


def _date_numbers(value: datetime | None) -> set[str]:
    if value is None:
        return set()
    return {str(value.year), str(value.month), str(value.day)}


@dataclass(frozen=True)
class EvidenceSource:
    ref_id: str
    ref: SourceReference
    report_group: str  # ref_id of the article this copy belongs to (itself, for an independent report)

    @property
    def publisher_key(self) -> str:
        return (self.ref.publisher or self.ref.site or self.ref.source_key).casefold()

    @property
    def is_official(self) -> bool:
        return self.ref.source_kind in _OFFICIAL_KINDS


@dataclass(frozen=True)
class EvidencePack:
    cluster: StoryCluster
    sources: list[EvidenceSource]

    @property
    def ref_ids(self) -> set[str]:
        return {s.ref_id for s in self.sources}

    def by_id(self, ref_id: str) -> EvidenceSource | None:
        return next((s for s in self.sources if s.ref_id == ref_id), None)

    def texts(self) -> list[str]:
        out = [self.cluster.title, self.cluster.description or "", self.cluster.excerpt or ""]
        for s in self.sources:
            out += [s.ref.title, s.ref.excerpt or "", s.ref.publisher or "", s.ref.source_name or ""]
        return [t for t in out if t]

    @property
    def word_count(self) -> int:
        """Distinct words of source text available (titles and excerpts of non-duplicate reports)."""
        texts = [self.cluster.title, self.cluster.excerpt or ""]
        texts += [s.ref.title + " " + (s.ref.excerpt or "") for s in self.sources if s.report_group == s.ref_id]
        return len({w.casefold() for t in texts for w in _WORD.findall(t)})

    def allowed_numbers(self) -> set[str]:
        """Every number that appears in the evidence (text and the sources' own dates)."""
        allowed: set[str] = set()
        for t in self.texts():
            allowed |= numbers_in(t)
        for s in self.sources:
            allowed |= _date_numbers(s.ref.published_at) | _date_numbers(s.ref.newsletter_date)
        return allowed

    def allowed_urls(self) -> set[str]:
        return {u for s in self.sources for u in (s.ref.original_url, s.ref.canonical_url)}

    def corroboration(self, ref_ids: list[str]) -> tuple[str, int, bool]:
        """(level, independent publishers, cites an official source) for a set of cited source ids."""
        cited = [s for s in self.sources if s.ref_id in set(ref_ids)]
        if not cited:
            return "no_sources_cited", 0, False
        groups = {s.report_group for s in cited}
        publishers = {self.by_id(g).publisher_key for g in groups if self.by_id(g)}
        level = "multiple_independent_reports" if len(publishers) >= 2 else "single_report"
        return level, len(publishers), any(s.is_official for s in cited)

    def contract_sources(self) -> list[IntelligenceSource]:
        return [
            IntelligenceSource(
                ref_id=s.ref_id,
                item_id=s.ref.item_id,
                source_key=s.ref.source_key,
                source_name=s.ref.source_name,
                source_kind=s.ref.source_kind,
                relation=s.ref.relation,
                duplicate_of=s.ref.duplicate_of,
                title=s.ref.title,
                publisher=s.ref.publisher,
                site=s.ref.site,
                original_url=s.ref.original_url,
                canonical_url=s.ref.canonical_url,
                published_at=s.ref.published_at,
                newsletter_date=s.ref.newsletter_date,
                content_flags=list(s.ref.content_flags),
            )
            for s in self.sources
        ]


def build_evidence(cluster: StoryCluster) -> EvidencePack:
    ref_ids = [f"S{i}" for i in range(1, len(cluster.sources) + 1)]
    first_ref_of_item: dict = {}
    for rid, ref in zip(ref_ids, cluster.sources, strict=True):
        if ref.relation != "same_url":
            first_ref_of_item.setdefault(ref.item_id, rid)
    sources = []
    for rid, ref in zip(ref_ids, cluster.sources, strict=True):
        group = rid
        if ref.relation in _DUPLICATE_RELATIONS and ref.duplicate_of is not None:
            group = first_ref_of_item.get(ref.duplicate_of, rid)
        sources.append(EvidenceSource(ref_id=rid, ref=ref, report_group=group))
    # Resolve chains (a copy of a copy) to the root report.
    by_id = {s.ref_id: s for s in sources}
    resolved = []
    for s in sources:
        group, seen = s.report_group, {s.ref_id}
        while group in by_id and by_id[group].report_group != group and group not in seen:
            seen.add(group)
            group = by_id[group].report_group
        resolved.append(EvidenceSource(ref_id=s.ref_id, ref=s.ref, report_group=group))
    return EvidencePack(cluster=cluster, sources=resolved)


def evidence_document(pack: EvidencePack) -> dict:
    """The JSON object placed inside the delimited evidence block of the prompt. Data only."""
    c = pack.cluster
    return {
        "story_cluster_id": str(c.cluster_id),
        "event_type_detected_by_rules": c.event_type,
        "entities_detected_by_rules": c.entities.model_dump(mode="json"),
        "earliest_published_at": c.earliest_published_at.isoformat() if c.earliest_published_at else None,
        "content_flags": c.content_flags,
        "sources": [
            {
                "id": s.ref_id,
                "relation": s.ref.relation,
                "copy_of": s.report_group if s.report_group != s.ref_id else None,
                "source_kind": s.ref.source_kind,
                "source_name": s.ref.source_name,
                "publisher": s.ref.publisher,
                "site": s.ref.site,
                "published_at": s.ref.published_at.date().isoformat() if s.ref.published_at else None,
                "title": s.ref.title,
                "excerpt": (s.ref.excerpt or "")[:EXCERPT_CHARS] or None,
                "content_flags": s.ref.content_flags,
            }
            for s in pack.sources
        ],
    }
