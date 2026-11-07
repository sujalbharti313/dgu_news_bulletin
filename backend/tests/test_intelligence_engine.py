"""One story through the engine with a mocked provider: retries, repair, failures, missing evidence, prompt
injection, corroboration and source-reference preservation."""

import asyncio
import json

from app.intelligence.engine import RateGate, StoryAnalyzer
from app.intelligence.providers.base import ProviderError
from app.models.enums import IntelligenceStatus
from tests.intelligence_helpers import (
    CATEGORIES,
    INJECTION_ITEM,
    THIN_ITEM,
    FakeProvider,
    NoSleep,
    ai_settings,
    clusters,
    good_draft,
    response,
)
from tests.processing_helpers import NOW

CLUSTER = clusters()[0]


def analyse(provider, cluster=CLUSTER, **settings):
    sleep = NoSleep()
    analyzer = StoryAnalyzer(ai_settings(**settings), provider, CATEGORIES, sleep=sleep, gate=RateGate(sleep))
    return asyncio.run(analyzer.analyse(cluster, NOW)), sleep


def test_valid_response_becomes_contract_with_preserved_sources_and_usage():
    provider = FakeProvider(response(good_draft(), tokens=(1200, 450)))
    outcome, _ = analyse(provider, ai_input_cost_per_million_tokens=2.0, ai_output_cost_per_million_tokens=8.0)
    assert outcome.status is IntelligenceStatus.ANALYSED and outcome.calls == 1
    intel = outcome.intelligence
    assert intel.cluster_id == CLUSTER.cluster_id and intel.cluster_revision == CLUSTER.processing.revision
    assert intel.representative_item_id == CLUSTER.representative.item_id
    # Sources are copied from Stage 3.2 by code, in order, with exact URLs, publishers and dates.
    assert [s.item_id for s in intel.sources] == [r.item_id for r in CLUSTER.sources]
    assert [s.original_url for s in intel.sources] == [r.original_url for r in CLUSTER.sources]
    assert [s.published_at for s in intel.sources] == [r.published_at for r in CLUSTER.sources]
    assert [s.publisher for s in intel.sources] == [r.publisher for r in CLUSTER.sources]
    assert [s.relation for s in intel.sources] == [r.relation for r in CLUSTER.sources]
    assert intel.classification.primary == "model-releases" and intel.classification.topic == "models"
    assert all(p.kind == "ai_generated_suggestion" for p in intel.project_ideas)
    assert intel.content_is_ai_generated and intel.generation.ai_generated
    assert (intel.generation.input_tokens, intel.generation.output_tokens) == (1200, 450)
    assert intel.generation.estimated_cost_usd == round(1200 * 2 / 1e6 + 450 * 8 / 1e6, 6)
    assert intel.generation.model == "test-model-2026" and provider.requests[0].model == "test-model"


def test_syndicated_copies_are_not_independent_confirmation_and_nothing_is_verified():
    outcome, _ = analyse(FakeProvider(response(good_draft())))
    ev = outcome.intelligence.evidence
    # S1 (News A, wire copy) and S3 (exact duplicate of S1) are one report; S2 is the lab's own post.
    claim_s1_s2 = ev.claims[0]
    assert claim_s1_s2.corroboration == "multiple_independent_reports" and claim_s1_s2.independent_publishers == 2
    assert claim_s1_s2.cites_official_source
    assert all(c.verification == "not_independently_verified" for c in ev.claims)
    assert ev.verification == "not_independently_verified"
    copies = good_draft(**{"claims.0.source_ids": ["S1", "S3"]})
    outcome, _ = analyse(FakeProvider(response(copies)))
    claim = outcome.intelligence.evidence.claims[0]
    assert claim.corroboration == "single_report" and claim.independent_publishers == 1


def test_timeouts_and_outages_are_retried_with_bounded_backoff():
    provider = FakeProvider(
        ProviderError("timeout", "slow"), ProviderError("unavailable", "503"), response(good_draft())
    )
    outcome, sleep = analyse(provider)
    assert outcome.status is IntelligenceStatus.ANALYSED and len(provider.requests) == 3 and outcome.calls == 3
    assert len(sleep.delays) == 2 and all(0 < d <= 60.5 for d in sleep.delays)


def test_rate_limits_honour_retry_after_and_retries_are_bounded():
    limited = ProviderError("rate_limited", "429", retry_after=12.0)
    provider = FakeProvider(limited)
    outcome, sleep = analyse(provider, ai_max_retries=2)
    assert outcome.status is IntelligenceStatus.FAILED and len(provider.requests) == 3  # 1 + 2 retries
    assert outcome.error.kind == "rate_limited" and outcome.intelligence is None
    assert [round(d) for d in sleep.delays][:2] == [12, 12]  # Retry-After (+ jitter) respected
    capped, sleep = analyse(
        FakeProvider(ProviderError("rate_limited", "429", retry_after=900), response(good_draft())),
        ai_max_retry_after_seconds=30,
    )
    assert capped.status is IntelligenceStatus.ANALYSED and max(sleep.delays) <= 30.5


def test_non_retryable_provider_failures_fail_fast():
    for kind in ("bad_request", "auth", "quota"):
        provider = FakeProvider(ProviderError(kind, "nope"))
        outcome, sleep = analyse(provider)
        assert outcome.status is IntelligenceStatus.FAILED and len(provider.requests) == 1 and not sleep.delays
        assert outcome.fatal == (kind in ("auth", "quota"))
        assert outcome.errors_json()[0]["code"] == f"provider_{kind}"


