"""The analysis engine with a scripted fake Gemini: prompts, retries, repair, quota and budget handling."""

import asyncio
import json

from app.intelligence.engine import StoryAnalyzer
from app.intelligence.providers.base import ProviderError
from app.intelligence.quota import MemoryUsageLedger
from app.models.enums import AnalysisStatus
from tests.intelligence_helpers import (
    CATEGORIES,
    FAKE_KEY,
    INJECTION_ITEM,
    THIN_ITEM,
    FakeProvider,
    NoSleep,
    ai_settings,
    clusters,
    evidence_of,
    good_draft,
    grounded,
    response,
)
from tests.processing_helpers import NOW, staged


def analyse(cluster, provider, **settings):
    sleep = NoSleep()
    ledger = MemoryUsageLedger()
    analyzer = StoryAnalyzer(
        ai_settings(**settings), provider, CATEGORIES, ledger=ledger, sleep=sleep, clock=lambda: NOW
    )
    outcome = asyncio.run(analyzer.analyse(cluster, NOW))
    return outcome, sleep, ledger, analyzer


def test_request_uses_the_exact_model_structured_output_and_no_temperature():
    provider = FakeProvider()
    outcome, *_ = analyse(clusters()[0], provider)
    assert outcome.status is AnalysisStatus.COMPLETED
    request = provider.requests[0]
    assert request.model == "gemini-3.5-flash-lite"
    assert request.thinking_level == "minimal"
    assert request.json_schema["type"] == "object"
    assert not hasattr(request, "temperature")
    assert outcome.analysis.model_id == "gemini-3.5-flash-lite"
    assert outcome.analysis.generation.model_version == "gemini-3.5-flash-lite-test"


def test_source_content_is_delimited_data_and_never_in_the_system_prompt():
    provider = FakeProvider(grounded)
    analyse(clusters([INJECTION_ITEM])[0], provider)
    system, user = provider.requests[0].messages[0].content, provider.requests[0].messages[1].content
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in system
    assert "untrusted" in system.lower()
    evidence = evidence_of(provider.requests[0])  # parses: the fake end marker inside the article cannot close it
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in evidence["sources"][0]["excerpt"]
    assert user.rstrip().endswith("Return only the JSON object required by the schema.")


def test_injected_instructions_cannot_change_urls_or_categories():
    """Even if the model obeyed the injected text, validation rejects the invented URL, and categories are
    enum-limited; the stored sources keep the real URL."""

    def obeys(request):
        return grounded(request, **{"why_it_matters.interpretation": "Details: https://evil.example.net/claim"})

    outcome, *_ = analyse(clusters([INJECTION_ITEM])[0], FakeProvider(obeys), ai_max_repair_attempts=0)
    assert outcome.status is AnalysisStatus.INVALID_OUTPUT
    assert "unsupported_url" in {i.code for i in outcome.issues}

    ok, *_ = analyse(clusters([INJECTION_ITEM])[0], FakeProvider(grounded))
    assert ok.analysis.analysis_status == "needs_review"  # the source was flagged by Stage 3.2
    assert [s.original_url for s in ok.analysis.sources] == ["https://labs.example.com/blog/grants"]


def test_a_failed_validation_gets_one_repair_with_the_problems():
    bad = response(good_draft(summary=good_draft()["summary"].replace("71.2%", "99%")))
    provider = FakeProvider(bad, response(good_draft()))
    outcome, *_ = analyse(clusters()[0], provider)
    assert outcome.status is AnalysisStatus.COMPLETED
    assert outcome.calls == 2
    repair = provider.requests[1].messages
    assert [m.role for m in repair] == ["system", "user", "assistant", "user"]  # never ends with a model turn
    assert "99" in repair[-1].content and "Numbers not found" in repair[-1].content


def test_invalid_after_repairs_is_reported_not_patched():
    bad = response("{not json")
    outcome, *_ = analyse(clusters()[0], FakeProvider(bad))
    assert outcome.status is AnalysisStatus.INVALID_OUTPUT and outcome.analysis is None
    assert outcome.calls == 2


def test_truncated_output_is_not_accepted():
    outcome, *_ = analyse(
        clusters()[0], FakeProvider(response(good_draft(), finish="length")), ai_max_repair_attempts=0
    )
    assert outcome.status is AnalysisStatus.INVALID_OUTPUT
    assert outcome.issues[0].code == "output_truncated"


