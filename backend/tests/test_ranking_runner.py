"""Stage 3.4 runner: totals, incomplete runs, stale analyses, per-story failure isolation, idempotency and the
contract Stage 3.5 reads. In-memory stores; no network."""

import uuid
from datetime import timedelta

from app.ranking import runner as runner_module
from app.ranking.contract import RankingResult
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
from tests.processing_helpers import NOW, cluster_of
from tests.ranking_helpers import WEEK, analysed, make_ranker

ALL = [
    LAB_POST,
    NEWS_REPORT,
    SYNDICATED_COPY,
    PAPER,
    INDIA,
    FUNDING,
    POLICY,
    HYPE,
    THIN_ITEM,
    SPARSE_ITEM,
    INJECTION_ITEM,
]


def test_full_week_ranking_totals_and_contract():
    clusters, records = analysed(ALL)
    runner, store = make_ranker(clusters, records)
    report = runner.run(week=WEEK)
    assert report.status == "completed", report.reason
    r = report.result
    t = r.totals
    assert t.clusters_in_window == len(clusters)
    assert t.evaluated == t.eligible + t.review_required + t.ineligible
    assert t.shortlisted == len(r.shortlist) == len(r.ranked_candidate_ids) <= 10
    assert r.edition["startDate"] == "2026-11-02" and r.edition["timezone"] == "Asia/Kolkata"
    assert r.ranking_config_version.startswith("3.4.1+") and r.verification_config_version
    assert r.externally_fact_checked is False
    # The stored JSON is the same contract, and validates.
    stored = next(iter(store.runs.values()))
    assert RankingResult.model_validate(stored.result).ranked_candidate_ids == r.ranked_candidate_ids
    assert stored.shortlist_cluster_ids == r.ranked_candidate_ids
    for c in r.candidates:
        rec = store.verifications[c.story_cluster_id]
        assert rec.final_priority_score == c.final_scores.final_priority_score and rec.rank == c.rank
        assert rec.ranking_explanation == c.ranking_explanation and rec.eligibility == c.eligibility_status
        assert rec.ranking_run_id == report.result.run_id


def test_stories_outside_the_collection_week_are_not_evaluated():
    clusters, records = analysed([PAPER])
    runner, _ = make_ranker(clusters, records)
    report = runner.run(week="2026-10-20")  # nothing was collected that week
    assert report.result.totals.clusters_in_window == 0 and report.result.shortlist == []
    assert any("no stories" in n for n in report.result.notes)


def test_missing_and_stale_analyses_make_the_run_incomplete():
    clusters, records = analysed([PAPER, INDIA, POLICY])
    paper, india = cluster_of(clusters, PAPER), cluster_of(clusters, INDIA)
    records = [r for r in records if r.cluster_id != paper.cluster_id]  # not analysed yet
    for r in records:
        if r.cluster_id == india.cluster_id:
            r.cluster_revision = "older-revision"
    runner, _ = make_ranker(clusters, records)
    report = runner.run(week=WEEK)
    assert report.status == "incomplete"
    reasons = {p.story_cluster_id: p.reason for p in report.result.pending}
    assert reasons == {paper.cluster_id: "not_analysed", india.cluster_id: "analysis_out_of_date"}
    assert report.result.status == "incomplete" and report.result.status_reason.startswith("Not a final ranking")


def test_quota_limited_and_failed_analyses():
    clusters, records = analysed([PAPER, INDIA])
    paper, india = cluster_of(clusters, PAPER), cluster_of(clusters, INDIA)
    for r in records:
        if r.cluster_id == paper.cluster_id:
            r.status, r.analysis = "quota_limited", None
        if r.cluster_id == india.cluster_id:
            r.status, r.analysis, r.attempt_count = "failed", None, 5
    runner, _ = make_ranker(clusters, records)
    result = runner.run(week=WEEK).result
    assert [p.reason for p in result.pending] == ["analysis_quota_limited"]
    assert [f.story_cluster_id for f in result.failed] == [india.cluster_id]  # exhausted: reported, not pending