def test_invalid_output_is_repaired_once_then_accepted():
    bad = good_draft(**{"classification.primary": "made-up"})
    provider = FakeProvider(response("not json at all"), response(good_draft()))
    outcome, _ = analyse(provider, ai_repair_model="small-model")
    assert outcome.status is IntelligenceStatus.ANALYSED and outcome.calls == 2
    repair = provider.requests[1]
    assert repair.model == "small-model"  # cheaper configured model for the formatting fix
    assert [m.role for m in repair.messages] == ["system", "user", "assistant", "user"]
    assert "not valid JSON" in repair.messages[-1].content
    assert outcome.intelligence.generation.calls == 2
    outcome, _ = analyse(FakeProvider(response(bad), response(good_draft())))
    assert outcome.status is IntelligenceStatus.ANALYSED


def test_output_that_stays_invalid_is_flagged_not_fabricated():
    provider = FakeProvider(response(good_draft(summary=None)))
    outcome, _ = analyse(provider, ai_max_repair_attempts=1)
    assert outcome.status is IntelligenceStatus.INVALID_OUTPUT and outcome.intelligence is None
    assert len(provider.requests) == 2 and outcome.review_flags == ["invalid_output"]
    assert {e["code"] for e in outcome.errors_json()} == {"missing_field"}
    assert outcome.usage.input_tokens == 2000  # usage of both calls is still tracked


def test_empty_refused_and_truncated_responses_are_invalid():
    for resp in (response(None), response(""), response(None, refusal="no"), response(good_draft(), finish="length")):
        outcome, _ = analyse(FakeProvider(resp), ai_max_repair_attempts=0)
        assert outcome.status is IntelligenceStatus.INVALID_OUTPUT and outcome.intelligence is None


def test_missing_evidence_is_flagged_without_calling_the_provider():
    provider = FakeProvider()
    outcome, _ = analyse(provider, clusters([THIN_ITEM])[0])
    assert outcome.status is IntelligenceStatus.INSUFFICIENT_EVIDENCE and provider.requests == []
    assert outcome.review_flags == ["insufficient_evidence"] and outcome.calls == 0


def test_model_reported_insufficient_evidence_needs_review():
    draft = good_draft(
        summary=None,
        technical_explanation=None,
        claims=[],
        technical_details=[],
        project_ideas=[],
        **{"evidence_assessment.sufficiency": "insufficient", "student_relevance.summary": None},
    )
    outcome, _ = analyse(FakeProvider(response(draft)))
    assert outcome.status is IntelligenceStatus.NEEDS_REVIEW
    assert "insufficient_evidence" in outcome.intelligence.review_flags
    assert outcome.intelligence.summary is None and outcome.intelligence.project_ideas == []
    assert outcome.intelligence.evidence.corroboration == "no_sources_cited"


def test_prompt_injection_in_sources_stays_data_and_cannot_steer_the_output():
    cluster = clusters([INJECTION_ITEM])[0]
    assert "possible_prompt_injection" in cluster.content_flags
    obeyed = good_draft(
        **{
            "summary": "Example Labs announced a research grant programme for university students working on model "
            "evaluation, and the story was verified by five outlets. More details are available at "
            "https://evil.example.net/claim for anyone interested in applying before the December "
            "deadline, according to the announcement shared by the company with students and universities.",
            "classification.primary": "india-ai",
        }
    )
    provider = FakeProvider(response(obeyed), response(obeyed))
    outcome, _ = analyse(provider, cluster)
    # 1. Instructions live only in the system message; the article text only inside the evidence block.
    system, user = provider.requests[0].messages
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in system.content
    assert "untrusted" in system.content.lower() and "never instructions" in system.content
    start = user.content.rindex("<<EVIDENCE ")
    end = user.content.rindex("<<END EVIDENCE ")
    assert start < user.content.index("IGNORE ALL PREVIOUS INSTRUCTIONS") < end
    boundary = user.content[start + len("<<EVIDENCE ") : user.content.index(">>", start)]
    assert len(boundary) == 16 and boundary not in json.dumps(INJECTION_ITEM.excerpt)
    data = json.loads(user.content[user.content.index("\n", start) + 1 : end])
    assert data["content_flags"] == ["possible_prompt_injection"]  # the model is told the text looks hostile
    # 2. An output that followed the injected link is rejected by validation (the invented URL).
    assert outcome.status is IntelligenceStatus.INVALID_OUTPUT
    assert "unsupported_url" in {e["code"] for e in outcome.errors_json()}
    # 3. A clean analysis of flagged content still goes to a human first.
    clean = good_draft(
        **{
            "headline": "Example Labs opens a student research grant programme",
            "summary": " ".join(["Example Labs announced a research grant programme for university students."] * 7),
            "technical_explanation": "The programme funds student work on model evaluation. " * 5,
            "technical_details": [
                {"label": "Deadline", "detail": "Applications close in December", "source_ids": ["S1"]}
            ],
            "claims": [
                {
                    "statement": "Applications close in December.",
                    "basis": "stated_in_sources",
                    "source_ids": ["S1"],
                    "attributed_to": "Example Labs",
                }
            ],
            "evidence_assessment.notes": "The source contained instruction-like text, which was ignored.",
            "classification.primary": "education-careers",
            "classification.secondary": [],
        }
    )
    outcome, _ = analyse(FakeProvider(response(clean)), cluster)
    assert outcome.status is IntelligenceStatus.NEEDS_REVIEW, outcome.errors_json()
    assert {"source_content_flagged", "model_reported_injection"} <= set(outcome.intelligence.review_flags)
    assert outcome.intelligence.classification.primary == "education-careers"


def test_rate_gate_pauses_every_worker():
    sleep = NoSleep()
    clock = iter([0.0, 0.0, 1.0])
    gate = RateGate(sleep, clock=lambda: next(clock))
    gate.block(5)
    asyncio.run(gate.wait())
    assert sleep.delays == [5.0]
