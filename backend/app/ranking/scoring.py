"""Final editorial scores: six dimensions (0-100) and the weighted total. Deterministic arithmetic only.

Where each dimension comes from:
- global_impact, technical_significance, student_relevance are the Gemini editorial evaluation's semantic
  judgements (made with the sources and related stories), then capped by code for weak evidence, hype and
  single unknown sources; nothing raises them. Stage 3.3's preliminary scores are not used. Every change is
  listed in `adjustments`.
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
from app.ranking.semantic import EvaluationDraft
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
    evaluation: EvaluationDraft,
    timely: TimelinessResult,
    config: RankingConfig,
) -> FinalScores:
    """The six dimensions (0-100) and the weighted final score. Impact, technical significance and student
    relevance are the Gemini evaluation's semantic judgements; evidence quality and source credibility come from
    verification; timeliness from dates. Code applies the configured caps (each listed) and computes the weighted
    sum: the model cannot set the final score."""
    caps = config.caps
    v = verification
    ev = evaluation
    adjustments: list[str] = []
    independent = v.corroboration_level in ("multiple_independent", "official_plus_independent")

    impact = ev.industry_impact.score
    adjustments.append(f"global impact: evaluation {impact}")
    if v.evidence_quality_score < caps.weak_evidence_below and impact > caps.weak_evidence_impact:
        impact = caps.weak_evidence_impact
        adjustments.append(f"global impact capped at {impact}: weak evidence")
    if ev.hype.level == "significant" and impact > caps.significant_hype_impact:
        impact = caps.significant_hype_impact
        adjustments.append(f"global impact capped at {impact}: significant hype")
    if (
        not independent
        and v.source_credibility_level in ("unknown", "low")
        and impact > caps.single_unknown_source_impact
    ):
        impact = caps.single_unknown_source_impact
        adjustments.append(f"global impact capped at {impact}: single account from an unknown or low-rated source")

    tech = ev.technical_novelty.score
    adjustments.append(f"technical significance: evaluation {tech} ({ev.technical_novelty.level} novelty)")
    if ev.hype.level == "significant" and tech > caps.significant_hype_technical:
        tech = caps.significant_hype_technical
        adjustments.append(f"technical significance capped at {tech}: significant hype")
    elif ev.hype.level == "mild" and tech > caps.mild_hype_technical:
        tech = caps.mild_hype_technical
        adjustments.append(f"technical significance capped at {tech}: mild hype")
    if "unverified_benchmark_claim" in v.hype_flags and not independent and tech > caps.mild_hype_technical:
        tech = caps.mild_hype_technical
        adjustments.append(f"technical significance capped at {tech}: benchmark results not independently reported")

    student = ev.student_relevance.score
    adjustments.append(f"student relevance: evaluation {student}")

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
    explanations = dict(v.assessment.explanations)
    explanations["source_credibility"] = "; ".join(v.verification_notes[:2])[:400]
    explanations["timeliness"] = timely.reason
    return FinalScores(
        **scores,
        final_priority_score=weighted_total(scores, weights),
        unavailable_dimensions=unavailable,
        weights=weights,
        adjustments=adjustments,
        explanations=explanations,
    )
