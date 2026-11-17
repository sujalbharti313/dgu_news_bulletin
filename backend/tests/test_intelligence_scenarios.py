"""The eight representative stories from the Stage 3.3 specification, end to end through Stage 3.2 clustering,
the engine, validation, priority and the stored contract. Gemini is replaced by scripted responses, so these
tests check what the engine does with a response (accepts, rejects, caps, flags, preserves provenance), not the
quality of a live model's writing.
"""

from app.intelligence.contract import StoryAnalysis
from tests.intelligence_helpers import (
    FUNDING,
    HYPE,
    INDIA,
    LAB_POST,
    NEWS_REPORT,
    PAPER,
    POLICY,
    SPARSE_ITEM,
    SYNDICATED_COPY,
    THIN_ITEM,
    FakeProvider,
    clusters,
    evidence_of,
    good_draft,
    grounded,
    make_runner,
    response,
)

LONG_ENOUGH = (
    " The sources attribute these points to the people involved and do not include independent evaluations, so "
    "readers should treat the details as reported claims rather than established results for now."
)


def analyse_one(items, step):
    provider = FakeProvider(step)
    runner, store = make_runner(clusters(items), provider)
    report = runner.run()
    assert report.status == "succeeded", report.as_dict()
    analyses = runner.list(statuses=["completed", "needs_review", "insufficient_evidence"])
    record = next(iter(store.records.values()))
    return (analyses[0] if analyses else None), record, provider


def test_1_and_8_major_model_release_reported_by_several_publishers():
    analysis, record, provider = analyse_one([LAB_POST, NEWS_REPORT, SYNDICATED_COPY], response(good_draft()))
    assert analysis.primary_category == "AI Model Releases"
    assert analysis.secondary_categories == ["AI Tools and Development"]
    # Three source references, but the syndicated copy does not count as an independent publisher.
    assert len(analysis.sources) == 3
    assert analysis.evidence.corroboration == "multiple_independent_reports"
    assert analysis.evidence.independent_publishers == 2
    assert analysis.evidence.official_source_present
    # Original URLs and publishers are exactly Stage 3.2's, all of them.
    assert {s.original_url for s in analysis.sources} == {
        LAB_POST.original_url, NEWS_REPORT.original_url, SYNDICATED_COPY.original_url,
    }  # fmt: skip
    assert {s.publisher for s in analysis.sources} == {"Example Labs Blog", "News A"}
    # All publishers' material was sent together in one request.
    assert len(evidence_of(provider.requests[0])["sources"]) == 3
    p = analysis.priority_assessment
    assert (p.weighted_score, p.overall_score, p.priority_level) == (71, 71, "High")
    assert analysis.technical_explanation_status == "available"
    assert analysis.project_ideas[0].kind == "ai_generated_suggestion"
    assert record.priority_score == 71 and record.primary_category == "ai-model-releases"
    assert analysis.model_id == "gemini-3.5-flash-lite"


def test_2_research_paper():
    def step(request):
        return grounded(
            request,
            headline="Researchers propose sparse routing that cuts inference memory in mixture-of-experts models",
            summary="Researchers describe a routing method for mixture-of-experts language models that activates "
            "fewer experts per token. In their own benchmarks it reduces inference memory by 48% with an accuracy "
            "drop under 1 point, and they released code and weights under an open licence." + LONG_ENOUGH,
            **{
                "classification.primary": "ai-research",
                "classification.secondary": ["ai-infrastructure-hardware"],
                "priority.technical_significance": 90,
                "priority.research_practical_importance": 85,
                "priority.global_impact": 55,
            },
        )

    analysis, _, _ = analyse_one([PAPER], step)
    assert analysis.primary_category == "AI Research"
    assert analysis.sources[0].source_kind == "research_repository"
    assert "single_report" in analysis.review_flags
    # A single non-official report is capped below Critical however high the scores.
    assert analysis.priority_assessment.priority_level in {"High", "Medium"}


def test_3_indian_ai_development():
    def step(request):
        return grounded(request, **{"classification.primary": "india-ai", "classification.secondary": ["ai-research"]})

    analysis, _, _ = analyse_one([INDIA], step)
    assert analysis.primary_category == "India × AI" and analysis.topic == "india"


