"""Source credibility: what kind of source each reference is, and how much weight its account deserves.

Order of evidence for a source's level:
1. A reviewed entry in `config/source_registry.toml` for the article's site (domain suffix) or its ingestion
   source key → that level (basis "registry").
2. The Stage 3.1 source category (official, research, government, news, newsletter) → PROVISIONAL, because the
   source was configured for collection but nobody has reviewed it here (basis "ingestion_category").
3. Nothing → UNKNOWN (basis "none"). Unknown is not "unreliable": it means unevaluated.

A newsletter summary is secondary aggregation, whatever site it links to. Primary sources (an organisation's
own channel, a government, a research repository) are the original source for their own announcements, which
makes them authoritative about *what was announced*, not independent confirmation of their own claims.
"""

from urllib.parse import urlsplit

from app.intelligence.evidence import EvidencePack, EvidenceSource
from app.ranking.config import RankingConfig, RegistryEntry, SourceRegistry
from app.ranking.contract import SourceAssessment

PRIMARY_KINDS = {"official_channel", "government", "research_repository"}
_RESERVED_SUFFIXES = (".invalid", ".test", ".localhost")


def valid_article_url(url: str | None) -> bool:
    """http(s) with a real-looking host. Reserved test domains (`.invalid`) are not article URLs."""
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").casefold()
    if parts.scheme not in ("http", "https") or not host or "." not in host:
        return False
    return not host.endswith(_RESERVED_SUFFIXES)


def _entry_level(entry: RegistryEntry) -> tuple[str, list[str], list[str]]:
    reasons = [
        f"registry: {entry.kind.replace('_', ' ')}" + (f" of {entry.organisation}" if entry.organisation else "")
    ]
    if entry.notes:
        reasons.append(entry.notes)
    if not entry.reviewed_on:
        return "provisional", reasons, ["registry entry has no review date, so its level is provisional"]
    return entry.level, reasons, []


def assess_source(source: EvidenceSource, registry: SourceRegistry, config: RankingConfig) -> SourceAssessment:
    ref = source.ref
    cred = config.credibility
    reasons: list[str] = []
    limitations: list[str] = []
    key_entry = registry.by_source_key(ref.source_key)
    site_entry = registry.by_site(ref.site)
    reviewed_on: str | None = None
    basis = "none"

    if ref.source_kind == "newsletter" or (key_entry is not None and key_entry.kind == "newsletter"):
        level = "aggregator"
        basis = "registry" if key_entry is not None else "ingestion_category"
        reviewed_on = key_entry.reviewed_on if key_entry else None
        reasons.append("newsletter summary: secondary aggregation, not original reporting")
        score = cred.aggregator
        if site_entry is not None and site_entry.reviewed_on and site_entry.kind != "newsletter":
            linked = cred.score(site_entry.level) - cred.newsletter_penalty
            if linked > score:
                score = linked
                reasons.append(f"links to {ref.site} ({site_entry.kind.replace('_', ' ')} in the registry)")
        limitations.append("the linked article itself was not collected; only the newsletter's summary was")
    else:
        entry = site_entry or key_entry
        if entry is not None:
            level, entry_reasons, entry_limits = _entry_level(entry)
            reasons += entry_reasons
            limitations += entry_limits
            basis, reviewed_on = "registry", entry.reviewed_on
        elif ref.source_kind != "unknown":
            level, basis = "provisional", "ingestion_category"
            reasons.append(f"configured for collection as {ref.source_kind.replace('_', ' ')}; not reviewed")
            limitations.append("source not yet reviewed in the source registry")
        else:
            level = "unknown"
            reasons.append("source not in the registry and not a configured source")
            limitations.append("credibility unknown: not independently evaluated (this is not a judgement of falsity)")
        score = cred.score(level)

    url_ok = valid_article_url(ref.original_url)
    if not url_ok:
        limitations.append("original article URL missing or invalid")
    if not (ref.publisher or ref.site):
        limitations.append("publisher not identified")
        score = min(score, cred.unknown)
    if ref.content_flags:
        limitations.append("source text contains flagged content (" + ", ".join(ref.content_flags[:3]) + ")")
    return SourceAssessment(
        ref_id=source.ref_id,
        item_id=ref.item_id,
        publisher=ref.publisher,
        site=ref.site,
        source_key=ref.source_key,
        source_kind=ref.source_kind,
        relation=ref.relation,
        is_copy=source.report_group != source.ref_id,
        original_url_valid=url_ok,
        credibility_level=level,
        credibility_score=max(0, min(100, score)),
        basis=basis,
        reviewed_on=reviewed_on,
        reasons=reasons,
        limitations=limitations,
    )


def cluster_credibility(
    assessments: list[SourceAssessment], independent: int | None, config: RankingConfig
) -> tuple[int, str, list[str]]:
    """(score, level, reasons) for the story: the best independent report, plus a bounded bonus for each further
    independent publisher. Copies never add anything."""
    reports = [a for a in assessments if not a.is_copy] or assessments
    best = max(reports, key=lambda a: (a.credibility_score, a.ref_id))
    cred = config.credibility
    bonus = 0
    if independent and independent > 1:
        bonus = min(cred.max_corroboration_bonus, cred.corroboration_bonus * (independent - 1))
    score = max(0, min(100, best.credibility_score + bonus))
    reasons = [
        f"best report {best.ref_id} ({best.publisher or best.site or best.source_key}): {best.credibility_level}"
    ]
    if bonus:
        reasons.append(f"+{bonus} for {independent - 1} further independent publisher(s)")
    return score, best.credibility_level, reasons


def assess_all(pack: EvidencePack, registry: SourceRegistry, config: RankingConfig) -> list[SourceAssessment]:
    return [assess_source(s, registry, config) for s in pack.sources]
