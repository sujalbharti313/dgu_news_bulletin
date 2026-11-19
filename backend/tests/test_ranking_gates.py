"""Stage 3.4 quality gates: a high score never overrides a critical verification problem, and no gate deletes."""

from app.ranking.duplicates import apply_duplicate_gate
from app.ranking.evaluate import evaluate_story
from app.ranking.gates import strictest
from app.ranking.window import edition_for
from tests.intelligence_helpers import FUNDING, HYPE, INDIA, INJECTION_ITEM, PAPER, POLICY, THIN_ITEM
from tests.processing_helpers import NOW, cluster_of, staged
from tests.ranking_helpers import CONFIG, REGISTRY, WEEK, analysed, analysis_of, make_ranker

WINDOW = edition_for(NOW, week="current")


def _story(item, **analysis_changes):
    clusters, records = analysed([item])
    cluster = cluster_of(clusters, item)
    analysis = analysis_of(records, cluster)
    if analysis_changes:
        analysis = analysis.model_copy(update=analysis_changes)
    return cluster, analysis


def _boost(analysis, value=95):
    p = analysis.priority_assessment.model_copy(
        update={"global_impact": value, "technical_significance": value, "student_relevance": value}
    )
    return analysis.model_copy(update={"priority_assessment": p})


def test_strictest_action_wins():
    assert strictest([]) == "eligible"


def test_missing_source_requires_review_even_with_a_high_score():
    cluster, analysis = _story(PAPER)
    broken = cluster.model_copy(
        update={"representative": cluster.representative.model_copy(update={"original_url": ""})}
    )
    story = evaluate_story(broken, _boost(analysis), WINDOW, CONFIG, REGISTRY)
    assert "missing_source" in {g.gate for g in story.gates}
    assert story.eligibility == "review_required"


def test_insufficient_evidence_gate():
    thin = staged("Lab teases model", "https://news-b.example.com/teaser", None, source="news-b")
    clusters, records = analysed([thin], ai_min_evidence_words=0)
    cluster = clusters[0]
    analysis = analysis_of(records, cluster)
    story = evaluate_story(cluster, _boost(analysis), WINDOW, CONFIG, REGISTRY)
    gate = next(g for g in story.gates if g.gate == "insufficient_evidence")
    assert gate.action in ("review_required", "ineligible") and story.eligibility != "eligible"
    insufficient = analysis.model_copy(
        update={"evidence": analysis.evidence.model_copy(update={"sufficiency": "insufficient"})}
    )
    assert evaluate_story(cluster, insufficient, WINDOW, CONFIG, REGISTRY).eligibility == "ineligible"


def test_severe_claim_inconsistency_gate():
    cluster, analysis = _story(HYPE)
    misleading = analysis.model_copy(
        update={"headline": "Robot that thinks like a human will replace every worker, proves 99% accuracy"}
    )
    story = evaluate_story(cluster, _boost(misleading), WINDOW, CONFIG, REGISTRY)
    assert story.verification.claim_consistency_score < CONFIG.gates.severe_inconsistency.min_claim_consistency
    assert "severe_inconsistency" in {g.gate for g in story.gates}
    assert story.eligibility == "review_required"


def test_out_of_window_story_is_flagged_and_penalised():
    cluster, analysis = _story(INJECTION_ITEM)  # published 300 hours before NOW: before the edition week
    story = evaluate_story(cluster, analysis, WINDOW, CONFIG, REGISTRY)
    assert "out_of_window" in story.flags and story.scores.timeliness < 50
    assert {g.gate for g in story.gates} >= {"out_of_window"}
    assert story.eligibility == "ineligible"


def test_late_arrival_is_penalised_but_not_gated():
    late = staged(
        "Example Labs publishes evaluation toolkit for agents",
        "https://news-a.example.com/agent-eval-toolkit",
        "Example Labs published an open-source evaluation toolkit for language-model agents on GitHub, with "
        "benchmarks for tool use and a dataset of 2,000 tasks. The code is released under the MIT licence.",
        source="news-a",
        publisher="News A",
        hours_ago=24 * 5 + 6,  # Sunday 1 November: one day before the edition week
    )
    cluster, analysis = _story(late)
    story = evaluate_story(cluster, analysis, WINDOW, CONFIG, REGISTRY)
    assert "late_arrival" in story.flags and story.scores.timeliness == CONFIG.timeliness.late_arrival_score
    assert "out_of_window" not in {g.gate for g in story.gates}


def test_unverified_major_claim_from_unreviewed_single_source():
    cluster, analysis = _story(FUNDING)
    story = evaluate_story(cluster, _boost(analysis), WINDOW, CONFIG, REGISTRY)
    assert "unverified_major_claim" in {g.gate for g in story.gates}
    assert story.eligibility == "review_required"


