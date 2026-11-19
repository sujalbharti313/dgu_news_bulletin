"""Stage 3.4 reliability with the optional Gemini consistency check: caching, quota exhaustion, HTTP 429, outages,
malformed output, retries, partial failure, resume and duplicate runs. Every response is mocked: no quota used."""

import json
from datetime import timedelta

from app.intelligence.providers.base import ProviderError
from app.intelligence.quota import MemoryUsageLedger, next_reset
from app.ranking.ai_check import SYSTEM_PROMPT, validate
from app.ranking.store import MemoryRankingStore
from tests.intelligence_helpers import INDIA, INJECTION_ITEM, LAB_POST, NEWS_REPORT, PAPER, FakeProvider, response
from tests.processing_helpers import NOW
from tests.ranking_helpers import (
    WEEK,
    analysed,
    analysis_of,
    check_reply,
    check_request_data,
    check_response,
    make_ranker,
    ranking_settings,
)

ITEMS = [LAB_POST, NEWS_REPORT, PAPER, INDIA]
MODEL = "gemini-3.5-flash-lite"


def setup(provider, *, store=None, ledger=None, **settings):
    clusters, records = analysed(ITEMS)
    runner, store = make_ranker(
        clusters,
        records,
        provider=provider,
        store=store,
        ledger=ledger,
        settings=ranking_settings(ranking_ai_check=True, **settings),
    )
    return runner, store


def statuses(result):
    return result.ai_check["statuses"]


def test_ai_check_runs_once_per_story_and_is_cached():
    provider = FakeProvider(check_reply())
    runner, store = setup(provider)
    first = runner.run(week=WEEK)
    assert first.status == "completed", first.reason
    checked = statuses(first.result).get("completed", 0)
    assert checked >= 2 and len(provider.requests) == checked
    assert all(c.verification.checked_by == ["rules", "gemini"] for c in first.result.shortlist)
    second = runner.run(week=WEEK)
    assert len(provider.requests) == checked  # nothing re-sent for unchanged stories
    assert statuses(second.result).get("cached") == checked
    assert second.reused_run and second.run_id == first.run_id and len(store.runs) == 1


def test_quota_exhaustion_stops_requests_and_keeps_stories_pending():
    ledger = MemoryUsageLedger()
    provider = FakeProvider(ProviderError("quota", "The Gemini free-tier quota is used up.", status_code=429))
    runner, _ = setup(provider, ledger=ledger)
    report = runner.run(week=WEEK)
    assert len(provider.requests) == 1  # stopped at once
    assert report.status == "incomplete" and "Not a final ranking" in report.reason
    assert report.result.status == "incomplete" and report.result.shortlist == []  # nothing unverified is eligible
    assert {p.reason for p in report.result.pending} == {"ai_check_pending"}
    assert any(a["kind"] == "quota_exhausted" for a in runner.alerts)
    assert ledger.snapshot(MODEL, NOW).quota_exhausted_until == next_reset(NOW)
    # Later runs send nothing until the reset.
    runner.run(week=WEEK)
    assert len(provider.requests) == 1


def test_local_daily_limit_is_respected():
    provider = FakeProvider(check_reply())
    runner, _ = setup(provider, ai_requests_per_day=1)
    report = runner.run(week=WEEK)
    assert len(provider.requests) == 1
    assert statuses(report.result)["completed"] == 1 and statuses(report.result)["pending"] >= 1
    assert report.status == "incomplete"


def test_ledger_already_exhausted_sends_nothing():
    ledger = MemoryUsageLedger()
    ledger.mark_exhausted(MODEL, NOW, NOW + timedelta(hours=3))
    provider = FakeProvider(check_reply())
    runner, _ = setup(provider, ledger=ledger)
    report = runner.run(week=WEEK)
    assert provider.requests == [] and report.status == "incomplete"
    assert runner.alerts and runner.alerts[0]["kind"] == "quota_exhausted"