def test_3b_invented_figures_about_the_dataset_are_rejected():
    def invents(request):
        return grounded(request, summary="Researchers at IIT Example released 5,000 hours of speech." + LONG_ENOUGH * 2)

    provider = FakeProvider(invents)
    runner, store = make_runner(clusters([INDIA]), provider)
    report = runner.run()
    assert report.counts["invalid_output"] == 1
    record = next(iter(store.records.values()))
    assert record.analysis is None and any(e["code"] == "unsupported_number" for e in record.errors)


def test_4_startup_funding_announcement():
    def step(request):
        return grounded(
            request,
            technical_explanation={"status": "not_applicable", "text": None},
            project_ideas=[],
            **{
                "classification.primary": "industry-business",
                "classification.secondary": ["robotics-automation"],
                "priority.technical_significance": 15,
                "priority.global_impact": 30,
                "priority.student_relevance": 35,
                "priority.research_practical_importance": 20,
                "priority.timeliness": 70,
            },
        )

    analysis, _, _ = analyse_one([FUNDING], step)
    assert analysis.primary_category == "Industry and Business"
    assert analysis.technical_explanation is None and analysis.technical_explanation_status == "not_applicable"
    assert analysis.project_ideas == []
    assert analysis.priority_assessment.priority_level == "Low"


def test_5_safety_and_governance():
    def step(request):
        return grounded(request, **{"classification.primary": "ai-safety-governance", "classification.secondary": []})

    analysis, _, _ = analyse_one([POLICY], step)
    assert analysis.primary_category == "AI Safety and Governance" and analysis.topic == "policy"


def test_6_misleading_headline_is_rewritten_and_capped():
    def step(request):
        return grounded(
            request,
            headline="University lab demonstrates a robot arm that sorts objects with a vision-language model",
            **{
                "evidence_assessment.sufficiency": "partial",
                "evidence_assessment.evidence_notes": [
                    "The source headline overstates the result: the excerpt describes a limited lab demonstration."
                ],
                "classification.primary": "robotics-automation",
                "classification.secondary": [],
                "priority.global_impact": 95,
                "priority.technical_significance": 90,
                "priority.student_relevance": 90,
                "priority.timeliness": 90,
                "priority.research_practical_importance": 90,
                "priority.marketing_heavy": True,
            },
        )

    analysis, _, _ = analyse_one([HYPE], step)
    assert "THINKS" not in analysis.headline
    p = analysis.priority_assessment
    assert p.weighted_score >= 85 and p.overall_score == 69 and p.priority_level == "Medium"
    assert any("promotional" in a for a in p.adjustments)
    assert {"partial_evidence", "marketing_heavy"} <= set(analysis.review_flags)
    # The original (exaggerated) headline is still recorded as the source's own title.
    assert analysis.sources[0].title == HYPE.title


def test_6b_hype_words_in_our_own_summary_are_rejected():
    def hype(request):
        return grounded(request, summary="A revolutionary robot was shown in a lab." + LONG_ENOUGH * 2)

    runner, store = make_runner(clusters([HYPE]), FakeProvider(hype))
    assert runner.run().counts["invalid_output"] == 1


def test_7_story_with_too_little_text_is_not_sent():
    provider = FakeProvider()
    runner, store = make_runner(clusters([THIN_ITEM]), provider)
    report = runner.run()
    assert report.counts["insufficient_evidence"] == 1 and provider.requests == []
    record = next(iter(store.records.values()))
    assert record.analysis is None and "not_sent_to_model" in record.review_flags
    assert runner.run().counts.get("up_to_date") == 1  # never retried for the same revision


def test_7b_model_reports_insufficient_evidence():
    def step(request):
        return grounded(
            request,
            summary=None,
            technical_explanation={"status": "insufficient_evidence", "text": None},
            project_ideas=[],
            **{
                "evidence_assessment.sufficiency": "insufficient",
                "evidence_assessment.missing_information": ["Model name", "Release date", "Any capabilities"],
                "priority.global_impact": 80,
                "priority.technical_significance": 80,
            },
        )

    analysis, record, _ = analyse_one([SPARSE_ITEM], step)
    assert analysis.analysis_status == "insufficient_evidence"
    assert analysis.summary is None and analysis.technical_explanation is None
    assert analysis.priority_assessment.priority_level == "Low"
    assert record.status == "insufficient_evidence" and record.analysis is not None
    StoryAnalysis.model_validate(record.analysis)  # the stored JSON is the validated contract
