"""Runs over many clusters: idempotency, per-story status, retrying failures without repeating successes,
fatal provider errors, run and cost limits, usage hooks, Stage 3.2 integration and the Stage 3.4 contract."""

import json
from datetime import timedelta

from app.ingestion.store import MemoryIngestionStore
from app.intelligence.contract import StoryIntelligence
from app.intelligence.providers.base import ProviderError
from app.intelligence.runner import IntelligenceRunner, needs_work
from app.intelligence.store import MemoryIntelligenceStore
from app.processing.runner import ProcessingRunner
from app.processing.store import MemoryProcessingStore
from tests.intelligence_helpers import (
    CATEGORIES,
    LAB_POST,
    NEWS_REPORT,
    SYNDICATED_COPY,
    THIN_ITEM,
    FakeProvider,
    ai_settings,
    clusters,
    grounded,
    make_runner,
)
from tests.processing_helpers import NOW, run, staged, staged_from_memory
from tests.test_processing_integration import INFO, ingest

OTHER = staged(
    "Example Labs publishes Nimbus 2.5 safety evaluations",
    "https://labs.example.com/blog/nimbus-safety",
    "Example Labs published safety evaluations for Nimbus 2.5 covering misuse, cyber capabilities and "
    "citation accuracy. The report says external red-teamers tested the model before release and lists open "
    "issues the company plans to address.",
    source="lab-blog",
    publisher="Example Labs Blog",
    hours_ago=150,
)


def two_clusters():
    out = clusters([LAB_POST, NEWS_REPORT, SYNDICATED_COPY, OTHER])
    assert len(out) == 2
    return out


def test_runs_are_idempotent_and_never_repeat_successful_work():
    runner, store = make_runner(two_clusters(), provider := FakeProvider(grounded))
    first = runner.run()
    assert first.status == "succeeded" and first.counts["new"] == 2 and len(provider.requests) == 2
    assert {r.status for r in store.records.values()} <= {"analysed", "needs_review", "invalid_output"}
    second = runner.run()
    assert second.counts.get("up_to_date") == 2 and len(provider.requests) == 2  # no new calls
    assert first.usage["calls"] == 2 and second.usage["calls"] == 0


def test_a_changed_cluster_revision_is_analysed_again():
    runner, store = make_runner(clusters(), provider := FakeProvider(grounded))
    runner.run()
    # A new source joins the story in Stage 3.2: its revision changes.
    runner.holder["clusters"] = clusters(
        [
            LAB_POST,
            NEWS_REPORT,
            SYNDICATED_COPY,
            staged(
                "Example Labs ships Nimbus 2.5 to developers",
                "https://news-b.example.com/nimbus-devs",
                "Example Labs released Nimbus 2.5 to developers through its API with a 400K-token context window, "
                "and the company reports 71.2% on SWE-bench Verified.",
                source="news-b",
                publisher="News B",
            ),
        ]
    )
    report = runner.run()
    assert report.counts["changed"] == 1 and len(provider.requests) == 2
    (record,) = store.records.values()
    assert record.cluster_revision == runner.holder["clusters"][0].processing.revision and record.attempt_count == 1


def test_failed_stories_are_retried_later_without_redoing_successes():
    a, b = sorted(two_clusters(), key=lambda c: "safety" in c.title)  # b = the safety story
    timeline = {"now": NOW}
    calls = []

    def flaky(request):
        calls.append(request)
        if "safety evaluations" in request.messages[1].content and len(calls) < 4:
            raise ProviderError("unavailable", "503")
        return grounded(request)

    provider = FakeProvider(flaky)
    runner, store = make_runner(
        [a, b], provider, settings=ai_settings(ai_max_retries=0, ai_max_concurrency=1), clock=lambda: timeline["now"]
    )
    first = runner.run()
    assert first.status == "partial" and first.counts["failed"] == 1
    failed = store.records[b.cluster_id]
    assert failed.status == "failed" and failed.attempt_count == 1 and failed.intelligence is None
    assert failed.next_attempt_at == NOW + timedelta(minutes=30)
    assert failed.errors[0]["code"] == "provider_unavailable"
    # Too early: nothing is called.
    assert runner.run().counts["waiting"] == 1 and len(calls) == 2
    # After the retry delay only the failed story is called again (and fails a second time)...
    timeline["now"] = NOW + timedelta(minutes=31)
    assert runner.run().counts["retry"] == 1 and len(calls) == 3
    assert store.records[b.cluster_id].attempt_count == 2
    # ... and --retry-failed retries it immediately; it now succeeds. The analysed story is never touched.
    report = runner.run(retry_failed=True)
    assert report.counts["retry"] == 1 and report.counts["up_to_date"] == 1 and len(calls) == 4
    assert store.records[b.cluster_id].status in ("analysed", "needs_review")
    assert store.records[a.cluster_id].attempt_count == 1


