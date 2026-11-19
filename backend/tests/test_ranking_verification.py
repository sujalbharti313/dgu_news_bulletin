"""Stage 3.4 verification engine: credibility, evidence, claim consistency, corroboration, substance and hype.

Real Stage 3.2 clusters and real Stage 3.3 analyses (made with a scripted fake model). No network, no quota."""

from app.ranking.ai_check import AiCheckDraft
from app.ranking.evaluate import evaluate_story
from app.ranking.verification import verify
from app.ranking.window import edition_for
from tests.intelligence_helpers import FUNDING, HYPE, LAB_POST, NEWS_REPORT, PAPER, SYNDICATED_COPY
from tests.processing_helpers import NOW, cluster_of, staged
from tests.ranking_helpers import CONFIG, REGISTRY, analysed, analysis_of, check_response, title_responder

WINDOW = edition_for(NOW, week="current")

ARXIV = staged(
    "Sparse routing halves inference memory in mixture-of-experts models",
    "https://arxiv.org/abs/2611.04321",
    "We present a routing method for mixture-of-experts language models that activates fewer experts per token. "
    "On our benchmarks it reduces inference memory by 48% with an accuracy drop under 1 point. Code and weights "
    "are released under an open licence on GitHub. This preprint has not been peer reviewed.",
    source="arxiv",
    publisher="arXiv",
    hours_ago=20,
)
UNKNOWN = staged(
    "Startup claims new chip for running language models on phones",
    "https://unknown-blog.example.net/2026/11/phone-chip",
    "A startup called Example Silicon says its new chip can run small language models on phones with lower power "
    "use. It did not publish measurements, a datasheet or a release date.",
    source="mystery-feed",
    publisher="Unknown Blog",
    hours_ago=10,
)
PROMO = staged(
    "Example Labs unveils revolutionary AI-powered assistant",
    "https://labs.example.com/blog/assistant",
    "We're excited to announce our revolutionary, game-changing AI-powered assistant that will transform how you "
    "work. It is the best assistant available and is seamless and magical. Sign up today and join the waitlist.",
    source="lab-blog",
    publisher="Example Labs Blog",
    hours_ago=15,
)
DATASET_A = staged(
    "IIT Example releases open speech dataset for Hindi and Marathi",
    "https://news-a.example.com/2026/11/iit-speech-dataset",
    "Researchers at IIT Example released an open dataset of 1,200 hours of speech in Hindi and Marathi for "
    "training speech recognition systems, recorded by volunteers in 40 districts.",
    source="news-a",
    publisher="News A",
    hours_ago=30,
)
DATASET_B = staged(
    "IIT Example releases open Hindi and Marathi speech dataset for researchers",
    "https://news-b.example.com/2026/11/iit-example-dataset",
    "A team at IIT Example has published 900 hours of speech in Hindi and Marathi, collected from volunteers, to "
    "help universities and startups build voice assistants. Interviews with the lead researcher describe plans "
    "for Bengali and Tamil next year.",
    source="news-b",
    publisher="News B",
    hours_ago=28,
)


def _verify(items, target, responder=None, ai=None, **settings):
    clusters, records = analysed(items, responder, **settings)
    cluster = cluster_of(clusters, target)
    analysis = analysis_of(records, cluster)
    return (
        cluster,
        analysis,
        verify(cluster, analysis, CONFIG, REGISTRY, ai=ai, ai_status="completed" if ai else "disabled"),
    )


def test_credible_primary_research_source():
    _, _, out = _verify([ARXIV], ARXIV)
    v = out.verification
    [source] = v.source_assessments
    assert source.basis == "registry" and source.credibility_level == "primary" and source.reviewed_on
    assert v.source_credibility_level == "primary" and v.source_credibility_score == CONFIG.credibility.primary
    assert v.content_nature == "research_publication"
    assert "paper_identifier" in v.technical_signals and v.technical_substance_score >= 50
    assert any("links to papers" in r and r.startswith("+15") for r in v.evidence_reasons)
    assert v.externally_fact_checked is False and v.checked_by == ["rules"]


