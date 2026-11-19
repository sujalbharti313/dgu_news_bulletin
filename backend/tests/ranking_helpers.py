"""Helpers for Stage 3.4 tests: real Stage 3.2 clusters, real Stage 3.3 analyses (made by the Stage 3.3 engine with
a scripted fake Gemini), and a ranking runner wired to in-memory stores and a fake Gemini that answers both
embedding and editorial-evaluation requests.

No test makes a network call, needs an API key or uses quota. All articles are fictional test data on example
domains (see tests/intelligence_helpers.py). The fake embeddings are a deterministic hashed bag of words: stories
sharing words get similar vectors. That is only a test double; the product uses Gemini embeddings."""

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Any

from app.intelligence.contract import StoryAnalysis
from app.intelligence.providers.base import (
    EmbeddingRequest,
    EmbeddingResponse,
    GenerationRequest,
    GenerationResponse,
    ProviderError,
)
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
DIMS = 768


def ranking_settings(**overrides):
    values = {"ranking_max_evaluations_per_run": 40, "embedding_requests_per_minute": 1000}
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


# --- fake embeddings ---------------------------------------------------------------------------------------------
def bag_vector(text: str, dims: int = DIMS) -> list[float]:
    """Deterministic hashed bag of the headline's words (test double only: the fake Stage 3.3 analyses share most
    of their wording, so only the headline, the first sentence after the task prefix, tells stories apart)."""
    headline = text.split("query: ", 1)[-1].split(". ", 1)[0]
    vec = [0.0] * dims
    for word in re.findall(r"[a-z0-9]{3,}", headline.lower()):
        h = int(hashlib.md5(word.encode(), usedforsecurity=False).hexdigest(), 16)
        vec[h % dims] += 1.0 if (h >> 20) % 2 else -1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


# --- fake editorial evaluation -----------------------------------------------------------------------------------
def request_data(request: GenerationRequest) -> dict:
    user = request.messages[1].content
    start = user.rindex("<<DATA ")
    return json.loads(user[user.index("\n", start) + 1 : user.rindex("\n<<END DATA ")])


def evaluation_body(data: dict, *, relation: str = "related_context", **changes) -> dict:
    """A valid evaluation for the data block: every claim supported, every related story given `relation`.
    `changes` use dotted paths, e.g. {"industry_impact.score": 90, "hype.level": "significant"}."""
    ids = [s["id"] for s in data["sources"]]
    body = {
        "event_understanding": "The sources describe one concrete development and what it changes.",
        "core_development": "A concrete development is described by the sources.",
        "technical_novelty": {"score": 60, "level": "meaningful", "explanation": "S1 gives technical detail."},
        "industry_impact": {"score": 60, "areas": ["developers"], "explanation": "S1 describes who it affects."},
        "student_relevance": {"score": 65, "areas": ["ai_ml"], "explanation": "Students can study or use it (S1)."},
        "evidence": {
            "score": 65,
            "explanation": "S1 gives concrete details.",
            "technical_detail": "moderate",
            "primary_source_available": True,
            "independent_reporting_ids": ids[:1],
            "missing_information": [],
        },
        "hype": {"level": "none", "signals": [], "explanation": "Plain reporting."},
        "headline": {"verdict": "supported", "explanation": "S1 states this."},
        "summary": {"verdict": "supported", "explanation": "S1 states this."},
        "claims": [
            {"index": c["index"], "verdict": "supported", "source_ids": ids[:1], "explanation": "S1 states it."}
            for c in data["analysis_to_check"]["claims"]
        ],
        "source_conflicts": [],
        "related": [
            {
                "related_id": r["id"],
                "relation": relation,
                "conflicts_with_story": False,
                "explanation": "Covers a similar topic.",
            }
            for r in data["related_stories"]
        ],
        "content_nature": "factual_report",
        "uncertainty_flags": [],
        "instruction_like_text_found": False,
    }
    body = copy.deepcopy(body)
    for path, value in changes.items():
        node = body
        *parents, last = path.split(".")
        for p in parents:
            node = node[int(p)] if isinstance(node, list) else node[p]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value
    return body


