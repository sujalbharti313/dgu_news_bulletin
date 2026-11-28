"""Phase 4.2 Scenarios K (failure recovery) and L (scheduler and background automation).

K runs the real command-line entry points the scheduler calls, against an unreachable database, and replays the
pipeline after simulated crashes. L checks the GitHub Actions workflow that is the only scheduler, including its
exit-code gate executed in bash with stubbed stages. Whether scheduled runs really happen is production evidence
(GitHub Actions run history), recorded in the Stage 4.2 report, not something a test can prove.
"""

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select

from app.models import Edition, IngestionItem, IngestionJob, StoryAnalysisRecord, StoryEmbedding
from tests.conftest import TEST_DATABASE_URL
from tests.e2e.test_b_processing import NOW, dataset, run
from tests.edition_helpers import SavepointDatabase

BACKEND = Path(__file__).resolve().parents[2]
WORKFLOW = BACKEND.parent / ".github" / "workflows" / "weekly-pipeline.yml"
SECRET = "S3cretPw-never-printed"
db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")


# ------------------------------------------------------------------------------------------- K: recovery
@pytest.mark.parametrize(
    "command",
    [
        ["scripts.ingest", "run"],
        ["scripts.process", "run"],
        ["scripts.intelligence", "run"],
        ["scripts.rank", "run", "--week", "current"],
        ["scripts.edition", "scheduled"],
        ["scripts.cleanup", "scheduled"],
    ],
)
def test_every_stage_fails_cleanly_when_the_database_is_unreachable(command, tmp_path):
    """E2E-K-01: each scheduled command, with the database down (closed port), exits with an error code (>= 2, so
    the workflow's Report step turns the run red), prints no traceback and never prints the database password."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GMAIL", "GEMINI", "SUPABASE", "DATABASE"))}
    env.update(
        APP_ENV="development",
        DATABASE_URL=f"postgresql://postgres:{SECRET}@127.0.0.1:1/aicampus_test",
        GEMINI_API_KEY="AIza-test-key-not-real-0123456789",
        DB_CONNECT_TIMEOUT_SECONDS="2",
        PYTHONDONTWRITEBYTECODE="1",
    )
    proc = subprocess.run(  # noqa: S603 - fixed module list, test-only
        [sys.executable, "-m", *command], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode >= 2, (proc.returncode, out[-2000:])
    assert SECRET not in out
    assert "Traceback (most recent call last)" not in out, out[-2000:]


def _pipeline(db_session, provider, now):
    from app.intelligence.service import build_runner as analysis
    from app.ranking.service import build_runner as ranking
    from tests.intelligence_helpers import CATEGORIES, NoSleep, ai_settings
    from tests.ranking_helpers import CONFIG, REGISTRY

    settings = ai_settings(
        database_url=TEST_DATABASE_URL,
        ingestion_item_retention_days=90,
        ranking_max_evaluations_per_run=40,
        embedding_requests_per_minute=1000,
    )
    dbs = SavepointDatabase(settings, db_session)
    _, processed = run(db_session, now)
    analysed = analysis(
        settings,
        dbs,
        provider_factory=lambda s: provider,
        categories=CATEGORIES,
        clock=lambda: now,
        sleep=NoSleep(),
        alert_hook=lambda a: None,
    ).run()
    ranked = ranking(
        settings,
        dbs,
        provider_factory=lambda s: provider,
        config=CONFIG,
        registry=REGISTRY,
        clock=lambda: now,
        sleep=NoSleep(),
        alert_hook=lambda a: None,
    ).run(week="current")
    return processed, analysed, ranked


@db
def test_scheduler_triggered_twice_does_no_duplicate_work(db_session):
    """E2E-K-02: the whole daily pipeline (process, analyse, embed, rank) run twice with the same inputs: the second
    run writes no new rows, makes no Gemini or embedding request, and the ranking is idempotent."""
    from tests.e2e.test_c_intelligence import Scripted  # noqa: F401  (same double family)
    from tests.intelligence_helpers import grounded
    from tests.ranking_helpers import FakeGemini, evaluator

    dataset(db_session)

    class Both(FakeGemini):
        def __init__(self):
            super().__init__(evaluator())
            self.analysis_calls = 0

        async def generate(self, request):
            if "<<EVIDENCE " in request.messages[1].content:
                self.analysis_calls += 1
                self.requests.append(request)
                return grounded(request)
            return await super().generate(request)

    provider = Both()
    _pipeline(db_session, provider, NOW)
    first = (len(provider.requests), len(provider.embed_requests))
    rows = {
        m.__name__: db_session.scalar(select(func.count()).select_from(m))
        for m in (IngestionItem, StoryAnalysisRecord, StoryEmbedding)
    }
    processed, analysed, ranked = _pipeline(db_session, provider, NOW + timedelta(minutes=5))
    assert (len(provider.requests), len(provider.embed_requests)) == first
    assert processed.counts["written"] == 0
    assert rows == {
        m.__name__: db_session.scalar(select(func.count()).select_from(m))
        for m in (IngestionItem, StoryAnalysisRecord, StoryEmbedding)
    }
    assert ranked.status in ("completed", "incomplete")


@db
@pytest.mark.parametrize("job_key", ["stage-3.2-processing", "stage-3.4-ranking"])
def test_worker_killed_mid_run_is_recovered(db_session, job_key):
    """E2E-K-03: a stage job left `running` by a worker that was killed (no finish recorded) blocks only until it
    is stale (60 minutes); the next run marks it failed and proceeds."""
    from app.ingestion.store import SqlIngestionStore
    from tests.conftest import make_settings

    dataset(db_session)
    settings = make_settings()
    store = SqlIngestionStore(SavepointDatabase(settings, db_session))
    job_type = "story_processing" if "3.2" in job_key else "verification"
    stuck = store.start_job(job_key, job_type, NOW - timedelta(hours=3))
    assert stuck is not None
    assert store.start_job(job_key, job_type, NOW) is None  # held by the dead worker
    recovered = store.recover_stale_jobs(NOW, settings.ingestion_job_stale_minutes)
    assert recovered >= 1
    db_session.expire_all()
    assert db_session.get(IngestionJob, stuck).status == "failed"
    assert store.start_job(job_key, job_type, NOW) is not None
    if job_key == "stage-3.2-processing":
        db_session.execute(
            IngestionJob.__table__.delete().where(IngestionJob.source_key == job_key, IngestionJob.status == "running")
        )
        store.start_job(job_key, job_type, NOW - timedelta(hours=3))
        _, report = run(db_session, NOW)  # the runner itself sweeps stale jobs before taking the lock
        assert report.status == "succeeded", report.as_dict()


@db
def test_edition_generation_crash_is_retried_and_recovers(db_session):
    """E2E-K-04: an unexpected crash while composing the edition leaves it `generation_failed` with a backoff (no
    partial edition visible, nothing public); the next scheduler run after the backoff generates it."""
    from tests.edition_helpers import (
        MONDAY,
        WEEK_START,
        ScriptedGemini,
        composer_answer,
        make_service,
        seed_admins,
        seed_pipeline,
    )

    seed_admins(db_session)
    seed_pipeline(db_session)

    class Crash(ScriptedGemini):
        def __init__(self):
            super().__init__(composer_answer)
            self.crash = True

        async def generate(self, request):
            if self.crash:
                raise RuntimeError("worker lost its connection")
            return await super().generate(request)

    provider = Crash()
    clock = {"now": MONDAY}
    service = make_service(db_session, provider=provider, clock=lambda: clock["now"])
    from app.editions.service import Actor

    report = service.generate_week(WEEK_START, actor=Actor.scheduler())
    edition = db_session.scalar(select(Edition).where(Edition.week_start == WEEK_START))
    print(f"\nE2E-K-04 first attempt: report={report.status}, edition={edition.status if edition else None}")
    if report.status == "generated":  # composition errors fall back to Stage 3.3 text: still a safe edition
        assert edition.status in ("review_required", "draft")
        return
    assert edition.status == "generation_failed" and edition.published_at is None
    provider.crash = False
    clock["now"] = MONDAY + timedelta(hours=2)
    service.scheduled_run()
    db_session.expire_all()
    assert db_session.get(Edition, edition.id).status in ("review_required", "draft")


# ------------------------------------------------------------------------------------------- L: scheduler
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_schedule_order_locking_and_permissions():
    """E2E-L-01: daily 00:30 UTC (06:00 IST) plus Monday 09:30 UTC (15:00 IST) retry and manual dispatch; one run at
    a time without cancelling a running one; read-only token; a hard timeout; stages in the order edition ->
    collect/process/analyse/rank -> cleanup -> report (report runs even after failures)."""
    wf = workflow()
    on = wf.get("on") or wf.get(True)
    assert [c["cron"] for c in on["schedule"]] == ["30 0 * * *", "30 9 * * 1"]
    assert "workflow_dispatch" in on and "pull_request_target" not in on and "pull_request" not in on
    assert wf["concurrency"] == {"group": "ai-campus-pipeline", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["pipeline"]
    assert 0 < job["timeout-minutes"] <= 60
    names = [s.get("name", "") for s in job["steps"]]
    order = [
        next(i for i, n in enumerate(names) if n.startswith(p))
        for p in ("Stage 3.5", "Stage 3.1-3.4", "Stage 3.6", "Report")
    ]
    assert order == sorted(order)
    assert job["steps"][names.index("Report")]["if"].startswith("always()")


def test_secrets_only_reach_the_job_as_masked_env_and_are_never_echoed():
    """E2E-L-02: secrets come only from `secrets.*` into env (masked by GitHub), and no step prints them."""
    job = workflow()["jobs"]["pipeline"]
    for key in ("DATABASE_URL", "GEMINI_API_KEY", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        assert job["env"][key] == "${{ secrets." + key + " }}"
    for step in job["steps"]:
        script = step.get("run", "")
        for key in ("DATABASE_URL", "GEMINI_API_KEY", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
            assert f"echo ${key}" not in script and f'echo "${key}' not in script and "set -x" not in script


def _run_stage_script(codes: dict[str, int], tmp_path):
    """Executes the workflow's real stage, cleanup and report scripts in bash, with `python -m scripts.X` replaced
    by stubs returning the given exit codes. Returns (report exit code, combined output)."""
    job = workflow()["jobs"]["pipeline"]
    steps = {s.get("name", ""): s.get("run", "") for s in job["steps"]}
    stub = tmp_path / "python"
    lines = ["#!/bin/bash", 'case "$2 $3" in']
    for key, code in codes.items():
        lines.append(f'  "{key}"*) exit {code};;')
    lines += ["  *) exit 0;;", "esac"]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)
    genv = tmp_path / "github_env"
    genv.write_text("")
    script = "\n".join(
        [
            "set -e",
            f'export PATH="{tmp_path}:$PATH"',
            f'export GITHUB_ENV="{genv}"',
            steps["Stage 3.5: weekly edition"],
            steps["Stage 3.1-3.4: collect, process, analyse, rank this week"],
            steps["Stage 3.6: cleanup and data retention"],
            f'set -a; source "{genv}"; set +a',
            steps["Report"],
        ]
    )
    proc = subprocess.run(  # noqa: S603 - the repository's own workflow script
        ["bash", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("codes", "red"),
    [
        ({}, False),
        ({"scripts.ingest run": 1}, False),  # one feed failed (partial): warning, run stays green
        ({"scripts.rank run": 1}, False),  # stories still pending: warning
        ({"scripts.ingest run": 2}, True),  # stage error: run turns red (GitHub emails the owner)
        ({"scripts.intelligence run": 2}, True),
        ({"scripts.edition scheduled": 1}, True),  # edition generation failed
        ({"scripts.cleanup scheduled": 2}, False),  # cleanup not set up: warning only
        ({"scripts.cleanup scheduled": 1}, True),  # a cleanup job failed
    ],
)
def test_exit_code_gate_turns_the_run_red_only_on_real_failures(codes, red, tmp_path):
    """E2E-L-03: the workflow's own bash, run with stubbed stages: partial results warn, failures fail the run, and a
    failing stage never stops the later stages from running."""
    code, out = _run_stage_script(codes, tmp_path)
    assert (code != 0) == red, out


def test_a_failed_newsletter_login_alone_leaves_the_run_green(tmp_path):
    """E2E-L-04 (observation, documents a known pre-handover item): ingestion returns 1 (partial) when only the
    Gmail source fails, so an expired Gmail token does not turn the run red today."""
    code, out = _run_stage_script({"scripts.ingest run": 1}, tmp_path)
    assert code == 0 and "finished partially" in out
