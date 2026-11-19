"""Evidence-aware verification of one story: its Stage 3.3 analysis checked against the Stage 3.2 source material,
combining the Gemini editorial evaluation (semantic judgements) with deterministic rules (facts code can check).

Evidence order: the source material (titles and excerpts collected by Stage 3.1) is the evidence. The Stage 3.3
analysis is what is being checked. Related stories found by embeddings are context for relationships and
contradictions; similarity is never evidence that a story is true. Nothing is checked outside the collected
material, so external corroboration is always "unavailable".

Deterministic here: source credibility (registry), corroboration counts, the evidence ceilings (no URL, no article
text, Stage 3.3 sufficiency), numbers in the analysis that are not in any source, claim-consistency deductions
from the per-claim verdicts, and every flag the quality gates read. There are no keyword lists: importance,
novelty, student value and hype are judged semantically by the evaluation and capped by code.
"""

from dataclasses import dataclass, field

from app.intelligence.contract import StoryAnalysis
from app.intelligence.evidence import EvidencePack, build_evidence, numbers_in
from app.processing.contract import StoryCluster
from app.ranking.config import RankingConfig, SourceRegistry
from app.ranking.contract import ClaimCheck, EditorialAssessment, SemanticRelation, Verification
from app.ranking.corroboration import assess_corroboration
from app.ranking.credibility import PRIMARY_KINDS, assess_all, cluster_credibility, valid_article_url
from app.ranking.semantic import EVALUATION_VERSION, EvaluationDraft

INDEPENDENT_LEVELS = {"multiple_independent", "official_plus_independent"}


@dataclass
class VerificationOutcome:
    verification: Verification
    # Machine-readable codes the quality gates and explanations use.
    flags: set[str] = field(default_factory=set)
    pack: EvidencePack | None = None
    evaluation: EvaluationDraft | None = None


@dataclass
class _Acc:
    """Collects deductions with their reasons, so a score is always explained."""

    score: int = 100
    reasons: list[str] = field(default_factory=list)

    def deduct(self, points: int, reason: str) -> None:
        self.score -= points
        self.reasons.append(f"-{points}: {reason}")


def _claim_checks(
    analysis: StoryAnalysis, ev: EvaluationDraft, acc: _Acc
) -> tuple[list[ClaimCheck], list[str], list[str], set[str]]:
    checks: list[ClaimCheck] = []
    unsupported: list[str] = []
    conflicting: list[str] = []
    flags: set[str] = set()
    verdicts = {c.index: c for c in ev.claims}
    for i, claim in enumerate(analysis.claims):
        verdict = verdicts.get(i)
        note = verdict.explanation[:300] if verdict else None
        if claim.basis == "inferred":
            support = "inferred"
            note = "AI inference, not a statement in the sources"
        elif not claim.source_ids:
            support = "uncited"
            acc.deduct(10, f"claim {i + 1} cites no source")
            unsupported.append(f"Claim {i + 1} cites no source: {claim.statement[:160]}")
        elif verdict is None:
            support = "not_checked"
            acc.deduct(5, f"claim {i + 1} was not checked by the evaluation")
            flags.add("claim_not_checked")
        else:
            support = verdict.verdict
        if verdict is not None and claim.basis != "inferred":
            if verdict.verdict == "contradicted":
                support = "contradicted"
                acc.deduct(30, f"claim {i + 1} contradicted by its sources")
                conflicting.append(f"Claim {i + 1} conflicts with the sources: {verdict.explanation[:200]}")
                flags.add("contradicted_claim")
            elif verdict.verdict == "not_in_sources":
                acc.deduct(15, f"claim {i + 1} is not in the sources")
                unsupported.append(f"Claim {i + 1} not found in the sources: {claim.statement[:160]}")
            elif verdict.verdict == "partially_supported":
                acc.deduct(5, f"claim {i + 1} only partly supported")
        checks.append(
            ClaimCheck(
                index=i,
                statement=claim.statement,
                basis=claim.basis,
                source_ids=list(claim.source_ids),
                support=support,
                note=note,
            )
        )
    inferred = sum(1 for c in checks if c.support == "inferred")
    if inferred >= 2 and inferred > len(checks) / 2:
        acc.deduct(10, "most claims are AI inferences rather than statements in the sources")
        flags.add("interpretation_exceeds_sources")
    return checks, unsupported, conflicting, flags