def evaluator(by_headline: dict[str, dict] | None = None, *, relation_for=None, **default_changes):
    """A generate() step answering a valid evaluation. `by_headline`: changes for stories whose analysis headline
    contains the key. `relation_for(data, related) -> relation` picks each related story's relation."""

    def respond(request: GenerationRequest) -> GenerationResponse:
        data = request_data(request)
        headline = data["analysis_to_check"]["headline"]
        changes = dict(default_changes)
        for needle, extra in (by_headline or {}).items():
            if needle.lower() in headline.lower():
                changes.update(extra)
        body = evaluation_body(data, **changes)
        if relation_for is not None:
            for verdict, related in zip(body["related"], data["related_stories"], strict=True):
                verdict["relation"] = relation_for(data, related)
        return response(body, tokens=(900, 300))

    return respond


EmbedStep = ProviderError | Callable[[EmbeddingRequest], EmbeddingResponse] | None


class FakeGemini(FakeProvider):
    """Generation (scripted like Stage 3.3's FakeProvider) plus embeddings. `embed_steps` play in order, the last
    repeats; None means "answer normally"."""

    def __init__(self, *steps, embed_steps: list[EmbedStep] | None = None, vector=bag_vector):
        super().__init__(*(steps or (evaluator(),)))
        self.embed_steps = list(embed_steps or [None])
        self.embed_requests: list[EmbeddingRequest] = []
        self.vector = vector

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.embed_requests.append(request)
        step = self.embed_steps[min(len(self.embed_requests) - 1, len(self.embed_steps) - 1)]
        if isinstance(step, ProviderError):
            raise step
        if callable(step):
            return step(request)
        return EmbeddingResponse(
            vectors=[self.vector(t)[: request.dimensions] for t in request.texts], model=request.model
        )

    @property
    def embedded_texts(self) -> int:
        return sum(len(r.texts) for r in self.embed_requests)


def make_ranker(clusters, records, *, provider=None, store=None, settings=None, ledger=None, clock=lambda: NOW, **kw):
    store = store or MemoryRankingStore()
    alerts: list[dict] = []
    holder = {"clusters": clusters, "records": records}
    provider = provider or FakeGemini()
    runner = RankingRunner(
        settings or ranking_settings(),
        store,
        lambda: holder["clusters"],
        lambda: holder["records"],
        ledger=ledger or MemoryUsageLedger(),
        provider_factory=lambda s: provider,
        config=kw.pop("config", CONFIG),
        registry=kw.pop("registry", REGISTRY),
        clock=clock,
        sleep=NoSleep(),
        alert_hook=alerts.append,
        **kw,
    )
    runner.holder = holder
    runner.alerts = alerts
    runner.provider = provider
    return runner, store


def draft_for(cluster, analysis: StoryAnalysis, related=(), **changes):
    """An EvaluationDraft for one story, built like the fake Gemini's answer (for unit tests of code rules)."""
    from app.intelligence.evidence import build_evidence
    from app.ranking.semantic import EvaluationDraft, data_document

    data = data_document(analysis, build_evidence(cluster), list(related))
    return EvaluationDraft.model_validate(evaluation_body(data, **changes))


def evaluated(cluster, analysis: StoryAnalysis, window=None, *, config=None, relations=None, **changes):
    """evaluate_story with a fake evaluation (no Gemini)."""
    from datetime import date

    from app.ranking.evaluate import evaluate_story
    from app.ranking.window import EditionWindow

    return evaluate_story(
        cluster,
        analysis,
        window or EditionWindow(date(2026, 11, 2)),
        config or CONFIG,
        REGISTRY,
        draft_for(cluster, analysis, **changes),
        relations=relations,
        model_id="gemini-3.5-flash-lite",
    )
