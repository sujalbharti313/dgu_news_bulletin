"""The verification engine: one Stage 3.3 analysis checked against the Stage 3.2 source material it came from.

Evidence order: the source material (titles and excerpts collected by Stage 3.1) is the evidence. The Stage 3.3
analysis is what is being checked, never evidence of its own. An optional Gemini consistency check (see
`ai_check.py`) can only add problems or clear a purely lexical doubt about a claim; it never raises evidence or
credibility scores.

Every score comes with the reasons that produced it. When a criterion cannot be assessed (no article text,
for example), it is reported as unknown and listed under `limitations` instead of being given a number.
"""

import re
from dataclasses import dataclass, field

from app.intelligence.contract import StoryAnalysis
from app.intelligence.evidence import EvidencePack, build_evidence, numbers_in
from app.processing.contract import StoryCluster
from app.ranking import text as t
from app.ranking.ai_check import AiCheckDraft
from app.ranking.config import RankingConfig, SourceRegistry
from app.ranking.contract import ClaimCheck, CorroborationResult, SourceAssessment, Verification
from app.ranking.corroboration import assess_corroboration
from app.ranking.credibility import PRIMARY_KINDS, assess_all, cluster_credibility, valid_article_url

_VERIFIED = re.compile(r"(?i)\b(is|are|was|were|been|be|now)\s+(officially |independently )?(confirmed|verified)\b")
_HEDGE = re.compile(r"(?i)\b(may|might|could|suggests?|likely|possibly|potentially|appears?|seems?|if)\b")
_NATURE_RANK = {
    "promotional": 0,
    "speculation": 1,
    "opinion": 2,
    "official_announcement": 3,
    "factual_report": 4,
    "research_publication": 5,
}
INDEPENDENT_LEVELS = {"multiple_independent", "official_plus_independent"}


@dataclass
class VerificationOutcome:
    verification: Verification
    # Machine-readable codes the quality gates and explanations use.
    flags: set[str] = field(default_factory=set)
    pack: EvidencePack | None = None


@dataclass
class _Acc:
    """Collects deductions with their reasons, so a score is always explained."""

    score: int = 100
    reasons: list[str] = field(default_factory=list)

    def deduct(self, points: int, reason: str) -> None:
        self.score -= points
        self.reasons.append(f"-{points}: {reason}")


def _report_texts(pack: EvidencePack) -> list[str]:
    return [f"{s.ref.title} {s.ref.excerpt or ''}" for s in pack.sources if s.report_group == s.ref_id]


def _body_texts(pack: EvidencePack) -> list[str]:
    bodies = [s.ref.excerpt for s in pack.sources if s.ref.excerpt]
    return bodies or [s.ref.title for s in pack.sources]


def _all_texts(pack: EvidencePack) -> list[str]:
    return [x for s in pack.sources for x in (s.ref.title, s.ref.excerpt or "") if x]


def content_nature(pack: EvidencePack, joined: str, signals: dict) -> str:
    reports = [s for s in pack.sources if s.report_group == s.ref_id]
    titles = " ".join(s.ref.title for s in reports)
    research = any(s.ref.source_kind == "research_repository" for s in reports) or any(
        i.startswith(("arxiv:", "doi:")) for s in reports for i in s.ref.identifiers
    )
    if research or re.search(r"(?i)\bwe (present|propose|introduce)\b", joined):
        return "research_publication"
    hype = len(t.marketing_terms(joined)) + len(t.promotional_phrases(joined))
    if hype >= 3 and len(signals) <= 1:
        return "promotional"
    if t.opinion_markers(titles) or len(t.opinion_markers(joined)) >= 2:
        return "opinion"
    rumours = [
        m for m in t.hedged_speculation(joined) if m.startswith(("rumo", "reported", "alleged", "leak", "sources"))
    ]
    if rumours or (len(t.hedged_speculation(joined)) >= 4 and len(t.words(joined)) < 150):
        return "speculation"
    if any(s.ref.source_kind in ("official_channel", "government") for s in reports):
        return "official_announcement"
    return "factual_report"