def test_one_failing_story_does_not_stop_the_run(monkeypatch):
    clusters, records = analysed([PAPER, INDIA, POLICY])
    bad = cluster_of(clusters, INDIA).cluster_id
    real = runner_module.evaluate_story

    def flaky(cluster, *args, **kwargs):
        if cluster.cluster_id == bad:
            raise RuntimeError("boom")
        return real(cluster, *args, **kwargs)

    monkeypatch.setattr(runner_module, "evaluate_story", flaky)
    runner, store = make_ranker(clusters, records)
    report = runner.run(week=WEEK)
    assert report.status == "completed"
    assert [f.story_cluster_id for f in report.result.failed] == [bad]
    assert "boom" not in report.result.failed[0].error  # error type only, no content
    assert len(report.result.candidates) == 2


def test_a_failed_save_is_isolated():
    clusters, records = analysed([PAPER, INDIA])
    runner, store = make_ranker(clusters, records)
    bad = cluster_of(clusters, PAPER).cluster_id
    store.fail_save_for = {bad}
    report = runner.run(week=WEEK)
    assert [f.story_cluster_id for f in report.result.failed] == [bad]
    assert cluster_of(clusters, INDIA).cluster_id in store.verifications


def test_rerun_on_unchanged_inputs_is_idempotent():
    clusters, records = analysed([PAPER, INDIA, POLICY])
    runner, store = make_ranker(clusters, records)
    a = runner.run(week=WEEK)
    runner.clock = lambda: NOW + timedelta(minutes=10)
    b = runner.run(week=WEEK)
    assert b.reused_run and a.run_id == b.run_id and len(store.runs) == 1
    assert a.result.ranked_candidate_ids == b.result.ranked_candidate_ids


def test_changed_inputs_create_a_new_run():
    clusters, records = analysed([PAPER, INDIA])
    runner, store = make_ranker(clusters, records)
    runner.run(week=WEEK)
    for r in records:
        r.analysis = {**r.analysis, "generation": {**r.analysis["generation"], "generatedAt": "2026-11-06T13:00:00Z"}}
    runner.clock = lambda: NOW + timedelta(hours=2)
    second = runner.run(week=WEEK)
    assert not second.reused_run and len(store.runs) == 2


def test_latest_prefers_completed_runs_for_stage_35():
    clusters, records = analysed([PAPER])
    runner, store = make_ranker(clusters, records)
    done = runner.run(week=WEEK)
    runner.holder["records"] = []  # now the story looks un-analysed: an incomplete run
    runner.clock = lambda: NOW + timedelta(hours=1)
    assert runner.run(week=WEEK).status == "incomplete"
    assert runner.latest(include_incomplete=False).run_id == done.result.run_id
    assert runner.latest(include_incomplete=True).status == "incomplete"


def test_invalid_stored_analysis_is_reported_not_fatal():
    clusters, records = analysed([PAPER, INDIA])
    bad = records[0]
    bad.analysis = {"not": "an analysis"}
    runner, _ = make_ranker(clusters, records)
    report = runner.run(week=WEEK)
    assert [f.story_cluster_id for f in report.result.failed] == [bad.cluster_id]
    assert report.result.candidates


def test_bad_configuration_fails_cleanly(tmp_path):
    clusters, records = analysed([PAPER])
    runner, store = make_ranker(clusters, records)
    runner._config = None
    runner.settings = runner.settings.model_copy(update={"ranking_config_file": str(tmp_path / "missing.toml")})
    report = runner.run(week=WEEK)
    assert report.status == "failed" and "not found" in report.reason and store.runs == {}


def test_explanations_are_grounded_in_scores_and_gates():
    clusters, records = analysed(ALL)
    runner, _ = make_ranker(clusters, records)
    for c in runner.run(week=WEEK).result.candidates:
        assert str(c.final_scores.final_priority_score) in c.ranking_explanation
        for gate in c.gates:
            if gate.action != "eligible":
                assert gate.reason[:40] in c.ranking_explanation
        if c.shortlisted:
            assert f"Shortlist position {c.shortlist_rank}" in c.ranking_explanation


def test_unknown_cluster_ids_never_appear():
    clusters, records = analysed([PAPER])
    runner, _ = make_ranker(clusters, records)
    ids = {c.story_cluster_id for c in runner.run(week=WEEK).result.candidates}
    assert ids == {c.cluster_id for c in clusters} and all(isinstance(i, uuid.UUID) for i in ids)
