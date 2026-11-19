"""Independent corroboration inside one Stage 3.2 cluster. Counts reports, not articles.

- Exact and near duplicates and repeated sightings of one URL are grouped with the report they copy (Stage 3.2
  relations, resolved by `build_evidence`), so syndication counts once.
- Distinct publishers among the remaining reports are candidates for independence. A report that adds almost
  nothing beyond the official announcement (or beyond the first report) is treated as repeating it.
- When the publisher of a report cannot be identified, independence is "unclear" and no count is claimed.

This never consults anything outside the cluster: the system has not fact-checked anything externally.
"""

from app.intelligence.evidence import EvidencePack, EvidenceSource
from app.ranking.config import RankingConfig
from app.ranking.contract import CorroborationResult
from app.ranking.credibility import PRIMARY_KINDS
from app.ranking.text import novelty


def _text(s: EvidenceSource) -> str:
    return f"{s.ref.title} {s.ref.excerpt or ''}"


def _publisher_known(s: EvidenceSource) -> bool:
    return bool(s.ref.publisher or s.ref.site)


def assess_corroboration(pack: EvidencePack, config: RankingConfig) -> CorroborationResult:
    threshold = config.verification.repeat_novelty_threshold
    sources = pack.sources
    reports = [s for s in sources if s.report_group == s.ref_id]
    copies = len(sources) - len(reports)
    limitations: list[str] = ["corroboration is assessed within the collected sources only; no external fact-check"]
    official = [r for r in reports if r.ref.source_kind in PRIMARY_KINDS]
    publishers = {r.publisher_key for r in reports}
    base = {
        "source_count": len(sources),
        "report_count": len(reports),
        "official_source_present": bool(official),
    }
    if copies:
        limitations.append(f"{copies} duplicate or syndicated cop{'y' if copies == 1 else 'ies'} counted once")

    if len(reports) > 1 and not all(_publisher_known(r) for r in reports):
        return CorroborationResult(
            level="unclear",
            independent_source_count=None,
            explanation="Several reports, but at least one has no identifiable publisher, so independence "
            "cannot be judged.",
            limitations=limitations,
            **base,
        )

    if len(publishers) <= 1:
        if copies:
            return CorroborationResult(
                level="syndicated_single_source",
                independent_source_count=1,
                explanation="All copies trace back to one report from a single publisher.",
                limitations=limitations,
                **base,
            )
        return CorroborationResult(
            level="single_source",
            independent_source_count=1,
            explanation="Reported by a single source; no independent corroboration in the collected material.",
            limitations=limitations,
            **base,
        )

    # Several publishers. Do the others add their own reporting, or repeat one account?
    lead = official or sorted(reports, key=lambda r: (r.ref.published_at is None, r.ref.published_at, r.ref_id))[:1]
    lead_keys = {r.publisher_key for r in lead}
    others = [r for r in reports if r.publisher_key not in lead_keys]
    no_text = [r for r in others if not r.ref.excerpt]
    if no_text:
        limitations.append(
            f"{len(no_text)} report(s) have no article text, so their independence is assumed by publisher"
        )
    adding = [r for r in others if not r.ref.excerpt or novelty(_text(r), [_text(x) for x in lead]) >= threshold]
    added_publishers = {r.publisher_key for r in adding}

    if not added_publishers:
        who = "the official announcement" if official else "the first report"
        return CorroborationResult(
            level="repeated_announcement",
            independent_source_count=1,
            explanation=f"{len(publishers)} publishers, but the other reports mostly repeat {who}; counted as one "
            "account.",
            limitations=limitations,
            **base,
        )
    independent = len(lead_keys) + len(added_publishers)
    if official:
        return CorroborationResult(
            level="official_plus_independent",
            independent_source_count=independent,
            explanation=f"Primary source plus {len(added_publishers)} independent report(s) from other publishers.",
            limitations=limitations,
            **base,
        )
    return CorroborationResult(
        level="multiple_independent",
        independent_source_count=independent,
        explanation=f"{independent} publishers report the story with their own wording or details.",
        limitations=limitations,
        **base,
    )