def test_http_429_is_retried_with_backoff():
    ledger = MemoryUsageLedger()
    provider = FakeProvider(
        ProviderError("rate_limited", "rate limit", status_code=429, retry_after=2.0), check_reply()
    )
    runner, _ = setup(provider, ledger=ledger)
    report = runner.run(week=WEEK)
    assert report.status == "completed"
    assert ledger.snapshot(MODEL, NOW).rate_limited == 1
    assert len(provider.requests) == statuses(report.result)["completed"] + 1


def test_provider_outage_fails_the_check_retries_later_and_never_makes_it_eligible():
    store = MemoryRankingStore()
    outage = FakeProvider(ProviderError("unavailable", "The Gemini API returned HTTP 503.", status_code=503))
    runner, store = setup(outage, store=store, ai_max_retries=1)
    report = runner.run(week=WEEK)
    assert report.status == "incomplete"
    failed = [c for c in report.result.candidates if c.verification.verification_status == "ai_check_failed"]
    assert failed and all(c.eligibility_status != "eligible" for c in failed)
    assert all("verification_incomplete" in {g.gate for g in c.gates} for c in failed)
    calls = len(outage.requests)
    assert calls == 2 * len(failed)  # one retry each, then given up for this run
    rec = store.verifications[failed[0].story_cluster_id]
    assert rec.ai_check_attempts == 1 and rec.ai_check_next_attempt_at > NOW and rec.errors
    # Same time: waits for its retry time.
    runner.run(week=WEEK)
    assert len(outage.requests) == calls
    # After the delay, with the provider back: retried and completed.
    runner.clock = lambda: NOW + timedelta(hours=2)
    runner.provider_factory = lambda s: FakeProvider(check_reply())
    later = runner.run(week=WEEK)
    assert later.status == "completed", later.reason


def test_retry_failed_retries_immediately():
    runner, store = setup(FakeProvider(ProviderError("unavailable", "down", status_code=503)), ai_max_retries=0)
    assert runner.run(week=WEEK).status == "incomplete"
    ok = FakeProvider(check_reply())
    runner.provider_factory = lambda s: ok
    assert runner.run(week=WEEK, retry_failed=True).status == "completed"
    assert ok.requests


def test_malformed_output_is_repaired_once():
    provider = FakeProvider(response("this is not json"), check_reply())
    runner, _ = setup(provider)
    report = runner.run(week=WEEK)
    assert report.status == "completed"
    repair = provider.requests[1]
    assert repair.messages[-1].role == "user" and "failed validation" in repair.messages[-1].content


def test_persistently_malformed_output_is_invalid_and_pending():
    provider = FakeProvider(response({"headline": "nope"}))
    runner, store = setup(provider, ai_max_repair_attempts=1)
    report = runner.run(week=WEEK)
    assert statuses(report.result).get("invalid_output", 0) >= 1 and report.status == "incomplete"
    rec = next(r for r in store.verifications.values() if r.ai_check_status == "invalid_output")
    assert rec.errors and all(e["code"] == "invalid_output" for e in rec.errors)


def test_partial_failure_is_isolated():
    def answer(request):
        if "Sparse routing" in request.messages[1].content:
            raise ProviderError("unavailable", "down", status_code=503)
        return check_reply()

    runner, _ = setup(FakeProvider(answer), ai_max_retries=0)
    report = runner.run(week=WEEK)
    by_status = statuses(report.result)
    assert by_status.get("failed") == 1 and by_status.get("completed", 0) >= 1
    assert report.result.shortlist  # the others are ranked normally
    assert all("Sparse routing" not in c.headline for c in report.result.shortlist)


