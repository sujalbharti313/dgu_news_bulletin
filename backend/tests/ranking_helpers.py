"""Helpers for Stage 3.4 tests: real Stage 3.2 clusters, real Stage 3.3 analyses (made by the Stage 3.3 engine with
a scripted fake Gemini), and a ranking runner wired to in-memory stores and a fake provider for the AI check.

No test makes a network call, needs an API key or uses quota. All articles are fictional test data on example
domains (see tests/intelligence_helpers.py)."""

import json
from collections.abc import Callable
from typing import Any

from app.intelligence.contract import StoryAnalysis
from app.intelligence.providers.base import GenerationRequest, GenerationResponse
from app.intelligence.quota import MemoryUsageLedger
from app.intelligence.store import AnalysisRecord
from app.ranking.config import load_ranking_config, load_source_registry
from app.ranking.runner import RankingRunner
from app.ranking.store import MemoryRankingStore
from tests.intelligence_helpers import FakeProvider, NoSleep, ai_settings, grounded, make_runner, response
from tests.intelligence_helpers import clusters as build_clusters
from tests.processing_helpers import NOW

CONFIG = load_ranking_config("config/ranking.toml")
REGISTRY = load_source_registry("config/source_registry.toml")
# NOW (2026-11-06, a Friday) falls in the edition week Monday 2026-11-02 to Sunday 2026-11-08 (Asia/Kolkata).
WEEK = "current"


def ranking_settings(**overrides):
    values = {"ranking_ai_check": False, "ranking_ai_max_checks_per_run": 20}
    values.update(overrides)
    return ai_settings(**values)


def title_responder(changes_by_title: dict[str, dict[str, Any]]) -> Callable[[GenerationRequest], GenerationResponse]:
    """A Stage 3.3 fake that answers `grounded`, with per-story changes chosen by a word in the first source title."""

    def respond(request: GenerationRequest) -> GenerationResponse:
        user = request.messages[1].content
        for needle, changes in changes_by_title.items():
            if needle in user:
                return grounded(request, **changes)
        return grounded(request)

    return respond


def analysed(items, responder=None, **settings) -> tuple[list, list[AnalysisRecord]]:
    """Real clusters and real Stage 3.3 analysis records for the given staged items."""
    clusters = build_clusters(items)
    provider = FakeProvider(responder or grounded)
    runner, store = make_runner(clusters, provider, settings=ai_settings(ai_max_stories_per_run=50, **settings))
    report = runner.run()
    assert report.status in ("succeeded", "partial"), report.as_dict()
    return clusters, list(store.records.values())


def analysis_of(records: list[AnalysisRecord], cluster) -> StoryAnalysis:
    rec = next(r for r in records if r.cluster_id == cluster.cluster_id)
    return StoryAnalysis.model_validate(rec.analysis)


def check_response(**overrides) -> dict:
    """A valid AI consistency check answer that finds no problems (claims 0 and 1 supported by S1)."""
    body = {
        "headline": {"verdict": "supported", "explanation": "S1 states this."},
        "summary": {"verdict": "supported", "explanation": "S1 states this."},
        "claims": [{"index": 0, "verdict": "supported", "source_ids": ["S1"], "explanation": "S1 states it."}],
        "source_conflicts": [],
        "content_nature": "factual_report",
        "hype_signals": [],
        "instruction_like_text_found": False,
    }
    body.update(overrides)
    return body


def check_reply(**overrides) -> GenerationResponse:
    return response(check_response(**overrides), tokens=(800, 150))


def make_ranker(clusters, records, *, provider=None, store=None, settings=None, ledger=None, clock=lambda: NOW, **kw):
    store = store or MemoryRankingStore()
    alerts: list[dict] = []
    holder = {"clusters": clusters, "records": records}
    runner = RankingRunner(
        settings or ranking_settings(),
        store,
        lambda: holder["clusters"],
        lambda: holder["records"],
        ledger=ledger or MemoryUsageLedger(),
        provider_factory=lambda s: provider or FakeProvider(check_reply()),
        config=kw.pop("config", CONFIG),
        registry=kw.pop("registry", REGISTRY),
        clock=clock,
        sleep=NoSleep(),
        alert_hook=alerts.append,
        **kw,
    )
    runner.holder = holder
    runner.alerts = alerts
    return runner, store


def check_request_data(request: GenerationRequest) -> dict:
    user = request.messages[1].content
    start = user.rindex("<<DATA ")
    return json.loads(user[user.index("\n", start) + 1 : user.rindex("\n<<END DATA ")])
