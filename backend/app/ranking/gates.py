"""Quality gates: verification problems that a high score must never override.

Each gate is configured in `config/ranking.toml` (`[gates.*]`: enabled, action). A triggered gate sets the
story's eligibility to its action; the strictest triggered action wins (ineligible > review_required >
eligible). Gates never delete anything: ineligible and review_required stories stay in the results for an admin.
The duplicate-event gate needs every story at once and lives in `duplicates.py`.
"""

from app.intelligence.contract import StoryAnalysis
from app.ranking.config import RankingConfig
from app.ranking.contract import Eligibility, GateResult, Verification
from app.ranking.credibility import PRIMARY_KINDS

_ORDER = {"eligible": 0, "review_required": 1, "ineligible": 2}
INDEPENDENT = {"multiple_independent", "official_plus_independent"}
MAJOR_HYPE = {"unsupported_superiority_claim", "unverified_benchmark_claim", "speculation_as_certainty"}


def strictest(results: list[GateResult]) -> Eligibility:
    status: Eligibility = "eligible"
    for r in results:
        if _ORDER[r.action] > _ORDER[status]:
            status = r.action
    return status


def evaluate_gates(
    analysis: StoryAnalysis,
    verification: Verification,
    flags: set[str],
    config: RankingConfig,
) -> list[GateResult]:
    g = config.gates
    v = verification
    out: list[GateResult] = []

    def hit(name: str, action: Eligibility, reason: str) -> None:
        out.append(GateResult(gate=name, action=action, reason=reason))

    # Gate 1: the original article must be identifiable.
    if g.missing_source.enabled and "missing_source_url" in flags:
        hit("missing_source", g.missing_source.action, "The original article URL is missing or invalid.")

    # Gate 2: claims must be reasonably supported by the source content.
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

    # Gate 3: the headline or AI analysis must not materially conflict with the sources.
    si = g.severe_inconsistency
    severe = flags & {"contradicted_claim", "headline_conflict", "summary_conflict"}
    if si.enabled and (severe or v.claim_consistency_score < si.min_claim_consistency):
        why = (
            "The headline or analysis conflicts with the sources."
            if severe
            else f"Claim consistency {v.claim_consistency_score} is below {si.min_claim_consistency}."
        )
        hit("severe_inconsistency", si.action, why)

    # Gate 5: inside the Monday-Sunday collection week.
    if g.out_of_window.enabled and "out_of_window" in flags and "late_arrival" not in flags:
        reason = (
            "Dated after the edition week; it belongs to a later edition."
            if "after_window" in flags
            else "Published before the edition week; older news is not presented as current."
        )
        hit("out_of_window", g.out_of_window.action, reason)

    # Gate 6: significant claims need adequate evidence.
    mc = g.unverified_major_claim
    major = analysis.priority_assessment.global_impact >= mc.major_impact_score or bool(set(v.hype_flags) & MAJOR_HYPE)
    # Not applied when the single account is a primary source (the organisation's own channel, a government or a
    # research repository) or a registry-reviewed publication: their self-reported claims are labelled and capped
    # by scoring instead (for example `unverified_benchmark_claim`).
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

    # Unknown source with no corroboration: not assumed false, but not shortlisted automatically either.
    if (
        g.unknown_single_source.enabled
        and v.source_credibility_level in ("unknown", "low")
        and v.corroboration_level not in INDEPENDENT
    ):
        level = "unknown" if v.source_credibility_level == "unknown" else "rated low in the registry"
        hit("unknown_single_source", g.unknown_single_source.action, f"Single account from a source {level}.")

    # Stage 3.3 asked for a human look (flagged source content, instruction-like text).
    if g.analysis_needs_review.enabled and flags & {"analysis_needs_review", "source_content_flagged"}:
        hit("analysis_needs_review", g.analysis_needs_review.action, "Stage 3.3 flagged this story for review.")
    if "instruction_like_text_in_sources" in flags:
        hit("analysis_needs_review", "review_required", "A source contains instruction-like text.")

    # Verification not finished: never eligible automatically.
    if v.verification_status == "pending_ai_check":
        hit("verification_incomplete", "review_required", "The AI consistency check is still pending (quota).")
    elif v.verification_status == "ai_check_failed":
        hit("verification_incomplete", "review_required", "The AI consistency check failed; it is retried later.")
    return out
