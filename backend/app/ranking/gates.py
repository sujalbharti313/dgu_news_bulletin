"""Quality gates: verification problems that a high score must never override.

Each gate is configured in `config/ranking.toml` (`[gates.*]`: enabled, action). A triggered gate sets the
story's eligibility to its action; the strictest triggered action wins (ineligible > review_required >
eligible). Gates are deterministic: they read the verification result, the evaluation's structured judgements
and dates, never keyword lists. They never delete anything. Technical failures (embedding or evaluation not
done) are not gates: those stories are `pending` and retried. The duplicate-event and previously-shortlisted
gates need every story at once and live in `duplicates.py`.
"""

from app.intelligence.contract import StoryAnalysis
from app.ranking.config import RankingConfig
from app.ranking.contract import Eligibility, FinalScores, GateResult, Verification
from app.ranking.credibility import PRIMARY_KINDS

_ORDER = {"eligible": 0, "review_required": 1, "ineligible": 2}
INDEPENDENT = {"multiple_independent", "official_plus_independent"}


def strictest(results: list[GateResult]) -> Eligibility:
    status: Eligibility = "eligible"
    for r in results:
        if _ORDER[r.action] > _ORDER[status]:
            status = r.action
    return status


def evaluate_gates(
    analysis: StoryAnalysis,
    verification: Verification,
    scores: FinalScores,
    flags: set[str],
    config: RankingConfig,
) -> list[GateResult]:
    g = config.gates
    v = verification
    out: list[GateResult] = []

    def hit(name: str, action: Eligibility, reason: str) -> None:
        out.append(GateResult(gate=name, action=action, reason=reason))

    if g.missing_source.enabled and "missing_source_url" in flags:
        hit("missing_source", g.missing_source.action, "The original article URL is missing or invalid.")

    if g.unclear_publisher.enabled and "publisher_unknown" in flags:
        hit("unclear_publisher", g.unclear_publisher.action, "No source has an identifiable publisher.")

    ie = g.insufficient_evidence
    if ie.enabled:
        if analysis.evidence.sufficiency == "insufficient":
            hit("insufficient_evidence", "ineligible", "Stage 3.3 found the source material insufficient.")
        elif v.evidence_quality_score < ie.ineligible_below:
            hit("insufficient_evidence", "ineligible", f"Evidence quality {v.evidence_quality_score} is very low.")
        elif v.evidence_quality_score < ie.min_evidence_quality:
            hit(
                "insufficient_evidence",
                ie.action,
                f"Evidence quality {v.evidence_quality_score} is below {ie.min_evidence_quality}.",
            )
        elif "title_only" in flags:
            hit("insufficient_evidence", ie.action, "Only headlines were collected; no article text to check.")

    si = g.severe_inconsistency
    severe = flags & {"contradicted_claim", "headline_conflict", "summary_conflict", "related_conflict"}
    if si.enabled and (severe or v.claim_consistency_score < si.min_claim_consistency):
        why = (
            "The headline, analysis or a related report conflicts with the sources."
            if severe
            else f"Claim consistency {v.claim_consistency_score} is below {si.min_claim_consistency}."
        )
        hit("severe_inconsistency", si.action, why)

    mc = g.unverified_major_claim
    major = (scores.global_impact or 0) >= mc.major_impact_score or bool(
        set(v.hype_flags) & {"unsupported_superiority_claim", "unverified_benchmark_claim", "speculation_as_certainty"}
    )
    # Not applied when the single account is a primary source or a reviewed established publication: their
    # self-reported claims are labelled and capped by scoring instead.
    trusted_account = v.source_credibility_level in ("primary", "established") or any(
        a.source_kind in PRIMARY_KINDS and not a.is_copy for a in v.source_assessments
    )
    if (
        mc.enabled
        and major
        and v.corroboration_level not in INDEPENDENT
        and v.evidence_quality_score < mc.min_evidence_quality
        and not trusted_account
    ):
        hit(
            "unverified_major_claim",
            mc.action,
            "A significant claim rests on one account with limited evidence; it must not be presented as confirmed.",
        )

    mis = g.misleading_claims
    overstated = flags & {"headline_exaggeration", "headline_unsupported"}
    if mis.enabled and "hype_level:significant" in flags and overstated:
        hit("misleading_claims", mis.action, "Significant hype and a headline stronger than the sources support.")

    lt = g.low_technical_substance
    if (
        lt.enabled
        and (scores.technical_significance or 0) <= lt.max_technical
        and (scores.global_impact or 0) <= lt.max_impact
    ):
        hit(
            "low_technical_substance",
            lt.action,
            f"Little substance: technical significance {scores.technical_significance} and impact "
            f"{scores.global_impact}.",
        )

    if g.out_of_window.enabled and "out_of_window" in flags and "late_arrival" not in flags:
        reason = (
            "Dated after the edition week; it belongs to a later edition."
            if "after_window" in flags
            else "Published before the edition week; older news is not presented as current."
        )
        hit("out_of_window", g.out_of_window.action, reason)

    if (
        g.unknown_single_source.enabled
        and v.source_credibility_level in ("unknown", "low")
        and v.corroboration_level not in INDEPENDENT
    ):
        level = "unknown" if v.source_credibility_level == "unknown" else "rated low in the registry"
        hit("unknown_single_source", g.unknown_single_source.action, f"Single account from a source {level}.")

    if g.analysis_needs_review.enabled and "analysis_needs_review" in flags:
        hit("analysis_needs_review", g.analysis_needs_review.action, "Stage 3.3 flagged this story for review.")
    if g.instruction_like_text.enabled and "instruction_like_text_in_sources" in flags:
        hit(
            "instruction_like_text",
            g.instruction_like_text.action,
            "A source contains text addressed to an AI; it was ignored, but an admin should look first.",
        )
    return out