def test_unknown_source_is_unknown_not_false():
    cluster, analysis, out = _verify([UNKNOWN], UNKNOWN)
    v = out.verification
    assert v.source_credibility_level == "unknown" and v.source_assessments[0].basis == "none"
    assert any("not a judgement of falsity" in lim for lim in v.source_assessments[0].limitations)
    story = evaluate_story(cluster, analysis, WINDOW, CONFIG, REGISTRY)
    # Not shortlisted automatically, but not rejected either: an admin can review it.
    assert story.eligibility == "review_required"
    assert {g.gate for g in story.gates} >= {"unknown_single_source"}
    assert all(g.action != "ineligible" for g in story.gates)


def test_configured_but_unreviewed_source_is_provisional():
    _, _, out = _verify([FUNDING], FUNDING)
    assert out.verification.source_credibility_level == "provisional"
    assert out.verification.source_assessments[0].basis == "ingestion_category"


def test_promotional_company_announcement_is_flagged_not_rejected():
    cluster, analysis, out = _verify(
        [PROMO],
        PROMO,
        title_responder(
            {
                "revolutionary": {
                    "headline": "Example Labs announces a new assistant product",
                    "priority.marketing_heavy": True,
                }
            }
        ),
    )
    v = out.verification
    assert {"marketing_language", "promotional_language", "marketing_only_announcement"} <= set(v.hype_flags)
    assert v.content_nature == "promotional"
    story = evaluate_story(cluster, analysis, WINDOW, CONFIG, REGISTRY)
    assert story.scores.technical_significance <= 60
    assert story.scores.global_impact <= 65
    assert story.eligibility != "ineligible"  # a company announcement is never rejected for being one


def test_multiple_independent_reports():
    clusters, records = analysed([DATASET_A, DATASET_B])
    cluster = cluster_of(clusters, DATASET_A)
    assert cluster is cluster_of(clusters, DATASET_B), "fixture: both reports must be one Stage 3.2 cluster"
    v = verify(cluster, analysis_of(records, cluster), CONFIG, REGISTRY).verification
    assert v.corroboration_level == "multiple_independent"
    assert v.independent_source_count == 2 and v.source_count == 2
    assert v.source_credibility_score == CONFIG.credibility.provisional + CONFIG.credibility.corroboration_bonus


def test_conflicting_figures_between_sources_are_reported():
    clusters, records = analysed([DATASET_A, DATASET_B])
    cluster = cluster_of(clusters, DATASET_A)
    out = verify(cluster, analysis_of(records, cluster), CONFIG, REGISTRY)
    assert "source_conflict" in out.flags
    assert any("900" in c and "1200" in c for c in out.verification.conflicting_claims)


def test_syndicated_copies_count_once():
    clusters, records = analysed([NEWS_REPORT, SYNDICATED_COPY])
    cluster = cluster_of(clusters, NEWS_REPORT)
    v = verify(cluster, analysis_of(records, cluster), CONFIG, REGISTRY).verification
    assert v.source_count == 2 and v.corroboration.report_count == 1
    assert v.corroboration_level == "syndicated_single_source" and v.independent_source_count == 1
    assert [a.is_copy for a in v.source_assessments].count(True) == 1
    assert v.source_credibility_score == CONFIG.credibility.provisional  # copies add no corroboration bonus


def test_official_announcement_plus_independent_report():
    clusters, records = analysed([LAB_POST, NEWS_REPORT, SYNDICATED_COPY])
    cluster = cluster_of(clusters, LAB_POST)
    v = verify(cluster, analysis_of(records, cluster), CONFIG, REGISTRY).verification
    assert v.corroboration.official_source_present
    assert v.corroboration_level == "official_plus_independent" and v.independent_source_count == 2
    assert "unverified_benchmark_claim" not in v.hype_flags  # independently reported


def test_articles_repeating_one_announcement_are_one_account():
    echo = staged(
        "Example Labs releases Nimbus 2.5 with a 400K-token context window",
        "https://news-b.example.com/2026/11/nimbus-25-release",
        "Nimbus 2.5, the new Example Labs language model, has a 400K-token context window, the company said on "
        "Tuesday. Example Labs says Nimbus 2.5 scores 71.2% on SWE-bench Verified; its API price is the same.",
        source="news-b",
        publisher="News B",
    )
    clusters, records = analysed([LAB_POST, echo])
    cluster = cluster_of(clusters, LAB_POST)
    assert cluster is cluster_of(clusters, echo)
    v = verify(cluster, analysis_of(records, cluster), CONFIG, REGISTRY).verification
    assert v.corroboration_level == "repeated_announcement" and v.independent_source_count == 1
    assert "unverified_benchmark_claim" in v.hype_flags