def _claim_checks(
    analysis: StoryAnalysis, pack: EvidencePack, config: RankingConfig, ai: AiCheckDraft | None, acc: _Acc
) -> tuple[list[ClaimCheck], list[str], list[str], set[str]]:
    threshold = config.verification.claim_support_threshold
    checks: list[ClaimCheck] = []
    unsupported: list[str] = []
    conflicting: list[str] = []
    flags: set[str] = set()
    ai_claims = {c.index: c for c in (ai.claims if ai else [])}
    weak_points = 0
    cleared = 0
    for i, claim in enumerate(analysis.claims):
        cited = [pack.by_id(r) for r in claim.source_ids]
        cited_texts = [f"{c.ref.title} {c.ref.excerpt or ''}" for c in cited if c is not None]
        cov = round(t.coverage(claim.statement, cited_texts), 2) if cited_texts else 0.0
        verdict = ai_claims.get(i)
        note = None
        if claim.basis == "inferred":
            support = "inferred"
            note = "AI inference, not a statement in the sources"
        elif not claim.source_ids:
            support = "uncited"
            acc.deduct(10, f"claim {i + 1} cites no source")
            unsupported.append(f"Claim {i + 1} cites no source: {claim.statement[:160]}")
        elif cov < threshold:
            support = "weakly_supported"
            note = f"only {cov:.0%} of its words appear in the cited sources"
        else:
            support = "supported"
        if verdict is not None:
            if verdict.verdict == "contradicted":
                support = "contradicted"
                acc.deduct(30, f"claim {i + 1} contradicted by its sources (AI check)")
                conflicting.append(f"Claim {i + 1} conflicts with the sources: {verdict.explanation[:200]}")
                flags.add("contradicted_claim")
            elif verdict.verdict == "not_in_sources" and support != "inferred":
                support = "not_in_sources"
                acc.deduct(15, f"claim {i + 1} not found in the sources (AI check)")
                unsupported.append(f"Claim {i + 1} not found in the sources: {claim.statement[:160]}")
            elif verdict.verdict in ("supported", "partially_supported") and support == "weakly_supported":
                # Clears the lexical doubt for consistency only; evidence quality still counts it (see verify).
                support = "supported"
                note = f"AI check: {verdict.verdict.replace('_', ' ')} (lexical overlap {cov:.0%})"
                flags.add("ai_cleared_lexical_doubt")
                cleared += 1
        if support == "weakly_supported":
            weak_points += 8
            unsupported.append(f"Claim {i + 1} weakly supported by its sources: {claim.statement[:160]}")
        checks.append(
            ClaimCheck(
                index=i,
                statement=claim.statement,
                basis=claim.basis,
                source_ids=list(claim.source_ids),
                support=support,
                lexical_coverage=min(1.0, max(0.0, cov)),
                ai_verdict=verdict.verdict if verdict else None,
                note=note if note is None else note[:300],
            )
        )
    if weak_points:
        acc.deduct(min(weak_points, 32), "claims with little footing in their cited sources")
    inferred = sum(1 for c in checks if c.support == "inferred")
    if inferred >= 2 and inferred > len(checks) / 2:
        acc.deduct(10, "most claims are AI inferences rather than statements in the sources")
        flags.add("interpretation_exceeds_sources")
    if cleared:
        flags.add(f"ai_cleared:{cleared}")
    return checks, unsupported, conflicting, flags


def _quantity_conflicts(pack: EvidencePack) -> list[str]:
    """Same quantity (unit + the word after it) given different values by different independent reports."""
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for s in pack.sources:
        if s.report_group != s.ref_id:
            continue
        for number, unit, following in t.quantities(f"{s.ref.title}. {s.ref.excerpt or ''}"):
            if not following:
                continue
            seen.setdefault((unit, following), {}).setdefault(s.publisher_key, number)
    out = []
    for (unit, following), by_publisher in seen.items():
        values = sorted(set(by_publisher.values()))
        if len(by_publisher) >= 2 and len(values) >= 2:
            out.append(f"Sources give different values for '{unit} {following}': {', '.join(values[:4])}")
    return out


