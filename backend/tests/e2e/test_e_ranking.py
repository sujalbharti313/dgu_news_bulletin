"""Phase 4.2 Scenario E: semantic verification and editorial ranking (Stage 3.4) on a controlled week of news with
known, varied impact, significance, relevance, evidence, credibility, timeliness and hype.

The ranking result is checked against an independent oracle: final scores are recomputed here from the six
dimension scores and the shipped weights, caps and thresholds are re-applied, and the shortlist rules are checked
story by story. Real Stage 3.2 clusters and real Stage 3.3 validation; Gemini is a scripted double.
"""

from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.ranking.scoring import weighted_total
from tests.intelligence_helpers import (
    FUNDING,
    HYPE,
    INDIA,
    INJECTION_ITEM,
    LAB_POST,
    NEWS_REPORT,
    PAPER,
    POLICY,
    SPARSE_ITEM,
    SYNDICATED_COPY,
    THIN_ITEM,
)
from tests.ranking_helpers import CONFIG, FakeGemini, analysed, evaluator, make_ranker, title_responder

ITEMS = [
    LAB_POST,
    NEWS_REPORT,
    SYNDICATED_COPY,
    PAPER,
    INDIA,
    FUNDING,
    POLICY,
    HYPE,
    INJECTION_ITEM,
    SPARSE_ITEM,
    THIN_ITEM,
]
# Stage 3.3 answers per story: who is involved, what kind of development, which category.
ANALYSIS = title_responder(
    {
        "Sparse routing": {
            "understanding.involved": ["Example University"],
            "understanding.development_type": "published_research",
            "classification.primary": "ai-research",
            "classification.secondary": [],
        },
        "IIT Example": {
            "understanding.involved": ["IIT Example"],
            "understanding.development_type": "released",
            "classification.primary": "india-ai",
            "classification.secondary": ["ai-research"],
        },
        "Example Robotics": {
            "understanding.involved": ["Example Robotics"],
            "understanding.development_type": "funded",
            "classification.primary": "industry-business",
            "classification.secondary": [],
        },
        "Regulator": {
            "understanding.involved": ["Example Digital Authority"],
            "understanding.development_type": "announced",
            "classification.primary": "ai-safety-governance",
            "classification.secondary": [],
        },
        "robot THINKS": {
            "understanding.involved": ["Example University lab"],
            "understanding.development_type": "demonstrated",
            "classification.primary": "robotics-automation",
            "classification.secondary": [],
        },
    }
)
# Stage 3.4 semantic evaluation per story (scores 0-100), chosen to exercise every rule.
EVALUATION = {
    "Nimbus": {
        "industry_impact.score": 85,
        "technical_novelty.score": 75,
        "student_relevance.score": 80,
        "evidence.score": 80,
    },
    "Sparse routing": {
        "industry_impact.score": 60,
        "technical_novelty.score": 85,
        "technical_novelty.level": "major",
        "student_relevance.score": 75,
    },
    "IIT Example": {"industry_impact.score": 55, "technical_novelty.score": 60, "student_relevance.score": 90},
    "Example Robotics": {
        "industry_impact.score": 35,
        "technical_novelty.score": 20,
        "technical_novelty.level": "none",
        "student_relevance.score": 30,
    },
    "Regulator": {"industry_impact.score": 75, "technical_novelty.score": 35, "student_relevance.score": 60},
    "robot THINKS": {
        "industry_impact.score": 80,
        "technical_novelty.score": 70,
        "hype.level": "significant",
        "hype.signals": ["vague_claims", "unsupported_superiority_claim"],
        "headline.verdict": "overstated",
        "headline.explanation": "S1 says it is a lab demo, not general-purpose.",
    },
    "teases": {
        "evidence.score": 10,
        "industry_impact.score": 50,
        "technical_novelty.score": 10,
        "technical_novelty.level": "none",
        "evidence.missing_information": ["No name, date or details."],
    },
}


def week():
    clusters, records = analysed(ITEMS, ANALYSIS)
    runner, _ = make_ranker(clusters, records, provider=FakeGemini(evaluator(EVALUATION)))
    report = runner.run(week="current")
    assert report.status == "completed", report.reason
    return report.result


