"""From Stage 3.4's ranking to the edition's stories.

Selection preserves Stage 3.4's decisions: the edition takes the run's diversity-aware shortlist in order
(eligible stories only, duplicates already resolved, at most N per category/organisation/topic), up to
`selection.target_stories`. It never re-ranks, never pads a short week with weaker stories and never reads news
outside the ranking run. Admins may add other candidates of the same run later (eligible ones, or review_required
ones after confirming their review), and every such change is recorded.

Each selected story is built from the Stage 3.3 analysis of the same cluster revision (text and source metadata)
and the Stage 3.4 verification (source credibility, corroboration). The source link is the original URL of the
most credible non-copy source with a valid URL, taken from the stored records, never from AI output.
"""

import uuid
from dataclasses import dataclass, field

from app.editions.content import EditionStory, Selection, StoryText, clean_text
from app.intelligence.contract import AnalysisSource, StoryAnalysis
from app.ranking.contract import RankedCandidate, RankingResult

STUDENT_AREA_LABELS = {
    "learning": "Learning",
    "research": "Research",
    "technical_skills": "Technical skills",
    "careers": "Careers",
    "internships": "Internships",
    "software_development": "Software development",
    "robotics_automation": "Robotics & automation",
    "academic_projects": "Academic projects",
}
CORROBORATION_TEXT = {
    "multiple_independent": "reported by several independent publishers",
    "official_plus_independent": "an official source plus independent reporting",
    "repeated_announcement": "one announcement repeated by several outlets",
    "syndicated_single_source": "one report, syndicated by other outlets",
    "single_source": "a single source",
    "unclear": "sources whose independence could not be judged",
}
DEFAULT_TOPIC = "research"
SELECTABLE = ("eligible", "review_required")


@dataclass
class SelectionOutcome:
    candidates: list[RankedCandidate]
    skipped: list[dict] = field(default_factory=list)


def select_candidates(ranking: RankingResult, target: int) -> SelectionOutcome:
    """The shortlist in Stage 3.4 order, defensively re-checked (eligible, not a duplicate, unique), capped."""
    out = SelectionOutcome(candidates=[])
    seen: set[uuid.UUID] = set()
    ordered = sorted(ranking.shortlist, key=lambda c: (c.shortlist_rank or 10_000, c.rank))
    for c in ordered:
        reason = None
        if c.eligibility_status != "eligible":
            reason = f"eligibility is {c.eligibility_status}"
        elif c.duplicate_of is not None:
            reason = "duplicate of another story"
        elif c.story_cluster_id in seen:
            reason = "listed twice"
        if reason:
            out.skipped.append({"clusterId": str(c.story_cluster_id), "reason": reason})
            continue
        seen.add(c.story_cluster_id)
        if len(out.candidates) >= target:
            out.skipped.append({"clusterId": str(c.story_cluster_id), "reason": "beyond the target story count"})
            continue
        out.candidates.append(c)
    return out


def addable_candidates(ranking: RankingResult, exclude: set[uuid.UUID]) -> list[RankedCandidate]:
    """Candidates an admin may add: same run, eligible or review_required, not a duplicate, not already in."""
    return [
        c
        for c in ranking.candidates
        if c.eligibility_status in SELECTABLE and c.duplicate_of is None and c.story_cluster_id not in exclude
    ]


def _credibility(candidate: RankedCandidate | None) -> dict[str, tuple[int, bool, bool]]:
    if candidate is None:
        return {}
    return {
        a.ref_id: (a.credibility_score, a.is_copy, a.original_url_valid)
        for a in candidate.verification.source_assessments
    }


