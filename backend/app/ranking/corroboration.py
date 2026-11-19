"""Independent corroboration inside one Stage 3.2 cluster. Counts reports, not articles.

- Exact and near duplicates and repeated sightings of one URL are grouped with the report they copy (Stage 3.2
  relations, resolved by `build_evidence`), so syndication counts once.
- Distinct publishers among the remaining reports are candidates for independence. Whether a report adds its own
  reporting or only rewrites one announcement or press release is a semantic judgement made by the Gemini
  evaluation (`independent_reporting_ids`); code then counts publishers.
- Other clusters the evaluation judged to be the same event (with high embedding similarity) may add publishers
  (`extra_publishers`); they are listed in the explanation.
- When the publisher of a report cannot be identified, independence is "unclear" and no count is claimed.

This never consults anything outside the collected material: external corroboration is unavailable and nothing
is fact-checked.
"""

from app.intelligence.evidence import EvidencePack, EvidenceSource
from app.ranking.contract import CorroborationResult
from app.ranking.credibility import PRIMARY_KINDS


def _publisher_known(s: EvidenceSource) -> bool:
    return bool(s.ref.publisher or s.ref.site)


def assess_corroboration(
    pack: EvidencePack, independent_ids: set[str], extra_publishers: set[str] | None = None
) -> CorroborationResult:
    extra = set(extra_publishers or ())
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

    extra -= publishers
    if extra:
        limitations.append(f"{len(extra)} further publisher(s) from separate clusters judged to be the same event")
    if len(publishers) <= 1 and not extra:
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
    adding = [r for r in others if r.ref_id in independent_ids]
    added_publishers = {r.publisher_key for r in adding} | extra

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