def test_attempts_are_bounded_per_story():
    runner, store = make_runner(
        clusters(),
        FakeProvider(ProviderError("unavailable", "503")),
        settings=ai_settings(ai_max_retries=0, ai_max_attempts_per_story=2, ai_retry_delay_minutes=0),
    )
    runner.run()
    runner.run()
    assert runner.run().counts["exhausted"] == 1
    assert next(iter(store.records.values())).attempt_count == 2


def test_fatal_provider_errors_stop_the_run_and_keep_story_state():
    runner, store = make_runner(
        two_clusters(),
        provider := FakeProvider(ProviderError("auth", "bad key")),
        settings=ai_settings(ai_max_concurrency=1),
    )
    report = runner.run()
    assert report.status == "failed" and report.reason == "bad key"
    assert len(provider.requests) == 1 and store.records == {}  # nothing marked failed for a config problem
    assert report.counts["not_started"] == 1
    job = next(iter(store.jobs.values()))
    assert job["status"] == "failed" and provider.closed


def test_insufficient_evidence_is_recorded_once_without_calls():
    runner, store = make_runner(clusters([THIN_ITEM]), provider := FakeProvider())
    runner.run()
    runner.run()
    (record,) = store.records.values()
    assert record.status == "insufficient_evidence" and record.intelligence is None and provider.requests == []


def test_run_and_cost_limits_and_usage_hook():
    events = []
    priced = ai_settings(
        ai_max_stories_per_run=1, ai_input_cost_per_million_tokens=1.0, ai_output_cost_per_million_tokens=1.0
    )
    runner, store = make_runner(two_clusters(), FakeProvider(grounded), settings=priced, usage_hook=events.append)
    report = runner.run()
    assert report.counts["deferred_by_run_limit"] == 1 and len(store.records) == 1
    assert events and events[0]["input_tokens"] == 1000 and events[0]["estimated_cost_usd"] == 0.0014
    assert report.usage["estimated_cost_usd"] == 0.0014 and report.usage["model"] == "test-model"

    capped = ai_settings(
        ai_max_concurrency=1,
        ai_max_cost_per_run_usd=0.001,
        ai_input_cost_per_million_tokens=1.0,
        ai_output_cost_per_million_tokens=1.0,
    )
    runner, store = make_runner(two_clusters(), FakeProvider(grounded), settings=capped)
    report = runner.run()
    assert report.counts["deferred_by_cost_limit"] == 1 and len(store.records) == 1


def test_a_broken_usage_hook_never_breaks_a_run():
    def hook(event):
        raise RuntimeError("monitoring down")

    runner, _ = make_runner(clusters(), FakeProvider(grounded), usage_hook=hook)
    assert runner.run().status == "succeeded"


def test_missing_configuration_fails_before_any_job_or_call():
    for settings in (ai_settings(ai_model=None), ai_settings(openai_api_key=None)):
        runner, store = make_runner(clusters(), provider := FakeProvider(), settings=settings)
        report = runner.run()
        assert report.status == "failed" and report.job_id is None and store.jobs == {} and not provider.requests


def test_a_concurrent_run_is_skipped():
    runner, store = make_runner(clusters(), FakeProvider())
    store.start(NOW)  # another process holds the lock
    assert runner.run().status == "skipped"


