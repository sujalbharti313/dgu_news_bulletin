"""Ranking order, diversity-aware shortlist selection and the explanation for every story's position.

1. Every evaluated story is ranked by final score (ties: evidence quality, then source credibility, then id).
2. Only `eligible` stories at or above `shortlist.min_score` can be shortlisted. The shortlist may be shorter
   than `target`: weak stories are never added to fill slots.
3. Walking down the ranking, a story is skipped when its primary category already has `max_per_category`
   picks or its organisation `max_per_organisation`, unless it is exceptional (score >= exceptional_score).
4. Coverage: if a configured coverage group (research, India, robotics, ...) is unrepresented and an eligible
   story from it scores within `coverage_swap_margin` of the lowest non-exceptional pick, it replaces that pick
   (never an exceptional one, never one that is the only representative of another group).
Every decision is written into the story's `selection_note`, so the shortlist is explainable.
"""

from collections import Counter

from app.ranking.config import DIMENSIONS, RankingConfig
from app.ranking.evaluate import Evaluated

LABELS = {
    "global_impact": "global impact",
    "technical_significance": "technical significance",
    "student_relevance": "student relevance",
    "evidence_quality": "evidence quality",
    "source_credibility": "source credibility",
    "timeliness": "timeliness",
}
HYPE_TEXT = {
    "marketing_language": "marketing language in the sources",
    "promotional_language": "promotional wording",
    "vague_ai_claim": "vague AI claims",
    "unsupported_superiority_claim": "superiority claims without benchmarks",
    "unverified_benchmark_claim": "benchmark figures not independently reproduced",
    "speculation_as_certainty": "speculation presented as certainty",
    "marketing_only_announcement": "mostly a marketing announcement",
    "sensational_source_headline": "a source headline stronger than its article",
}
CORROBORATION_TEXT = {
    "multiple_independent": "several independent reports",
    "official_plus_independent": "a primary source plus independent reporting",
    "repeated_announcement": "several articles repeating one announcement",
    "syndicated_single_source": "one report and its syndicated copies",
    "single_source": "a single source",
    "unclear": "sources whose independence is unclear",
}


def rank_key(e: Evaluated) -> tuple:
    return (
        -e.final,
        -e.verification.evidence_quality_score,
        -e.verification.source_credibility_score,
        str(e.cluster_id),
    )


def rank(stories: list[Evaluated]) -> list[Evaluated]:
    return sorted(stories, key=rank_key)


def _org(e: Evaluated) -> str | None:
    return e.organisation.casefold() if e.organisation else None