def primary_source(analysis: StoryAnalysis, candidate: RankedCandidate | None) -> AnalysisSource | None:
    scores = _credibility(candidate)
    usable = [s for s in analysis.sources if s.original_url.startswith(("https://", "http://"))]
    if scores:
        usable = [s for s in usable if scores.get(s.ref_id, (0, False, True))[2]] or usable

    def key(s: AnalysisSource):
        score, is_copy, _ = scores.get(s.ref_id, (0, False, True))
        return (
            is_copy or s.relation not in ("primary", "related_report"),
            -score,
            s.item_id != analysis.representative_item_id,
            s.ref_id,
        )

    return min(usable, key=key) if usable else None


def stage33_text(analysis: StoryAnalysis) -> StoryText:
    why = analysis.why_it_matters
    why_text = " ".join(t for t in (why.established_facts, why.interpretation) if t)
    rel = analysis.student_relevance
    rel_text = rel.summary or " ".join(a.detail for a in rel.angles[:2])
    idea = ""
    if analysis.project_ideas:
        first = analysis.project_ideas[0]
        idea = f"{first.title}: {first.description}"
    return StoryText(
        headline=clean_text(analysis.headline, multiline=False),
        summary=clean_text(analysis.summary or analysis.understanding.main_event),
        technical_explanation=clean_text(analysis.technical_explanation or ""),
        why_it_matters=clean_text(why_text or analysis.understanding.what_is_new or ""),
        student_relevance=clean_text(rel_text),
        project_idea=clean_text(idea),
    )


def verification_note(candidate: RankedCandidate | None, publisher: str) -> str:
    if candidate is None:
        return (
            f"Source: {publisher}. Checked against the collected source material only; not independently fact-checked."
        )
    corr = candidate.verification.corroboration
    basis = CORROBORATION_TEXT.get(corr.level, "the collected sources")
    reports = f"{corr.report_count} distinct report{'s' if corr.report_count != 1 else ''}"
    return (
        f"Source context: {reports}, {basis}. The AI//CAMPUS summary was checked against the collected source "
        "material only and is not an independent fact-check; read the original for full detail."
    )


def build_story(
    analysis: StoryAnalysis,
    candidate: RankedCandidate | None,
    *,
    source: str,
    eligibility: str,
    ranking_run_id: uuid.UUID | None,
    acknowledged_by: uuid.UUID | None = None,
) -> EditionStory | None:
    """A snapshot story from backend records, or None when no source has a usable URL."""
    src = primary_source(analysis, candidate)
    if src is None:
        return None
    publisher = clean_text(src.publisher or src.source_name or src.site or "", multiline=False) or "Unknown publisher"
    published = src.published_at or src.newsletter_date or analysis.earliest_published_at
    text = stage33_text(analysis)
    areas = []
    for angle in analysis.student_relevance.angles:
        label = STUDENT_AREA_LABELS.get(angle.area)
        if label and label not in areas:
            areas.append(label)
    explore: list[str] = []
    for idea in analysis.project_ideas:
        for tech in idea.technologies:
            tech = clean_text(tech, multiline=False)[:40]
            if tech and tech not in explore:
                explore.append(tech)
    return EditionStory(
        cluster_id=analysis.story_cluster_id,
        cluster_revision=analysis.cluster_revision,
        text=text,
        original=text,
        source_url=src.original_url,
        source_item_id=src.item_id,
        publisher=publisher[:200],
        source_kind=src.source_kind,
        published_date=published.date() if published else None,
        category=analysis.primary_category,
        category_key=analysis.primary_category_key,
        topic=analysis.topic or DEFAULT_TOPIC,
        disciplines=areas[:6],
        explore=explore[:6],
        verification_note=verification_note(candidate, publisher),
        source_count=len(analysis.sources),
        selection=Selection(
            source=source,
            eligibility=eligibility,
            rank=candidate.rank if candidate else None,
            shortlist_rank=candidate.shortlist_rank if candidate else None,
            final_priority_score=candidate.final_scores.final_priority_score if candidate else None,
            priority=candidate.priority_classification if candidate else None,
            review_flags=list(candidate.review_flags) if candidate else [],
            ranking_run_id=ranking_run_id,
            review_acknowledged_by=acknowledged_by,
        ),
    )
