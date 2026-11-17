"""Preliminary priority: the model scores five dimensions (0-100); code computes the weighted score, applies
evidence-based caps and assigns the level. Deterministic and explainable, so the same dimension scores always
give the same result. This is an input for Stage 3.4, never a publication decision.
"""

from dataclasses import dataclass

# Weights from the Stage 3.3 specification. They sum to 1.
WEIGHTS: dict[str, float] = {
    "global_impact": 0.25,
    "technical_significance": 0.25,
    "student_relevance": 0.20,
    "timeliness": 0.15,
    "research_practical_importance": 0.15,
}
# Lower bound (inclusive) of each level, highest first.
LEVELS: list[tuple[str, int]] = [("Critical", 85), ("High", 70), ("Medium", 45), ("Low", 0)]
# Caps applied after weighting, so thin or promotional stories cannot rank high on the model's enthusiasm alone.
CAP_INSUFFICIENT = 44  # at most Low
CAP_PARTIAL = 84  # never Critical
CAP_MARKETING = 69  # at most Medium
CAP_SINGLE_UNOFFICIAL = 84  # one non-official report: never Critical


def level_for(score: int) -> str:
    return next(name for name, floor in LEVELS if score >= floor)


@dataclass(frozen=True)
class PriorityResult:
    weighted_score: int
    overall_score: int
    priority_level: str
    adjustments: list[str]


def compute_priority(
    scores: dict[str, int], *, sufficiency: str, marketing_heavy: bool, single_unofficial_report: bool
) -> PriorityResult:
    weighted = round(sum(scores[k] * w for k, w in WEIGHTS.items()))
    overall, adjustments = weighted, []

    def cap(limit: int, reason: str) -> None:
        nonlocal overall
        if overall > limit:
            overall = limit
            adjustments.append(reason)

    if sufficiency == "insufficient":
        cap(CAP_INSUFFICIENT, f"capped at {CAP_INSUFFICIENT}: insufficient evidence")
    elif sufficiency == "partial":
        cap(CAP_PARTIAL, f"capped at {CAP_PARTIAL}: partial evidence")
    if marketing_heavy:
        cap(CAP_MARKETING, f"capped at {CAP_MARKETING}: mostly promotional claims")
    if single_unofficial_report:
        cap(CAP_SINGLE_UNOFFICIAL, f"capped at {CAP_SINGLE_UNOFFICIAL}: one non-official report only")
    return PriorityResult(weighted, overall, level_for(overall), adjustments)