def test_missing_original_url_is_flagged():
    clusters, records = analysed([PAPER])
    cluster = clusters[0]
    broken = cluster.model_copy(
        update={"representative": cluster.representative.model_copy(update={"original_url": "not a url"})}
    )
    out = verify(broken, analysis_of(records, cluster), CONFIG, REGISTRY)
    assert "missing_source_url" in out.flags
    assert any(r.startswith("+0 original article URL") for r in out.verification.evidence_reasons)


def test_unsupported_headline_claims():
    cluster, analysis, _ = _verify([HYPE], HYPE)
    worse = analysis.model_copy(update={"headline": "Robot arm solves sorting for every warehouse with 99% accuracy"})
    out = verify(cluster, worse, CONFIG, REGISTRY)
    assert {"headline_exaggeration", "unsupported_number"} <= out.flags
    assert any("99" in u for u in out.verification.unsupported_claims)
    assert out.verification.claim_consistency_score < 70


def test_sensational_source_headline_is_detected():
    _, _, out = _verify([HYPE], HYPE)
    assert "source_headline_exaggeration" in out.flags
    assert "speculation_as_certainty" in out.verification.hype_flags


def test_insufficient_article_content():
    title_only = staged("Example Labs mentions new model", "https://news-b.example.com/n/1", None, source="news-b")
    cluster, analysis, out = _verify([title_only], title_only, ai_min_evidence_words=0)
    v = out.verification
    assert "title_only" in out.flags and v.evidence_quality_score <= 30
    assert v.technical_substance_score is None
    assert any("too little source text" in lim for lim in v.limitations)
    story = evaluate_story(cluster, analysis, WINDOW, CONFIG, REGISTRY)
    assert "insufficient_evidence" in {g.gate for g in story.gates} and story.eligibility != "eligible"


def test_ai_generated_analysis_is_never_counted_as_evidence():
    """A claim citing a source that does not say it is weakly supported, however confident the analysis is."""
    cluster, analysis, _ = _verify([PAPER], PAPER)
    claim = analysis.claims[0].model_copy(
        update={"statement": "The method was adopted by every major cloud provider in production this month."}
    )
    out = verify(cluster, analysis.model_copy(update={"claims": [claim]}), CONFIG, REGISTRY)
    [check] = out.verification.claim_checks
    assert check.support == "weakly_supported"
    assert out.verification.unsupported_claims


def test_misleading_ai_summary_caught_by_the_ai_check():
    cluster, analysis, _ = _verify([HYPE], HYPE)
    ai = AiCheckDraft.model_validate(
        check_response(
            summary={"verdict": "contradicted", "explanation": "S1 says it was only tested in a lab."},
            claims=[{"index": 0, "verdict": "contradicted", "source_ids": ["S1"], "explanation": "S1 differs."}],
        )
    )
    story = evaluate_story(cluster, analysis, WINDOW, CONFIG, REGISTRY, ai=ai, ai_status="completed")
    v = story.verification
    assert {"summary_conflict", "contradicted_claim"} <= story.flags
    assert v.checked_by == ["rules", "gemini"] and v.conflicting_claims
    assert "severe_inconsistency" in {g.gate for g in story.gates}
    assert story.eligibility == "review_required"


def test_ai_check_can_only_make_nature_more_cautious():
    cluster, analysis, _ = _verify([PAPER], PAPER)
    rosy = AiCheckDraft.model_validate(check_response(content_nature="research_publication"))
    gloomy = AiCheckDraft.model_validate(check_response(content_nature="promotional"))
    nature_rules = verify(cluster, analysis, CONFIG, REGISTRY).verification.content_nature
    assert verify(cluster, analysis, CONFIG, REGISTRY, ai=rosy).verification.content_nature == nature_rules
    assert verify(cluster, analysis, CONFIG, REGISTRY, ai=gloomy).verification.content_nature == "promotional"