def test_rate_limits_are_retried_with_backoff_and_counted():
    limited = ProviderError("rate_limited", "rate limit", status_code=429, retry_after=7.0)
    provider = FakeProvider(limited, limited, response(good_draft()))
    outcome, sleep, ledger, _ = analyse(clusters()[0], provider)
    assert outcome.status is AnalysisStatus.COMPLETED and outcome.calls == 3
    assert len(sleep.delays) == 2 and all(7.0 <= d <= 7.5 for d in sleep.delays)
    snap = ledger.snapshot("gemini-3.5-flash-lite", NOW)
    assert snap.requests == 3 and snap.rate_limited == 2 and snap.output_tokens == 400


def test_timeouts_use_exponential_backoff_then_fail_the_story():
    timeout = ProviderError("timeout", "timeout")
    outcome, sleep, *_ = analyse(clusters()[0], FakeProvider(timeout), ai_max_retries=3)
    assert outcome.status is AnalysisStatus.FAILED and not outcome.fatal
    assert outcome.calls == 4
    base = [round(d) for d in sleep.delays]
    assert base == [2, 4, 8, 16] or base[:3] == [2, 4, 8]


def test_daily_quota_from_google_stops_without_retrying():
    quota = ProviderError("quota", "Gemini free-tier quota used up", status_code=429)
    outcome, sleep, ledger, analyzer = analyse(clusters()[0], FakeProvider(quota))
    assert outcome.status is AnalysisStatus.QUOTA_LIMITED and outcome.fatal and outcome.calls == 1
    assert sleep.delays == []
    assert ledger.snapshot("gemini-3.5-flash-lite", NOW).quota_exhausted_until is not None
    assert analyzer.quota_stopped


def test_configured_daily_limit_stops_before_calling_gemini():
    provider = FakeProvider()
    outcome, *_ = analyse(clusters()[0], provider, ai_requests_per_day=0)
    assert outcome.status is AnalysisStatus.COMPLETED
    sleep = NoSleep()
    ledger = MemoryUsageLedger()
    s = ai_settings(ai_requests_per_day=1)
    analyzer = StoryAnalyzer(s, provider, CATEGORIES, ledger=ledger, sleep=sleep, clock=lambda: NOW)
    first = asyncio.run(analyzer.analyse(clusters()[0], NOW))
    second = asyncio.run(analyzer.analyse(clusters()[0], NOW))
    assert first.status is AnalysisStatus.COMPLETED
    assert second.status is AnalysisStatus.QUOTA_LIMITED and second.calls == 0
    assert len(provider.requests) == 2  # one from the unlimited run above, one here


def test_a_missing_model_never_switches_to_another_model():
    missing = ProviderError("model_unavailable", "model not found", status_code=404)
    provider = FakeProvider(missing)
    outcome, *_ = analyse(clusters()[0], provider)
    assert outcome.status is AnalysisStatus.FAILED and outcome.fatal
    assert {r.model for r in provider.requests} == {"gemini-3.5-flash-lite"}


def test_too_little_text_is_not_sent_to_the_model():
    provider = FakeProvider()
    outcome, *_ = analyse(clusters([THIN_ITEM])[0], provider)
    assert outcome.status is AnalysisStatus.INSUFFICIENT_EVIDENCE and outcome.analysis is None
    assert provider.requests == []


def test_long_input_is_trimmed_to_the_token_budget():
    long_text = "Example Labs described its new evaluation suite in detail. " * 300
    items = [
        staged("Example Labs publishes a long evaluation report", f"https://news-{i}.example.com/eval", long_text,
               source="news-a", publisher=f"Outlet {i}")
        for i in range(6)
    ]  # fmt: skip
    cluster = max(clusters(items), key=lambda c: c.source_count)
    provider = FakeProvider(grounded)
    outcome, *_ = analyse(cluster, provider, ai_max_input_tokens=3000, ai_max_sources_per_story=3)
    evidence = evidence_of(provider.requests[0])
    assert len(evidence["sources"]) <= 3
    assert all(len(s["excerpt"] or "") <= 800 for s in evidence["sources"])
    assert len(json.dumps(evidence)) // 4 < 3000
    if cluster.source_count > 3:
        assert "sources_trimmed_for_budget" in outcome.analysis.review_flags
        assert any(not s.sent_to_model for s in outcome.analysis.sources)


def test_api_key_is_never_in_prompts_or_outcomes():
    provider = FakeProvider()
    outcome, *_ = analyse(clusters()[0], provider)
    dumped = json.dumps(outcome.analysis.model_dump(mode="json")) + json.dumps(
        [m.content for r in provider.requests for m in r.messages]
    )
    assert FAKE_KEY not in dumped