def test_needs_work_decisions():
    (cluster,) = clusters()
    runner, store = make_runner([cluster], FakeProvider(grounded))
    runner.run()
    state = store.records[cluster.cluster_id]
    common = dict(now=NOW, max_attempts=3, retry_failed=False)
    assert needs_work(cluster, None, key=state.engine_key, **common) == "new"
    assert needs_work(cluster, state, key=state.engine_key, **common) == "up_to_date"
    assert needs_work(cluster, state, key="prompt-9.9.9", **common) == "changed"  # prompt/category change


def test_stage_31_to_32_to_33_in_memory_and_stage_34_contract():
    """Stage 3.1 store → Stage 3.2 runner → Stage 3.3 runner → the list Stage 3.4 reads."""
    ingestion = MemoryIngestionStore()
    ingest(ingestion)  # mocked feeds through the real Stage 3.1 runner
    processing = ProcessingRunner(
        ai_settings(), MemoryProcessingStore(staged_from_memory(ingestion)), sources=INFO, clock=lambda: NOW
    )
    assert processing.run().status == "succeeded"
    stage32 = processing.clusters()
    assert len(stage32) == 3

    store = MemoryIntelligenceStore()
    runner = IntelligenceRunner(
        ai_settings(ai_min_evidence_words=0),
        store,
        processing.clusters,
        provider_factory=lambda s: FakeProvider(grounded),
        categories=CATEGORIES,
        clock=lambda: NOW,
    )
    report = runner.run()
    assert report.status == "succeeded" and report.counts["new"] == 3, report.as_dict()
    assert report.counts.get("analysed", 0) + report.counts.get("needs_review", 0) == 3

    for_34 = runner.list()
    assert len(for_34) == 3
    by_cluster = {c.cluster_id: c for c in stage32}
    for intel in for_34:
        source_cluster = by_cluster[intel.cluster_id]  # cluster id preserved
        assert [s.item_id for s in intel.sources] == [r.item_id for r in source_cluster.sources]
        assert intel.cluster_revision == source_cluster.processing.revision
        # The contract round-trips through JSON (what a later stage or the admin API sees).
        dumped = intel.model_dump(mode="json", by_alias=True)
        assert StoryIntelligence.model_validate(dumped) == intel
        assert dumped["contractVersion"] == "1.0" and dumped["contentIsAiGenerated"] is True
        assert "excerpt" not in json.dumps(dumped["sources"])  # metadata only, no article text stored
    assert {i.status for i in runner.list(statuses=["analysed"])} <= {"analysed"}
    assert runner.run().counts["up_to_date"] == 3  # Stage 3.2 unchanged → no new calls


def test_stage_34_contract_shape_is_stable():
    schema = StoryIntelligence.model_json_schema(by_alias=True)
    required = set(schema["required"])
    assert {
        "clusterId",
        "clusterRevision",
        "status",
        "headline",
        "classification",
        "evidence",
        "sources",
        "generation",
        "whyItMatters",
        "studentRelevance",
        "projectIdeas",
        "technicalDetails",
    } <= required
    assert set(schema["properties"]["status"]["enum"]) == {"analysed", "needs_review"}


def test_dry_runs_store_nothing_in_the_database_layer():
    _, _, cl = run([LAB_POST, NEWS_REPORT])
    runner, store = make_runner(cl, FakeProvider(grounded), dry_run=True)
    runner.run()
    assert all(r.job_id is None for r in store.records.values())


def test_cli_check_reports_configuration_without_calls(monkeypatch, capsys):
    from scripts import intelligence as cli

    monkeypatch.setattr(cli, "Settings", lambda: ai_settings())
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "Model: test-model" in out and "API key: configured" in out and "sk-test" not in out
    monkeypatch.setattr(cli, "Settings", lambda: ai_settings(ai_model=None))
    assert cli.main(["check"]) == 2
    assert cli.main(["run"]) == 2  # no DATABASE_URL: refuses before any call