def test_major_claim_from_primary_source_is_capped_not_gated():
    arxiv = staged(
        "Sparse routing halves inference memory in mixture-of-experts models",
        "https://arxiv.org/abs/2611.04321",
        "We present a routing method for mixture-of-experts language models that activates fewer experts per "
        "token. On our benchmarks it reduces inference memory by 48% with an accuracy drop under 1 point.",
        source="arxiv",
        publisher="arXiv",
    )
    cluster, analysis = _story(arxiv)
    story = evaluate_story(cluster, _boost(analysis), WINDOW, CONFIG, REGISTRY)
    assert "unverified_major_claim" not in {g.gate for g in story.gates}
    assert "unverified_benchmark_claim" in story.verification.hype_flags
    assert story.scores.technical_significance <= 75


def test_stage_33_review_flags_require_review():
    cluster, analysis = _story(INJECTION_ITEM)
    assert analysis.analysis_status == "needs_review"
    story = evaluate_story(cluster, analysis, WINDOW, CONFIG, REGISTRY)
    assert "analysis_needs_review" in {g.gate for g in story.gates}


def test_gates_can_be_switched_off():
    cluster, analysis = _story(FUNDING)
    relaxed = CONFIG.model_copy(
        update={
            "gates": CONFIG.gates.model_copy(
                update={
                    "unverified_major_claim": CONFIG.gates.unverified_major_claim.model_copy(update={"enabled": False}),
                    "unknown_single_source": CONFIG.gates.unknown_single_source.model_copy(update={"enabled": False}),
                }
            )
        }  # noqa: E501
    )
    story = evaluate_story(cluster, _boost(analysis), WINDOW, relaxed, REGISTRY)
    assert "unverified_major_claim" not in {g.gate for g in story.gates}


def _with_identifier(cluster, identifier):
    return cluster.model_copy(
        update={"sources": [s.model_copy(update={"identifiers": [identifier]}) for s in cluster.sources]}
    )


def test_duplicate_event_keeps_the_better_cluster_and_deletes_nothing():
    """Two clusters about one paper (Stage 3.2 kept them apart): only the better evidenced one stays eligible."""
    short = staged(
        "New routing preprint from the same lab",
        "https://news-b.example.com/2026/11/routing-preprint",
        "A short note says the lab posted a preprint about expert routing.",
        source="news-b",
        publisher="News B",
        hours_ago=2,
    )
    clusters, records = analysed([PAPER, short])
    a, b = cluster_of(clusters, PAPER), cluster_of(clusters, short)
    assert a is not b
    stories = [
        evaluate_story(_with_identifier(c, "arxiv:2611.01234"), analysis_of(records, c), WINDOW, CONFIG, REGISTRY)
        for c in (a, b)
    ]
    assert apply_duplicate_gate(stories, CONFIG) == 1
    kept, dup = sorted(stories, key=lambda e: e.duplicate_of is not None)
    assert kept.cluster_id == a.cluster_id  # the more complete, better evidenced cluster
    assert dup.duplicate_of == kept.cluster_id and dup.eligibility == "ineligible"
    assert "duplicate_event" in {g.gate for g in dup.gates} and kept.duplicate_of is None
    assert "same paper identifier" in next(g.reason for g in dup.gates if g.gate == "duplicate_event")
    assert dup.cluster.sources  # provenance preserved, nothing merged or deleted


def test_same_product_far_apart_is_not_a_duplicate():
    clusters, records = analysed([PAPER, POLICY])
    a, b = cluster_of(clusters, PAPER), cluster_of(clusters, POLICY)
    stories = [evaluate_story(c, analysis_of(records, c), WINDOW, CONFIG, REGISTRY) for c in (a, b)]
    assert apply_duplicate_gate(stories, CONFIG) == 0


def test_review_required_and_ineligible_stories_are_kept_in_the_results():
    items = [PAPER, INDIA, FUNDING, POLICY, INJECTION_ITEM, THIN_ITEM]
    clusters, records = analysed(items)
    runner, store = make_ranker(clusters, records)
    result = runner.run(week=WEEK).result
    statuses = {c.eligibility_status for c in result.candidates}
    assert {"review_required", "ineligible"} <= statuses
    assert all(c.shortlisted is False for c in result.candidates if c.eligibility_status != "eligible")
    assert len(result.excluded) == 1  # THIN_ITEM: no analysis possible, kept as ineligible
    assert result.totals.evaluated == len(result.candidates) + len(result.excluded)
    assert len(store.verifications) == result.totals.evaluated  # every story saved, none deleted
