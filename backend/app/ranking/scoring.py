"""Final editorial scores: six dimensions (0-100) and the weighted total. Deterministic arithmetic only.

Where each dimension comes from:
- global_impact, technical_significance, student_relevance start from Stage 3.3's dimension scores (an AI
  judgement of the evidence) and are then checked against what code measured in the sources: technical
  substance, hype signals, corroboration and evidence quality can lower them; nothing raises them above what the
  evidence shows. Every change is listed in `adjustments`.
- evidence_quality and source_credibility are the verification engine's scores.
- timeliness is computed from the story's earliest known date and the edition window (Asia/Kolkata).

Rounding: dimension scores are integers; the final score is rounded half-up to one decimal. With weights that
sum to 1 and dimensions in 0-100, the final score is always within 0-100; no bonus is ever added on top.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.intelligence.contract import StoryAnalysis
from app.processing.contract import StoryCluster
from app.ranking.config import DIMENSIONS, RankingConfig
from app.ranking.contract import FinalScores, Timing, Verification
from app.ranking.window import EditionWindow


def clamp(value: float) -> int:
    return int(max(0, min(100, Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))))


def weighted_total(scores: dict[str, int | None], weights: dict[str, float]) -> float:
    """Sum of score x weight, missing dimensions counted as 0, rounded half-up to one decimal."""
    total = sum((Decimal(str(scores.get(d) or 0)) * Decimal(str(weights[d])) for d in DIMENSIONS), Decimal("0"))
    value = float(total.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return max(0.0, min(100.0, value))


@dataclass
class TimelinessResult:
    score: int | None
    timing: Timing
    flags: set[str] = field(default_factory=set)
    reason: str = ""


def event_date(cluster: StoryCluster) -> tuple[datetime | None, str]:
    """The earliest known date: a publication date, else a newsletter date. Collection time is never used as a
    publication date."""
    if cluster.earliest_published_at is not None:
        return cluster.earliest_published_at, "published"
    dates = [s.newsletter_date for s in cluster.sources if s.newsletter_date is not None]
    if dates:
        return min(dates), "newsletter"
    return None, "none"


def timeliness(cluster: StoryCluster, window: EditionWindow, config: RankingConfig) -> TimelinessResult:
    cfg = config.timeliness
    moment, basis = event_date(cluster)
    if moment is None:
        return TimelinessResult(
            None,
            Timing(date_basis="none"),
            {"publication_date_missing"},
            "no publication or newsletter date: timeliness withheld (counts as 0)",
        )
    local = window.local_date(moment)
    flags: set[str] = set()
    if basis == "newsletter":
        flags.add("publication_date_from_newsletter")
    if window.contains(moment):
        days_before_end = (window.end_date - local).days
        score = 100 - cfg.in_window_daily_decay * days_before_end
        reason = f"published {local.isoformat()}, inside the edition week"
        outside = 0
        in_window = True
    elif moment >= window.end:
        outside = (local - window.end_date).days
        score = 0
        reason = f"dated {local.isoformat()}, after the edition week (belongs to a later edition)"
        flags |= {"out_of_window", "after_window"}
        in_window = False
    else:
        outside = (window.start_date - local).days
        in_window = False
        flags.add("out_of_window")
        if outside <= cfg.late_arrival_days:
            score, reason = cfg.late_arrival_score, f"published {outside} day(s) before the edition week"
            flags.add("late_arrival")
        elif outside <= cfg.stale_days:
            score, reason = cfg.stale_score, f"published {outside} days before the edition week (older news)"
        elif outside <= cfg.very_stale_days:
            score, reason = cfg.very_stale_score, f"published {outside} days before the edition week (old news)"
        else:
            score, reason = 0, f"published {outside} days before the edition week (resurfaced old story)"
            flags.add("resurfaced")
    if basis == "newsletter" and score > cfg.newsletter_date_cap:
        score = cfg.newsletter_date_cap
        reason += f"; only a newsletter date is known (capped at {cfg.newsletter_date_cap})"
    return TimelinessResult(
        clamp(score),
        Timing(event_date=moment, date_basis=basis, in_window=in_window, days_outside_window=outside),
        flags,
        reason,
    )


def final_scores(
    analysis: StoryAnalysis,
    verification: Verification,
    timely: TimelinessResult,
    config: RankingConfig,
) -> FinalScores:
    p = analysis.priority_assessment
    v = verification
    adjustments: list[str] = []
    hype = set(v.hype_flags)
    independent = v.corroboration_level in ("multiple_independent", "official_plus_independent")

    # Global and industry impact: Stage 3.3's judgement, bounded by how well the story is evidenced.
    impact = p.global_impact
    adjustments.append(f"global impact starts at Stage 3.3's {p.global_impact}")
    if v.evidence_quality_score < 40 and impact > 60:
        impact = 60
        adjustments.append("global impact capped at 60: weak evidence")
    if hype & {"marketing_only_announcement", "unsupported_superiority_claim"} and impact > 65:
        impact = 65
        adjustments.append("global impact capped at 65: promotional or unsupported superiority claims")
    if not independent and v.source_credibility_level in ("unknown", "low") and impact > 55:
        impact = 55
        adjustments.append("global impact capped at 55: single report from an unknown or low-credibility source")

    # Technical significance: half AI judgement, half measured technical substance.
    if v.technical_substance_score is None:
        tech = min(p.technical_significance, 50)
        adjustments.append(
            f"technical significance = Stage 3.3's {p.technical_significance}, capped at 50: substance unknown"
        )
    else:
        tech = (p.technical_significance + v.technical_substance_score) / 2
        adjustments.append(
            f"technical significance = average of Stage 3.3's {p.technical_significance} and measured substance "
            f"{v.technical_substance_score}"
        )
    hype_count = len(hype - {"sensational_source_headline"})
    if hype_count >= 2 and tech > 60:
        tech = 60
        adjustments.append("technical significance capped at 60: several hype signals")
    if "unverified_benchmark_claim" in hype and tech > 75:
        tech = 75
        adjustments.append("technical significance capped at 75: benchmark results not independently reproduced")

    # Student relevance: Stage 3.3's judgement, lowered when the analysis has nothing concrete for students.
    student = p.student_relevance
    angles = len(analysis.student_relevance.angles)
    adjustments.append(f"student relevance starts at Stage 3.3's {p.student_relevance}")
    if not analysis.student_relevance.summary or angles == 0:
        student -= 15
        adjustments.append("-15 student relevance: no concrete learning, project or career angle")
    elif analysis.project_ideas and angles >= 2 and v.technical_substance_score and v.technical_substance_score >= 40:
        student += 5
        adjustments.append("+5 student relevance: concrete angles and project ideas grounded in technical substance")
    if v.technical_substance_score is not None and v.technical_substance_score < 25 and student > 70:
        student = 70
        adjustments.append("student relevance capped at 70: little technical substance to learn from")

    scores: dict[str, int | None] = {
        "global_impact": clamp(impact),
        "technical_significance": clamp(tech),
        "student_relevance": clamp(student),
        "evidence_quality": v.evidence_quality_score,
        "source_credibility": v.source_credibility_score,
        "timeliness": timely.score,
    }
    adjustments.append(f"timeliness: {timely.reason}")
    weights = config.weights.as_dict()
    unavailable = [d for d in DIMENSIONS if scores[d] is None]
    return FinalScores(
        **scores,
        final_priority_score=weighted_total(scores, weights),
        unavailable_dimensions=unavailable,
        weights=weights,
        adjustments=adjustments,
    )