def assessment_of(ev: EvaluationDraft, model_id: str, model_version: str | None) -> EditorialAssessment:
    return EditorialAssessment(
        event_understanding=ev.event_understanding,
        core_development=ev.core_development,
        novelty_level=ev.technical_novelty.level,
        impact_areas=list(ev.industry_impact.areas),
        student_areas=list(ev.student_relevance.areas),
        explanations={
            "technical_significance": ev.technical_novelty.explanation,
            "global_impact": ev.industry_impact.explanation,
            "student_relevance": ev.student_relevance.explanation,
            "evidence_quality": ev.evidence.explanation,
        },
        hype_level=ev.hype.level,
        hype_explanation=ev.hype.explanation,
        uncertainty_flags=list(ev.uncertainty_flags),
        missing_information=list(ev.evidence.missing_information),
        model_id=model_id,
        model_version=model_version,
        prompt_version=EVALUATION_VERSION,
    )


def verify(
    cluster: StoryCluster,
    analysis: StoryAnalysis,
    evaluation: EvaluationDraft,
    config: RankingConfig,
    registry: SourceRegistry,
    *,
    relations: list[SemanticRelation] | None = None,
    extra_publishers: set[str] | None = None,
    evaluation_status: str = "completed",
    model_id: str = "",
    model_version: str | None = None,
) -> VerificationOutcome:
    caps = config.caps
    ev = evaluation
    pack = build_evidence(cluster)
    relations = relations or []
    flags: set[str] = set()
    notes: list[str] = []
    limitations: list[str] = [
        "external corroboration unavailable: no web search or outside sources are consulted",
        "embedding similarity is used to find related stories, not to establish that a claim is true",
    ]
    has_body = any(s.ref.excerpt for s in pack.sources)

    # --- Source credibility (registry, deterministic) ------------------------------------------------------
    assessments = assess_all(pack, registry, config)
    valid_ids = {s.ref_id for s in pack.sources}
    corroboration = assess_corroboration(pack, set(ev.evidence.independent_reporting_ids) & valid_ids, extra_publishers)
    cred_score, cred_level, cred_reasons = cluster_credibility(
        assessments, corroboration.independent_source_count, config
    )
    if cred_level == "unknown":
        limitations.append("source credibility is unknown (not independently evaluated); not a judgement of falsity")
        flags.add("credibility_unknown")
    elif cred_level == "provisional":
        limitations.append("source credibility is provisional (configured source, not reviewed in the registry)")
    limitations += corroboration.limitations
    primary_available = any(s.ref.source_kind in PRIMARY_KINDS for s in pack.sources)
    if ev.evidence.primary_source_available and not primary_available:
        notes.append("the evaluation saw a primary source, but none is classified as one; not counted")

    # --- Hype (semantic judgement) -------------------------------------------------------------------------
    hype = list(ev.hype.signals)
    if ev.hype.level != "none":
        notes.append(f"hype ({ev.hype.level}): {ev.hype.explanation[:200]}")
    flags.add(f"hype_level:{ev.hype.level}")

    # --- Claim consistency ---------------------------------------------------------------------------------
    acc = _Acc()
    checks, unsupported, conflicting, claim_flags = _claim_checks(analysis, ev, acc)
    flags |= claim_flags
    for part, verdict, weight in (("headline", ev.headline, (15, 20, 40)), ("summary", ev.summary, (10, 15, 30))):
        if verdict.verdict == "overstated":
            acc.deduct(weight[0], f"the {part} overstates the sources")
            flags.add(f"{part}_exaggeration")
            unsupported.append(f"The {part} overstates the sources: {verdict.explanation[:200]}")
        elif verdict.verdict == "unsupported":
            acc.deduct(weight[1], f"the {part} is not supported by the sources")
            unsupported.append(f"The {part} is not supported by the sources: {verdict.explanation[:200]}")
            flags.add(f"{part}_unsupported")
        elif verdict.verdict == "contradicted":
            acc.deduct(weight[2], f"the {part} conflicts with the sources")
            conflicting.append(f"The {part} conflicts with the sources: {verdict.explanation[:200]}")
            flags.add(f"{part}_conflict")
    for conflict in ev.source_conflicts:
        acc.deduct(15, "sources disagree")
        conflicting.append(f"Sources disagree ({', '.join(conflict.source_ids)}): {conflict.description[:200]}")
        flags.add("source_conflict")
    for rel in relations:
        if rel.conflicts_with_story and rel.relation in ("same_event", "follow_up"):
            acc.deduct(10, "a related report of the same development gives conflicting facts")
            conflicting.append(f"A related story conflicts with this one: {rel.explanation[:200]}")
            flags.add("related_conflict")
    factual = [analysis.headline, analysis.summary or "", analysis.technical_explanation or ""]
    factual.append(analysis.why_it_matters.established_facts or "")
    factual += [c.statement for c in analysis.claims if c.basis == "stated_in_sources"]
    stray = sorted(numbers_in(" ".join(factual)) - pack.allowed_numbers())
    if stray:
        acc.deduct(min(40, 20 * len(stray)), f"numbers not found in the sources: {', '.join(stray[:5])}")
        unsupported.append("Numbers not found in the sources: " + ", ".join(stray[:5]))
        flags.add("unsupported_number")
    if analysis.evidence.sufficiency == "partial":
        acc.deduct(5, "Stage 3.3 judged the evidence partial")
    missing = list(dict.fromkeys([*analysis.evidence.missing_information, *ev.evidence.missing_information]))
    if missing:
        limitations.append("missing context: " + "; ".join(missing[:4])[:300])
    consistency = max(0, min(100, acc.score))

    # --- Evidence quality: the evaluation's judgement under ceilings code can check ------------------------
    evidence = ev.evidence.score
    ev_reasons = [f"evaluation: {evidence} ({ev.evidence.technical_detail} technical detail)"]

    def cap(limit: int, why: str) -> None:
        nonlocal evidence
        if evidence > limit:
            evidence = limit
            ev_reasons.append(f"capped at {limit}: {why}")

    if not valid_article_url(cluster.representative.original_url):
        flags.add("missing_source_url")
        cap(caps.missing_url_evidence, "original article URL missing or invalid")
    if not any(s.ref.publisher or s.ref.site for s in pack.sources):
        flags.add("publisher_unknown")
    if not has_body:
        flags.add("title_only")
        cap(caps.no_article_text_evidence, "titles only, no article text")
    if analysis.evidence.sufficiency == "insufficient":
        cap(caps.stage3_insufficient_evidence, "Stage 3.3 judged the evidence insufficient")
    elif analysis.evidence.sufficiency == "partial":
        cap(caps.stage3_partial_evidence, "Stage 3.3 judged the evidence partial")
    problems = len(unsupported) + len(conflicting)
    if problems:
        penalty = min(caps.max_claim_penalty, caps.claim_problem_penalty * problems)
        evidence -= penalty
        ev_reasons.append(f"-{penalty}: {problems} unsupported or conflicting statement(s)")
    evidence = max(0, min(100, evidence))

    if ev.instruction_like_text_found:
        flags.add("instruction_like_text_in_sources")
        notes.append("the evaluation reported instruction-like text inside a source (ignored as data)")
    if analysis.analysis_status == "needs_review":
        flags.add("analysis_needs_review")
    if set(analysis.review_flags) & {"source_content_flagged", "model_reported_injection"}:
        flags.add("instruction_like_text_in_sources")
    flags |= {f"hype:{h}" for h in hype}

    verification = Verification(
        source_credibility_score=cred_score,
        source_credibility_level=cred_level,
        source_assessments=assessments,
        evidence_quality_score=evidence,
        evidence_reasons=ev_reasons,
        claim_consistency_score=consistency,
        consistency_reasons=acc.reasons or ["no consistency problems found"],
        claim_checks=checks,
        corroboration=corroboration,
        corroboration_level=corroboration.level,
        source_count=corroboration.source_count,
        independent_source_count=corroboration.independent_source_count,
        primary_source_available=primary_available,
        content_nature=ev.content_nature,
        hype_flags=hype,
        unsupported_claims=unsupported,
        conflicting_claims=conflicting,
        semantic_relations=relations,
        assessment=assessment_of(ev, model_id, model_version),
        verification_notes=cred_reasons + [corroboration.explanation] + notes,
        limitations=list(dict.fromkeys(limitations)),
        verification_status="completed",
        evaluation_status="cached" if evaluation_status == "cached" else "completed",
        checked_by=["rules", "gemini", "embeddings"],
    )
    return VerificationOutcome(verification, flags, pack, ev)
