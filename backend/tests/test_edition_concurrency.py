"""Concurrency against a real PostgreSQL (committed data, cleaned up afterwards): simultaneous scheduler runs,
approvals and publications each take effect exactly once."""

import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select, text

from app.db.session import Database
from app.editions.compose import EditionComposer
from app.editions.service import Actor, EditionService
from app.intelligence.quota import MemoryUsageLedger
from app.models import Edition, EditionAuditEntry, Story
from tests.conftest import make_settings
from tests.edition_helpers import (
    ADMIN,
    MONDAY,
    OTHER,
    WEEK_START,
    ScriptedGemini,
    edition_config,
    seed_admins,
    seed_pipeline,
)
from tests.intelligence_helpers import NoSleep

pytestmark = pytest.mark.usefixtures("engine")
TABLES = (
    "edition_audit_log",
    "edition_items",
    "edition_revisions",
    "stories",
    "editions",
    "sources",
    "editorial_admins",
    "ranking_runs",
    "story_analyses",
    "ingestion_jobs",
    "ai_usage_daily",
)


@pytest.fixture
def committed(engine) -> Iterator[Database]:
    settings = make_settings(gemini_api_key="test-key-not-real", db_statement_timeout_ms=20_000)
    db = Database(settings)
    with db.write_session() as s:
        seed_admins(s)
        seed_pipeline(s)
    yield db
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))  # bypass append-only triggers
        for table in TABLES:
            conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed table names
    db.dispose()


def service(db: Database) -> EditionService:
    config = edition_config()
    provider = ScriptedGemini()

    def factory():
        return EditionComposer(
            db.settings, config, provider, ledger=MemoryUsageLedger(), sleep=NoSleep(), clock=lambda: MONDAY
        )

    return EditionService(db.settings, db, config, composer_factory=factory, clock=lambda: MONDAY)


def run_parallel(fn, n=4):
    results, errors = [], []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results, errors


def test_concurrent_scheduler_runs_create_one_edition(committed):
    results, errors = run_parallel(lambda: service(committed).generate_week(WEEK_START, actor=Actor.scheduler()))
    assert not errors, errors
    assert sorted(r.status for r in results).count("generated") == 1
    with committed.write_session() as s:
        assert s.scalar(select(func.count()).select_from(Edition)) == 1


def test_concurrent_approval_and_publication_happen_once(committed):
    svc = service(committed)
    eid = svc.generate_week(WEEK_START, actor=Actor.scheduler()).edition_id
    d = svc.detail(eid)
    actors = [ADMIN, OTHER, ADMIN, OTHER]
    approvals, errors = run_parallel(
        lambda: service(committed).approve(eid, actors[threading.get_ident() % 4], 1, d["contentHash"])
    )
    assert not errors, errors
    assert sum(1 for r in approvals if not r.get("alreadyApproved")) == 1
    publications, errors = run_parallel(lambda: service(committed).publish(eid, ADMIN, 1))
    assert not errors, errors
    assert sum(1 for r in publications if not r.get("alreadyPublished")) == 1
    with committed.write_session() as s:
        assert s.scalar(select(func.count()).select_from(Story)) == len(d["content"]["stories"])
        audit = s.scalars(select(EditionAuditEntry.action).where(EditionAuditEntry.edition_id == eid)).all()
        assert audit.count("approved") == 1 and audit.count("published") == 1


def test_simultaneous_opens_log_one_review_entry(committed):
    eid = service(committed).generate_week(WEEK_START, actor=Actor.scheduler()).edition_id
    _, errors = run_parallel(lambda: service(committed).detail(eid, actor=ADMIN))
    assert not errors, errors
    with committed.write_session() as s:
        actions = s.scalars(select(EditionAuditEntry.action).where(EditionAuditEntry.edition_id == eid)).all()
        assert actions.count("opened_for_review") == 1