def verify(
    cluster: StoryCluster,
    analysis: StoryAnalysis,
    config: RankingConfig,
    registry: SourceRegistry,
    *,
    ai: AiCheckDraft | None = None,
    ai_status: str = "disabled",
) -> VerificationOutcome:
    v = config.verification
    pack = build_evidence(cluster)
    flags: set[str] = set()
    notes: list[str] = []
    limitations: list[str] = []
    report_texts = _report_texts(pack)
    joined = " ".join(report_texts)
    bodies = _body_texts(pack)
    has_body = any(s.ref.excerpt for s in pack.sources)
    distinct_words = len(t.content_words(joined))

    # --- A. Source credibility -------------------------------------------------------------------------
    assessments: list[SourceAssessment] = assess_all(pack, registry, config)
    corroboration: CorroborationResult = assess_corroboration(pack, config)
    cred_score, cred_level, cred_reasons = cluster_credibility(
        assessments, corroboration.independent_source_count, config
    )
    if cred_level == "unknown":
        limitations.append("source credibility is unknown (not independently evaluated)")
        flags.add("credibility_unknown")
    elif cred_level == "provisional":
        limitations.append("source credibility is provisional (configured source, not reviewed in the registry)")
    limitations += corroboration.limitations

    # --- Technical substance and hype -----------------------------------------------------------------
    signals = t.technical_signals(joined)
    nature = content_nature(pack, joined, signals)
    if ai is not None and _NATURE_RANK[ai.content_nature] < _NATURE_RANK[nature]:
        notes.append(f"AI check judged the sources as {ai.content_nature.replace('_', ' ')} (rules: {nature})")
        nature = ai.content_nature  # only ever more cautious
    hype: list[str] = []
    marketing = t.marketing_terms(joined)
    promo = t.promotional_phrases(joined)
    if marketing:
        hype.append("marketing_language")
        notes.append("marketing wording in the sources: " + ", ".join(marketing[:5]))
    if promo:
        hype.append("promotional_language")
    if t.vague_ai_terms(joined) and len(signals) <= 1:
        hype.append("vague_ai_claim")
    superiority = t.superiority_terms(joined) + t.superiority_terms(analysis.headline)
    if superiority and not t.benchmark_terms(joined):
        hype.append("unsupported_superiority_claim")
    benchmark_numbers = bool(t.benchmark_terms(joined)) and any(u == "%" for _, u, _ in t.quantities(joined))
    if benchmark_numbers and corroboration.level not in INDEPENDENT_LEVELS:
        hype.append("unverified_benchmark_claim")
        notes.append("benchmark figures come from a single account and are not independently reproduced here")
    if t.certainty_speculation(joined) or t.certainty_speculation(analysis.headline):
        hype.append("speculation_as_certainty")
    for signal in ai.hype_signals if ai else []:
        if signal not in hype:
            hype.append(signal)

    substance: int | None
    substance_names = sorted(signals)
    if distinct_words < v.min_words_for_substance:
        substance = None
        limitations.append("too little source text to judge technical substance (unknown)")
    else:
        points = 12 * len(signals)
        ids = [i for s in pack.sources for i in s.ref.identifiers]
        if ids:
            points += 10
            substance_names.append("paper_identifier")
        if t.evidence_links(joined):
            points += 8
            substance_names.append("links_to_supporting_material")
        grounded = 0
        for d in analysis.technical_details:
            cited = [pack.by_id(r) for r in d.source_ids]
            texts = [f"{c.ref.title} {c.ref.excerpt or ''}" for c in cited if c is not None]
            if texts and t.coverage(d.detail, texts) >= 0.5:
                grounded += 1
        points += min(12, 4 * grounded)
        if analysis.technical_explanation_status == "available":
            points += 8
        penalty = min(30, 6 * len(hype))
        substance = max(0, min(100, points - penalty))
    if analysis.priority_assessment.marketing_heavy and (substance is None or substance < 40):
        if "marketing_only_announcement" not in hype:
            hype.append("marketing_only_announcement")

    # --- C. Claim consistency ------------------------------------------------------------------------
    acc = _Acc()
    checks, unsupported, conflicting, claim_flags = _claim_checks(analysis, pack, config, ai, acc)
    flags |= claim_flags
    body_pool = " ".join(bodies)
    added = [w for w in t.absolute_terms(analysis.headline) if w not in t.absolute_terms(body_pool)]
    if added:
        acc.deduct(15, f"headline adds wording the articles do not support: {', '.join(added[:3])}")
        unsupported.append("Headline wording not supported by the article text: " + ", ".join(added[:3]))
        flags.add("headline_exaggeration")
    for s in pack.sources:
        if s.report_group != s.ref_id or not s.ref.excerpt:
            continue
        loud = [w for w in t.absolute_terms(s.ref.title) if w not in t.absolute_terms(s.ref.excerpt)]
        if loud and t.limiting_terms(s.ref.excerpt):
            acc.deduct(5, f"source headline {s.ref_id} overstates its own article ({', '.join(loud[:3])})")
            notes.append(f"{s.ref_id}'s headline is stronger than its article text; the article adds limitations")
            flags.add("source_headline_exaggeration")
            if "sensational_source_headline" not in hype:
                hype.append("sensational_source_headline")
    head_cov = t.coverage(analysis.headline, _all_texts(pack))
    if head_cov < v.headline_support_threshold:
        acc.deduct(15, f"only {head_cov:.0%} of the headline's words appear in the sources")
        flags.add("headline_weakly_supported")
    if analysis.summary and t.coverage(analysis.summary, _all_texts(pack)) < v.headline_support_threshold:
        acc.deduct(10, "the summary has little footing in the source text")
        flags.add("summary_weakly_supported")
    factual = [analysis.headline, analysis.summary or "", analysis.technical_explanation or ""]
    factual.append(analysis.why_it_matters.established_facts or "")
    factual += [c.statement for c in analysis.claims if c.basis == "stated_in_sources"]
    stray = sorted(numbers_in(" ".join(factual)) - pack.allowed_numbers())
    if stray:
        acc.deduct(min(40, 20 * len(stray)), f"numbers not found in the sources: {', '.join(stray[:5])}")
        unsupported.append("Numbers not found in the sources: " + ", ".join(stray[:5]))
        flags.add("unsupported_number")
    if any(_VERIFIED.search(x) for x in factual if x):
        acc.deduct(5, "the analysis calls something confirmed or verified")
    interpretation = analysis.why_it_matters.interpretation
    if interpretation and not _HEDGE.search(interpretation):
        acc.deduct(5, "the AI interpretation is not hedged")
        notes.append("why-it-matters interpretation reads as fact; it is AI interpretation")
    if analysis.evidence.sufficiency == "partial":
        acc.deduct(5, "Stage 3.3 judged the evidence partial")
    if analysis.evidence.missing_information:
        limitations.append("missing context: " + "; ".join(analysis.evidence.missing_information[:3])[:300])
    source_conflicts = _quantity_conflicts(pack)
    if source_conflicts:
        acc.deduct(min(30, 15 * len(source_conflicts)), "sources disagree on a figure")
        conflicting += source_conflicts
        flags.add("source_conflict")
    if ai is not None:
        for part, verdict, weight in (
            ("headline", ai.headline, (15, 20, 40)),
            ("summary", ai.summary, (10, 15, 30)),
        ):
            if verdict.verdict == "overstated":
                acc.deduct(weight[0], f"AI check: the {part} overstates the sources")
                flags.add(f"{part}_exaggeration")
                unsupported.append(f"The {part} overstates the sources: {verdict.explanation[:200]}")
            elif verdict.verdict == "unsupported":
                acc.deduct(weight[1], f"AI check: the {part} is not supported by the sources")
                unsupported.append(f"The {part} is not supported by the sources: {verdict.explanation[:200]}")
                flags.add(f"{part}_unsupported")
            elif verdict.verdict == "contradicted":
                acc.deduct(weight[2], f"AI check: the {part} conflicts with the sources")
                conflicting.append(f"The {part} conflicts with the sources: {verdict.explanation[:200]}")
                flags.add(f"{part}_conflict")
        for conflict in ai.source_conflicts:
            acc.deduct(15, "AI check: sources disagree")
            conflicting.append(f"Sources disagree ({', '.join(conflict.source_ids)}): {conflict.description[:200]}")
            flags.add("source_conflict")
        if ai.instruction_like_text_found:
            flags.add("instruction_like_text_in_sources")
            notes.append("AI check reported instruction-like text inside a source (ignored as data)")

    # --- B. Evidence quality ---------------------------------------------------------------------------
    ev = 0
    ev_reasons: list[str] = []
    rep_url_ok = valid_article_url(cluster.representative.original_url)
    if rep_url_ok:
        ev += 15
        ev_reasons.append("+15 valid original article URL")
    else:
        flags.add("missing_source_url")
        ev_reasons.append("+0 original article URL missing or invalid")
    if any(s.ref.publisher or s.ref.site for s in pack.sources):
        ev += 10
        ev_reasons.append("+10 publisher identified")
    else:
        flags.add("publisher_unknown")
    body_words = len(t.content_words(" ".join(s.ref.excerpt or "" for s in pack.sources)))
    content_points = (
        0 if not has_body else 5 if body_words < 15 else 12 if body_words < 35 else 18 if body_words < 70 else 25
    )
    ev += content_points
    ev_reasons.append(
        f"+{content_points} article text ({body_words} distinct words)" if has_body else "+0 no article text"
    )
    if not has_body:
        flags.add("title_only")
    specific = 0
    if numbers_in(joined):
        specific += 5
    if cluster.entities.organisations or cluster.entities.products or cluster.entities.benchmarks:
        specific += 5
    if t.quantities(joined):
        specific += 5
    ev += specific
    ev_reasons.append(f"+{specific} specific, checkable details")
    support = 0
    if any(s.ref.identifiers for s in pack.sources):
        support += 10
    if t.evidence_links(joined):
        support += 5
    if any(s.ref.source_kind in PRIMARY_KINDS for s in pack.sources):
        support += 5
    support = min(15, support)
    ev += support
    ev_reasons.append(f"+{support} links to papers, code, data or a primary source")
    nature_points = {"research_publication": 20, "factual_report": 20, "official_announcement": 15}.get(nature, 5)
    if nature == "promotional":
        nature_points = 0
    ev += nature_points
    ev_reasons.append(f"+{nature_points} content is {nature.replace('_', ' ')}")
    # The AI check may clear a lexical doubt for claim consistency, but never improves evidence quality.
    doubts = len(unsupported) + sum(int(f.split(":")[1]) for f in flags if f.startswith("ai_cleared:"))
    if doubts:
        penalty = min(24, 8 * doubts)
        ev -= penalty
        ev_reasons.append(f"-{penalty} unsupported claims")
    if not has_body and ev > 30:
        ev = 30
        ev_reasons.append("capped at 30: titles only, no article text")
    if analysis.evidence.sufficiency == "insufficient" and ev > 30:
        ev = 30
        ev_reasons.append("capped at 30: Stage 3.3 judged the evidence insufficient")
    elif analysis.evidence.sufficiency == "partial" and ev > 70:
        ev = 70
        ev_reasons.append("capped at 70: Stage 3.3 judged the evidence partial")
    evidence_score = max(0, min(100, ev))

    p = analysis.priority_assessment
    if p.overall_score >= v.preliminary_overreach_score and evidence_score < v.preliminary_overreach_evidence:
        acc.deduct(10, "Stage 3.3's preliminary priority is higher than the evidence supports")
        flags.add("preliminary_priority_exceeds_evidence")
    consistency = max(0, min(100, acc.score))

    if ai_status in ("pending", "failed", "invalid_output"):
        status = "pending_ai_check" if ai_status == "pending" else "ai_check_failed"
    else:
        status = "completed"
    checked_by = ["rules", "gemini"] if ai is not None else ["rules"]
    if ai is None:
        limitations.append(
            "claim consistency checked by rules only"
            + {"disabled": " (AI check disabled)", "not_selected": " (below the AI check threshold)"}.get(ai_status, "")
        )
    if analysis.analysis_status == "needs_review":
        flags.add("analysis_needs_review")
    if set(analysis.review_flags) & {"source_content_flagged", "model_reported_injection"}:
        flags.add("source_content_flagged")
    flags |= {f"hype:{h}" for h in hype}

    verification = Verification(
        source_credibility_score=cred_score,
        source_credibility_level=cred_level,
        source_assessments=assessments,
        evidence_quality_score=evidence_score,
        evidence_reasons=ev_reasons,
        claim_consistency_score=consistency,
        consistency_reasons=acc.reasons or ["no consistency problems found"],
        claim_checks=checks,
        corroboration=corroboration,
        corroboration_level=corroboration.level,
        source_count=corroboration.source_count,
        independent_source_count=corroboration.independent_source_count,
        technical_substance_score=substance,
        technical_signals=substance_names,
        content_nature=nature,
        hype_flags=hype,
        unsupported_claims=unsupported,
        conflicting_claims=conflicting,
        verification_notes=cred_reasons + [corroboration.explanation] + notes,
        limitations=list(dict.fromkeys(limitations)),
        verification_status=status,
        ai_check_status=ai_status,
        checked_by=checked_by,
    )
    return VerificationOutcome(verification, flags, pack)
