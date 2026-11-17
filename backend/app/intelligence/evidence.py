"""The evidence pack: what the model is allowed to see about a cluster, plus the facts code needs to check it.

Built only from the Stage 3.2 `StoryCluster`. Each source reference gets an opaque id (S1, S2, ...) in the
cluster's own order, so citations can be checked and mapped back without the model ever writing a URL.
Duplicate copies (exact, near, same URL) are grouped with the article they copy: they share one
`report_group`, so syndication never counts as independent confirmation.

Input budget: at most AI_MAX_SOURCES_PER_STORY sources are sent (independent reports first, copies last), and
excerpts are shortened until the estimated prompt fits AI_MAX_INPUT_TOKENS. Sources that were not sent stay
in the analysis's provenance with `sent_to_model = false`.
"""

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime

from app.intelligence.contract import AnalysisSource
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
EXCERPT_STEPS = (1500, 800, 400, 200)
CHARS_PER_TOKEN = 4  # conservative estimate for English text; the real count comes back in usage metadata


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
    sent: frozenset[str] = frozenset()  # ref_ids included in the prompt (empty = all)
    excerpt_chars: int = EXCERPT_CHARS

    def is_sent(self, ref_id: str) -> bool:
        return not self.sent or ref_id in self.sent

    @property
    def sent_sources(self) -> list[EvidenceSource]:
        return [s for s in self.sources if self.is_sent(s.ref_id)]

    @property
    def ref_ids(self) -> set[str]:
        """Ids the model may cite: the sources it was shown."""
        return {s.ref_id for s in self.sent_sources}

    def by_id(self, ref_id: str) -> EvidenceSource | None:
        return next((s for s in self.sources if s.ref_id == ref_id), None)

    def texts(self) -> list[str]:
        out = [self.cluster.title, self.cluster.description or "", self.cluster.excerpt or ""]
        for s in self.sent_sources:
            out += [
                s.ref.title,
                (s.ref.excerpt or "")[: self.excerpt_chars],
                s.ref.publisher or "",
                s.ref.source_name or "",
            ]
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

    def contract_sources(self) -> list[AnalysisSource]:
        return [
            AnalysisSource(
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
                sent_to_model=self.is_sent(s.ref_id),
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


def evidence_document(pack: EvidencePack, *, analysis_date: datetime | None = None) -> dict:
    """The JSON object placed inside the delimited evidence block of the prompt. Data only."""
    c = pack.cluster
    return {
        "analysis_date": analysis_date.date().isoformat() if analysis_date else None,
        "event_type_detected_by_rules": c.event_type,
        "entities_detected_by_rules": c.entities.model_dump(mode="json"),
        "earliest_published_at": c.earliest_published_at.isoformat() if c.earliest_published_at else None,
        "content_flags": c.content_flags,
        "sources": [
            {
                "id": s.ref_id,
                "relation": s.ref.relation,
                "is_copy": s.report_group != s.ref_id,
                "copy_of": s.report_group if s.report_group != s.ref_id and pack.is_sent(s.report_group) else None,
                "source_kind": s.ref.source_kind,
                "source_name": s.ref.source_name,
                "publisher": s.ref.publisher,
                "site": s.ref.site,
                "published_at": s.ref.published_at.date().isoformat() if s.ref.published_at else None,
                "title": s.ref.title,
                "excerpt": (s.ref.excerpt or "")[: pack.excerpt_chars] or None,
                "content_flags": s.ref.content_flags,
            }
            for s in pack.sent_sources
        ],
    }


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def fit_to_budget(pack: EvidencePack, *, max_sources: int, max_input_tokens: int, overhead_tokens: int) -> EvidencePack:
    """Choose the sources to send (independent reports first, in cluster order) and the excerpt length, so the
    estimated prompt fits the input budget. Never drops below one source."""
    independent = [s.ref_id for s in pack.sources if s.report_group == s.ref_id]
    copies = [s.ref_id for s in pack.sources if s.report_group != s.ref_id]
    chosen = (independent + copies)[:max_sources]
    candidate = pack
    for chars in EXCERPT_STEPS:
        candidate = replace(pack, sent=frozenset(chosen), excerpt_chars=chars)
        size = estimate_tokens(json.dumps(evidence_document(candidate), ensure_ascii=False))
        if size + overhead_tokens <= max_input_tokens:
            return candidate
    while len(chosen) > 1:
        chosen = chosen[:-1]
        candidate = replace(pack, sent=frozenset(chosen), excerpt_chars=EXCERPT_STEPS[-1])
        size = estimate_tokens(json.dumps(evidence_document(candidate), ensure_ascii=False))
        if size + overhead_tokens <= max_input_tokens:
            return candidate
    return candidate
