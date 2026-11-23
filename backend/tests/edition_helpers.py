"""Helpers for Stage 3.5 tests.

A real Stage 3.4 ranking (real Stage 3.2 clusters, Stage 3.3 analyses and Stage 3.4 run made with the scripted
fake Gemini of the earlier stages) is computed once per test session and stored into the rolled-back test
database. Edition composition uses a scripted fake Gemini as well: no test makes a network call or uses quota.
"""

import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from sqlalchemy.orm import Session

from app.db.session import Database
from app.editions.compose import EditionComposer
from app.editions.config import EditionConfig, load_edition_config
from app.editions.service import Actor, EditionService
from app.intelligence.providers.base import GenerationRequest, GenerationResponse, Usage
from app.intelligence.quota import MemoryUsageLedger
from app.intelligence.store import _FIELDS as ANALYSIS_FIELDS
from app.models import EditorialAdmin, RankingRun, StoryAnalysisRecord
from app.ranking.contract import RankingResult
from tests.conftest import make_settings
from tests.intelligence_helpers import FUNDING, INDIA, LAB_POST, NEWS_REPORT, PAPER, POLICY, NoSleep
from tests.ranking_helpers import WEEK, analysed, make_ranker

WEEK_START = date(2026, 11, 2)  # the ranking helpers' week (NOW = Friday 2026-11-06)
WEEK_END_UTC = datetime(2026, 11, 8, 18, 30, tzinfo=UTC)  # Monday 2026-11-09 00:00 Asia/Kolkata
# Monday 2026-11-09 07:00 Asia/Kolkata: the week is over and its edition is due (06:00 local).
MONDAY = datetime(2026, 11, 9, 1, 30, tzinfo=UTC)
ADMIN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
OTHER_ADMIN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
ITEMS = [LAB_POST, NEWS_REPORT, PAPER, INDIA, FUNDING, POLICY]


@lru_cache(maxsize=1)
def pipeline() -> tuple[list[dict], dict]:
    """(story_analyses rows, ranking result JSON), computed once."""
    clusters, records = analysed(ITEMS)
    runner, store = make_ranker(clusters, records)
    report = runner.run(week=WEEK)
    assert report.status == "completed", report.reason
    rows = [{f: getattr(r, f) for f in ANALYSIS_FIELDS} for r in records]
    return rows, report.result.model_dump(mode="json")


def ranking_result() -> RankingResult:
    return RankingResult.model_validate(pipeline()[1])


def edition_config(**sections) -> EditionConfig:
    base = load_edition_config("config/edition.toml").model_dump()
    base["schedule"]["start_week"] = None
    base["schedule"]["catch_up_weeks"] = 0
    for name, values in sections.items():
        base[name].update(values)
    return EditionConfig.model_validate(base)


def seed_pipeline(
    session: Session, *, processed_at: datetime | None = None, result: dict | None = None, status: str = "completed"
) -> RankingRun:
    rows, ranking = pipeline()
    far = datetime(2027, 1, 1, tzinfo=UTC)
    for row in rows:
        session.add(StoryAnalysisRecord(**{**row, "expires_at": far, "job_id": None}))
    data = result or ranking
    run = RankingRun(
        run_key=uuid.uuid4().hex,
        edition_start=WEEK_START,
        edition_end=WEEK_START + timedelta(days=6),
        timezone="Asia/Kolkata",
        status=status,
        ranking_config_version=data["rankingConfigVersion"],
        verification_config_version=data["verificationConfigVersion"],
        processed_at=processed_at or WEEK_END_UTC + timedelta(hours=1),
        total_evaluated=data["totals"]["evaluated"],
        total_eligible=data["totals"]["eligible"],
        shortlist_cluster_ids=[uuid.UUID(c) for c in data["rankedCandidateIds"]],
        result=data,
    )
    session.add(run)
    session.flush()
    return run


def seed_admins(session: Session) -> None:
    session.add(EditorialAdmin(user_id=ADMIN_ID, email="editor@example.com", display_name="Editor"))
    session.add(EditorialAdmin(user_id=OTHER_ADMIN_ID, email="second@example.com", display_name="Second"))
    session.flush()


class SavepointDatabase(Database):
    """Runs every unit of work in a SAVEPOINT of the test's rolled-back transaction."""

    def __init__(self, settings, session: Session):
        super().__init__(settings, engine=session.get_bind().engine)
        self._session = session

    def read_session(self) -> Iterator[Session]:
        self._session.flush()
        yield self._session

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        with self._session.begin_nested():
            yield self._session


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+", text)


def composer_answer(request: GenerationRequest, **changes) -> GenerationResponse:
    """A valid composition: the same stories, lightly reworded, and an introduction from their headlines."""
    user = request.messages[1].content
    data = json.loads(user[user.index("{") : user.rindex("}") + 1])
    stories = [
        {
            "ref": s["ref"],
            "headline": s["headline"],
            "summary": s["summary"],
            "technical_explanation": s["technical_explanation"],
            "why_it_matters": s["why_it_matters"],
            "student_relevance": s["student_relevance"],
        }
        for s in data["stories"]
    ]
    body = {
        "title": "Open models, new research and policy moves",
        "introduction": "This week brings together a model release, new research and policy updates. "
        + " ".join(f"Also covered: {s['headline']}." for s in stories[:2]),
        "closing_note": "",
        "stories": stories,
    }
    for key, value in changes.items():
        body[key] = value(body) if callable(value) else value
    return GenerationResponse(
        content=json.dumps(body), model=request.model, usage=Usage(2000, 800), finish_reason="stop"
    )


class ScriptedGemini:
    name = "gemini"

    def __init__(self, *steps):
        self.steps = list(steps) or [composer_answer]
        self.requests: list[GenerationRequest] = []

    async def generate(self, request):
        self.requests.append(request)
        step = self.steps[min(len(self.requests) - 1, len(self.steps) - 1)]
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step

    async def check_model(self, model):
        return model

    async def aclose(self):
        return None


def make_service(
    session: Session,
    *,
    provider=None,
    config: EditionConfig | None = None,
    clock=lambda: MONDAY,
    ledger=None,
    rank_week=None,
    settings=None,
    **settings_overrides,
) -> EditionService:
    settings = settings or make_settings(gemini_api_key="test-key-not-real", **settings_overrides)
    config = config or edition_config()
    db = SavepointDatabase(settings, session)
    ledger = ledger or MemoryUsageLedger()
    provider = provider if provider is not None else ScriptedGemini()

    def factory():
        if provider is False:
            return None
        return EditionComposer(settings, config, provider, ledger=ledger, sleep=NoSleep(), clock=clock)

    service = EditionService(settings, db, config, composer_factory=factory, rank_week=rank_week, clock=clock)
    service.provider = provider
    return service


ADMIN = Actor("admin", ADMIN_ID)
OTHER = Actor("admin", OTHER_ADMIN_ID)