def test_resume_after_quota_reset():
    ledger = MemoryUsageLedger()
    runner, _ = setup(FakeProvider(ProviderError("quota", "used up", status_code=429)), ledger=ledger)
    assert runner.run(week=WEEK).status == "incomplete"
    ok = FakeProvider(check_reply())
    runner.provider_factory = lambda s: ok
    runner.clock = lambda: next_reset(NOW) + timedelta(minutes=5)
    resumed = runner.run(week=WEEK)
    assert resumed.status == "completed" and ok.requests


def test_concurrent_run_is_skipped():
    runner, store = setup(FakeProvider(check_reply()))
    store.start(NOW)  # another process holds the lock
    report = runner.run(week=WEEK)
    assert report.status == "skipped" and report.result is None


def test_ai_check_disabled_sends_no_requests():
    called = []
    clusters, records = analysed(ITEMS)
    runner, _ = make_ranker(clusters, records, settings=ranking_settings(ranking_ai_check=False))
    runner.provider_factory = lambda s: called.append(1)
    report = runner.run(week=WEEK)
    assert called == [] and report.status == "completed"
    assert report.result.ai_check["enabled"] is False
    assert all("rules only" in " ".join(c.verification.limitations) for c in report.result.candidates)


def test_missing_key_with_ai_check_on_fails_before_any_work():
    clusters, records = analysed(ITEMS)
    runner, store = make_ranker(
        clusters, records, settings=ranking_settings(ranking_ai_check=True, gemini_api_key=None)
    )
    report = runner.run(week=WEEK)
    assert report.status == "failed" and "RANKING_AI_CHECK=false" in report.reason
    assert report.job_id is None and store.runs == {}


def test_source_text_stays_inside_the_data_block():
    provider = FakeProvider(check_reply())
    clusters, records = analysed([INJECTION_ITEM, PAPER])
    runner, _ = make_ranker(clusters, records, provider=provider, settings=ranking_settings(ranking_ai_check=True))
    runner.run(week=WEEK)
    assert provider.requests
    for request in provider.requests:
        system, user = request.messages[0].content, request.messages[1].content
        assert system == SYSTEM_PROMPT and "IGNORE ALL PREVIOUS" not in system
        data = check_request_data(request)
        assert data["statements_to_check"]["note"].startswith("AI-generated")
        assert sum(1 for line in user.splitlines() if line.startswith("<<DATA ")) == 1


def test_ai_result_cannot_raise_scores():
    clusters, records = analysed([PAPER])
    rules, _ = make_ranker(clusters, records)
    with_ai, _ = make_ranker(
        clusters, records, provider=FakeProvider(check_reply()), settings=ranking_settings(ranking_ai_check=True)
    )
    a = rules.run(week=WEEK).result.candidates[0]
    b = with_ai.run(week=WEEK).result.candidates[0]
    assert b.verification.evidence_quality_score <= a.verification.evidence_quality_score
    assert b.verification.source_credibility_score == a.verification.source_credibility_score
    assert b.final_scores.final_priority_score <= a.final_scores.final_priority_score


def test_validation_rejects_bad_ai_output():
    clusters, records = analysed([PAPER])
    from app.intelligence.evidence import build_evidence

    analysis = analysis_of(records, clusters[0])
    pack = build_evidence(clusters[0])
    ok, problems = validate(json.dumps(check_response()), analysis, pack)
    assert ok is not None and problems == []
    cases = [
        check_response(claims=[{"index": 0, "verdict": "supported", "source_ids": ["S9"], "explanation": "x y z"}]),
        check_response(claims=[{"index": 7, "verdict": "supported", "source_ids": ["S1"], "explanation": "x y z"}]),
        check_response(headline={"verdict": "supported", "explanation": "See https://evil.example.net/x"}),
        check_response(content_nature="breaking"),
        {**check_response(), "publish": True},
    ]
    for body in cases:
        draft, problems = validate(json.dumps(body), analysis, pack)
        assert draft is None and problems, body
    assert validate("", analysis, pack)[0] is None
    assert validate("[1, 2]", analysis, pack)[0] is None
