"""Runs: idempotency, failure isolation, quota exhaustion and resume, locking. In memory, no network."""

from datetime import timedelta

from app.intelligence.providers.base import ProviderError
from app.intelligence.quota import MemoryUsageLedger, next_reset
from tests.intelligence_helpers import (
    FUNDING,
    INDIA,
    PAPER,
    POLICY,
    FakeProvider,
    ai_settings,
    clusters,
    grounded,
    make_runner,
)
from tests.processing_helpers import NOW, staged

FOUR = [PAPER, INDIA, FUNDING, POLICY]


def test_completed_stories_are_never_sent_again():
    provider = FakeProvider(grounded)
    runner, store = make_runner(clusters(FOUR), provider)
    first = runner.run()
    assert first.status == "succeeded" and first.counts["completed"] == 4
    assert len(provider.requests) == 4
    second = runner.run()
    assert second.counts.get("up_to_date") == 4 and "new" not in second.counts
    assert len(provider.requests) == 4  # no duplicate requests
    assert len(runner.list()) == 4


def test_duplicate_clusters_in_the_input_are_analysed_once():
    provider = FakeProvider(grounded)
    cs = clusters([PAPER])
    runner, _ = make_runner(cs + cs, provider)
    report = runner.run()
    assert report.counts["clusters"] == 1 and len(provider.requests) == 1


def test_a_changed_cluster_is_analysed_again():
    provider = FakeProvider(grounded)
    runner, store = make_runner(clusters([PAPER]), provider)
    runner.run()
    extra = staged("Sparse routing halves inference memory in mixture-of-experts models, researchers say",
                   "https://news-a.example.com/2026/11/sparse-routing", PAPER.excerpt, source="news-a",
                   publisher="News A", hours_ago=29)  # fmt: skip
    runner.holder["clusters"] = clusters([PAPER, extra])
    report = runner.run()
    assert report.counts.get("changed", 0) + report.counts.get("new", 0) >= 1
    assert len(provider.requests) >= 2


def test_one_failing_story_does_not_stop_the_others_and_is_retried_later():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("unavailable", "HTTP 503")
        return grounded(request)

    provider = FakeProvider(flaky)
    runner, store = make_runner(clusters(FOUR), provider, settings=ai_settings(ai_max_retries=0, ai_max_concurrency=1))
    report = runner.run()
    assert report.status == "partial"
    assert report.counts["failed"] == 1 and report.counts["completed"] == 3
    failed = [r for r in store.records.values() if r.status == "failed"]
    assert failed[0].errors[0]["code"] == "provider_unavailable" and failed[0].analysis is None
    assert failed[0].next_attempt_at == NOW + timedelta(minutes=30)
    # Not retried before its retry time...
    assert runner.run().counts.get("waiting") == 1
    # ... then retried and completed.
    later, _ = make_runner(clusters(FOUR), provider, store=store, clock=lambda: NOW + timedelta(hours=1))
    assert later.run().counts.get("completed") == 1


def test_quota_exhaustion_stops_the_run_marks_stories_and_alerts():
    calls = {"n": 0}

    def until_quota(request):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise ProviderError("quota", "The Gemini free-tier quota for this model is used up for today.")
        return grounded(request)

    provider = FakeProvider(until_quota)
    ledger = MemoryUsageLedger()
    runner, store = make_runner(clusters(FOUR), provider, ledger=ledger, settings=ai_settings(ai_max_concurrency=1))
    report = runner.run()
    assert report.status == "quota_limited"
    assert report.counts["completed"] == 1 and report.counts["quota_limited"] == 3
    assert len(provider.requests) == 2  # stopped at once, no retries of a daily quota error
    limited = [r for r in store.records.values() if r.status == "quota_limited"]
    assert all(r.attempt_count == 0 and r.next_attempt_at == next_reset(NOW) for r in limited)
    assert runner.alerts and runner.alerts[0]["kind"] == "quota_exhausted"
    assert report.alerts == runner.alerts
    job = next(iter(store.jobs.values()))
    assert job["details"]["outcome"] == "quota_limited" and job["details"]["alerts"]

    # A second run the same day sends nothing at all.
    again = runner.run()
    assert again.status == "quota_limited" and len(provider.requests) == 2
    assert store.skipped

    # After the reset the remaining stories resume automatically.
    resumed_provider = FakeProvider(grounded)
    after = next_reset(NOW) + timedelta(minutes=1)
    later, _ = make_runner(clusters(FOUR), resumed_provider, store=store, ledger=ledger, clock=lambda: after)
    report = later.run()
    assert report.counts.get("resume") == 3 and report.counts.get("completed") == 3


def test_configured_daily_request_limit_marks_the_rest_quota_limited():
    provider = FakeProvider(grounded)
    runner, store = make_runner(
        clusters(FOUR), provider, settings=ai_settings(ai_requests_per_day=2, ai_max_concurrency=1)
    )
    report = runner.run()
    assert report.status == "quota_limited" and len(provider.requests) == 2
    assert report.counts["completed"] == 2 and report.counts["quota_limited"] == 2


def test_quota_never_overwrites_an_existing_analysis():
    provider = FakeProvider(grounded)
    runner, store = make_runner(clusters([PAPER]), provider)
    runner.run()
    before = next(iter(store.records.values()))
    # The engine changes (e.g. a new prompt version) while the quota is used up.
    quota = FakeProvider(ProviderError("quota", "used up"))
    later, _ = make_runner(clusters([PAPER]), quota, store=store, settings=ai_settings(ai_max_project_ideas=2))
    report = later.run()
    assert report.counts.get("deferred_by_quota") == 1
    assert next(iter(store.records.values())).analysis == before.analysis


def test_auth_or_missing_model_stops_the_run_without_touching_stories():
    provider = FakeProvider(ProviderError("model_unavailable", "The configured Gemini model was not found."))
    runner, store = make_runner(clusters(FOUR), provider, settings=ai_settings(ai_max_concurrency=1))
    report = runner.run()
    assert report.status == "failed" and "not found" in report.reason
    assert store.records == {} and len(provider.requests) == 1
    assert runner.alerts[0]["kind"] == "provider_model_unavailable"


def test_missing_api_key_fails_before_any_request():
    provider = FakeProvider()
    runner, store = make_runner(clusters(FOUR), provider, settings=ai_settings(gemini_api_key=None))
    report = runner.run()
    assert report.status == "failed" and "GEMINI_API_KEY" in report.reason
    assert provider.requests == [] and store.jobs == {}


def test_a_second_run_cannot_start_while_one_holds_the_lock():
    runner, store = make_runner(clusters(FOUR), FakeProvider(grounded))
    store.start(NOW)  # another process holds the lock
    report = runner.run()
    assert report.status == "skipped"


def test_run_limit_defers_the_rest():
    provider = FakeProvider(grounded)
    runner, _ = make_runner(clusters(FOUR), provider, settings=ai_settings(ai_max_stories_per_run=2))
    report = runner.run()
    assert report.counts["deferred_by_run_limit"] == 2 and len(provider.requests) == 2