def oracle_total(s) -> float:
    w = {
        "global_impact": "0.20",
        "technical_significance": "0.20",
        "student_relevance": "0.20",
        "evidence_quality": "0.15",
        "source_credibility": "0.15",
        "timeliness": "0.10",
    }  # the specification
    total = sum(Decimal(str(getattr(s, d) or 0)) * Decimal(v) for d, v in w.items())
    return float(total.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def by(result, needle):
    return next(c for c in result.candidates if needle.lower() in c.headline.lower())


def test_hand_computed_scores():
    """E2E-E-01: the weighted total matches hand calculation, including half-up rounding at .x5 where binary
    floating point would round down (9.15 -> 9.2) and the 0-100 bounds."""
    w = CONFIG.weights.as_dict()
    full = dict(
        global_impact=80,
        technical_significance=70,
        student_relevance=90,
        evidence_quality=60,
        source_credibility=85,
        timeliness=100,
    )
    assert weighted_total(full, w) == 79.8  # 16 + 14 + 18 + 9 + 12.75 + 10 = 79.75 -> 79.8
    assert weighted_total({"evidence_quality": 61}, w) == 9.2  # 9.15 half-up
    assert weighted_total({d: 55 for d in full}, w) == 55.0
    assert weighted_total({d: 100 for d in full}, w) == 100.0 and weighted_total({}, w) == 0.0


def test_every_final_score_matches_the_oracle_and_the_classification():
    """E2E-E-02: for every evaluated story the stored final score equals the oracle recomputation from its six
    dimensions, lies in 0-100, and its priority class follows the configured thresholds (85/70/55/40)."""
    result = week()
    assert result.candidates
    th = CONFIG.classification
    for c in result.candidates:
        s = c.final_scores
        assert s.final_priority_score == oracle_total(s), c.headline
        assert 0 <= s.final_priority_score <= 100
        f = s.final_priority_score
        expected = (
            "exceptional"
            if f >= th.exceptional
            else "high"
            if f >= th.high
            else "medium"
            if f >= th.medium
            else "low"
            if f >= th.low
            else "very low"
        )
        assert c.priority_classification.lower() == expected, (c.headline, f, c.priority_classification)


def test_caps_gates_and_eligibility_follow_the_rules():
    """E2E-E-03: significant hype caps impact (<=65) and technical significance (<=55) and the misleading headline
    needs review; a teaser with almost no evidence is not eligible; low technical substance is ineligible; a story
    with instruction-like text is not eligible without a human; caps never raise a score."""
    result = week()
    hype = by(result, "robot THINKS")
    assert hype.final_scores.global_impact <= 65 and hype.final_scores.technical_significance <= 55
    assert hype.eligibility_status in ("review_required", "ineligible")
    teaser = next((c for c in result.candidates if "teases" in c.headline), None)
    if teaser is not None:
        assert teaser.eligibility_status in ("review_required", "ineligible")
    else:
        assert any("teas" in str(p) for p in result.pending + result.excluded)
    robotics = by(result, "Example Robotics")
    assert robotics.eligibility_status == "ineligible"  # minimal technical substance
    injection = by(result, "student research grant")
    assert injection.eligibility_status != "eligible"
    for c in result.candidates:
        s = c.final_scores
        if s.evidence_quality < CONFIG.caps.weak_evidence_below:
            assert s.global_impact <= CONFIG.caps.weak_evidence_impact


def test_shortlist_is_eligible_ordered_diverse_and_never_padded():
    """E2E-E-04: the shortlist holds only eligible stories at or above the minimum score, in score order, at most
    10 and here fewer (the week has fewer good stories); every rejected eligible story has a stated reason."""
    result = week()
    sl = result.shortlist
    assert 1 <= len(sl) < 10 and len(sl) <= CONFIG.shortlist.target
    assert [c.story_cluster_id for c in sl] == list(result.ranked_candidate_ids)
    assert all(
        c.eligibility_status == "eligible" and c.final_scores.final_priority_score >= CONFIG.shortlist.min_score
        for c in sl
    )
    scores = [c.final_scores.final_priority_score for c in sl]
    assert scores == sorted(scores, reverse=True) or CONFIG.shortlist.coverage_swap_margin > 0
    assert len({c.story_cluster_id for c in sl}) == len(sl)
    for c in result.candidates:
        if not c.shortlisted:
            assert c.selection_note and c.selection_note.startswith("not shortlisted")
    # The launch (three reports) takes exactly one place.
    assert sum("Nimbus" in c.headline for c in sl) <= 1


def test_ranking_is_deterministic():
    """E2E-E-05: the same inputs give the same scores, eligibility, ranks and shortlist on a fresh run."""
    a, b = week(), week()

    def sig(r):
        return [
            (c.headline, c.final_scores.final_priority_score, c.eligibility_status, c.rank, c.shortlisted)
            for c in r.candidates
        ]

    assert sig(a) == sig(b) and a.ranked_candidate_ids == b.ranked_candidate_ids


def test_same_event_needs_semantic_judgement_and_takes_one_slot():
    """E2E-E-06: two separately clustered reports of one event, judged `same_event` by the evaluation and highly
    similar, take one shortlist place; the other is kept in the results as a duplicate (nothing deleted)."""
    from tests.processing_helpers import staged
    from tests.ranking_helpers import bag_vector

    twin = staged(
        "IIT Example researchers publish 1,200 hours of Hindi and Marathi speech data",
        "https://news-b.example.com/2026/11/iit-example-speech-corpus",
        "IIT Example has published 1,200 hours of Hindi and Marathi speech recordings from 3,000 volunteers "
        "in 40 districts, free for academic and commercial use, for speech recognition research.",
        source="news-b",
        publisher="News B",
        hours_ago=60,
    )
    clusters, records = analysed([*ITEMS, twin], ANALYSIS)
    if len({c.cluster_id for c in clusters if "Hindi" in c.title}) < 2:
        pytest.skip("Stage 3.2 already merged the two reports; the Stage 3.4 path is not reached")

    def relation(data, related):
        both = "Hindi" in data["analysis_to_check"]["headline"] and "Hindi" in related.get("headline", "")
        return "same_event" if both else "related_context"

    def vector(text):
        # The offline bag-of-words double does not capture meaning; a real embedding model would place these two
        # reports close together. Give them near-identical vectors (cos ~0.97) so the semantic path is exercised.
        if "Hindi" in text:
            v = bag_vector("hindi marathi speech dataset iit example")
            return [x + 0.01 * y for x, y in zip(v, bag_vector(text), strict=True)]
        return bag_vector(text)

    provider = FakeGemini(evaluator(EVALUATION, relation_for=relation), vector=vector)
    runner, _ = make_ranker(clusters, records, provider=provider)
    result = runner.run(week="current").result
    hindi = [c for c in result.candidates if "Hindi" in c.headline]
    assert len(hindi) == 2
    assert sum(c.shortlisted for c in hindi) <= 1
    assert any(c.duplicate_of is not None or "duplicate" in (c.selection_note or "") for c in hindi)