def select_shortlist(ranked: list[Evaluated], config: RankingConfig) -> tuple[list[Evaluated], list[str]]:
    sl = config.shortlist
    notes: list[str] = []
    selected: list[Evaluated] = []
    per_cat: Counter[str] = Counter()
    per_org: Counter[str] = Counter()
    skipped_for_cap: set = set()
    for e in ranked:
        if e.eligibility != "eligible":
            e.selection_note = f"not shortlisted: {e.eligibility.replace('_', ' ')}"
            continue
        if e.final < sl.min_score:
            e.selection_note = f"not shortlisted: score {e.final} is below the shortlist minimum {sl.min_score}"
            continue
        if len(selected) >= sl.target:
            e.selection_note = f"not shortlisted: the shortlist is full ({sl.target} stories)"
            continue
        exceptional = e.final >= sl.exceptional_score
        cat = e.analysis.primary_category_key
        org = _org(e)
        if not exceptional and per_cat[cat] >= sl.max_per_category:
            e.selection_note = f"not shortlisted: already {sl.max_per_category} {e.analysis.primary_category} stories"
            skipped_for_cap.add(e.cluster_id)
            continue
        if not exceptional and org and per_org[org] >= sl.max_per_organisation:
            e.selection_note = f"not shortlisted: already {sl.max_per_organisation} stories about {e.organisation}"
            skipped_for_cap.add(e.cluster_id)
            continue
        selected.append(e)
        per_cat[cat] += 1
        if org:
            per_org[org] += 1
        e.selection_note = "shortlisted" + (" (exceptional: diversity caps do not apply)" if exceptional else "")

    # Coverage swaps.
    for group in sl.coverage_groups:
        if any(group in e.coverage_groups for e in selected):
            continue
        pool = [
            e
            for e in ranked
            if e not in selected
            and e.eligibility == "eligible"
            and e.final >= sl.min_score
            and group in e.coverage_groups
            and e.cluster_id not in skipped_for_cap
            and not (_org(e) and per_org[_org(e)] >= sl.max_per_organisation)
        ]
        if not pool:
            continue
        candidate = pool[0]
        if len(selected) < sl.target:  # room left: it was below min_score or capped; nothing to swap
            continue

        def sole_cover(s: Evaluated) -> bool:
            return any(sum(1 for x in selected if g in x.coverage_groups) == 1 for g in s.coverage_groups)

        removable = [s for s in selected if s.final < sl.exceptional_score and not sole_cover(s)]
        if not removable:
            continue
        lowest = min(removable, key=lambda s: (s.final, str(s.cluster_id)))
        if candidate.final < lowest.final - sl.coverage_swap_margin:
            continue
        selected.remove(lowest)
        per_cat[lowest.analysis.primary_category_key] -= 1
        if _org(lowest):
            per_org[_org(lowest)] -= 1
        selected.append(candidate)
        per_cat[candidate.analysis.primary_category_key] += 1
        if _org(candidate):
            per_org[_org(candidate)] += 1
        candidate.selection_note = f"shortlisted for coverage of '{group}' (within {sl.coverage_swap_margin} points)"
        lowest.selection_note = f"not shortlisted: replaced by a '{group}' story for coverage"
        notes.append(f"coverage swap: '{group}' story {candidate.cluster_id} replaced {lowest.cluster_id}")

    selected.sort(key=rank_key)
    if len(selected) < sl.target:
        notes.append(
            f"only {len(selected)} stor{'y' if len(selected) == 1 else 'ies'} met the quality bar "
            f"(target {sl.target}); the shortlist was not filled with weaker stories"
        )
    return selected, notes


def explain(e: Evaluated, *, shortlist_rank: int | None) -> str:
    """A grounded, concise explanation built only from the story's scores, verification and gates."""
    s = e.scores
    v = e.verification
    scores = {d: getattr(s, d) for d in DIMENSIONS}
    parts = [f"{e.priority} priority ({s.final_priority_score})."]
    strong = [f"{LABELS[d]} {scores[d]}" for d in DIMENSIONS if scores[d] is not None and scores[d] >= 70]
    weak = [f"{LABELS[d]} {scores[d]}" for d in DIMENSIONS if scores[d] is not None and scores[d] < 40]
    if strong:
        parts.append("Strengths: " + ", ".join(strong) + ".")
    evidence = f"Evidence: {CORROBORATION_TEXT[v.corroboration_level]}"
    if v.source_credibility_level in ("unknown", "provisional"):
        evidence += f", source credibility {v.source_credibility_level}"
    if v.content_nature in ("opinion", "speculation", "promotional"):
        evidence += f", content is mainly {v.content_nature}"
    parts.append(evidence + ".")
    limits = weak + [HYPE_TEXT[h] for h in v.hype_flags if h in HYPE_TEXT]
    if s.unavailable_dimensions:
        limits.append("not assessable: " + ", ".join(LABELS[d] for d in s.unavailable_dimensions))
    if limits:
        parts.append("Limits: " + "; ".join(limits[:5]) + ".")
    blocking = [g for g in e.gates if g.action != "eligible"]
    if blocking:
        label = "Ineligible" if e.eligibility == "ineligible" else "Needs review"
        parts.append(f"{label}: " + " ".join(dict.fromkeys(g.reason for g in blocking))[:400])
    if shortlist_rank is not None:
        parts.append(
            f"Shortlist position {shortlist_rank}"
            + (f": {e.selection_note}." if e.selection_note and e.selection_note != "shortlisted" else ".")
        )
    elif e.selection_note and e.eligibility == "eligible":
        parts.append(e.selection_note[0].upper() + e.selection_note[1:] + ".")
    return " ".join(parts)
