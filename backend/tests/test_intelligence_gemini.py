"""The Gemini provider (the default, free tier via Google AI Studio) against a mocked HTTP transport, plus the
engine's request pacing and fallback-model switch. No network, no key."""

import asyncio
import json

import httpx
import pytest

from app.intelligence.draft import response_json_schema
from app.intelligence.engine import RateGate, RequestPacer, StoryAnalyzer
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers import build_provider
from app.intelligence.providers.base import GenerationRequest, Message, ProviderError
from app.intelligence.providers.gemini import GeminiProvider, inline_refs
from app.intelligence.runner import check_ready
from app.models.enums import IntelligenceStatus
from tests.conftest import make_settings
from tests.intelligence_helpers import CATEGORIES, FakeProvider, NoSleep, ai_settings, clusters, good_draft, response
from tests.processing_helpers import NOW

SCHEMA = response_json_schema(CATEGORIES.keys, 3)
KEY = "AIza-test-not-a-real-key"


def gemini_settings(**overrides):
    values = {"ai_provider": "gemini", "gemini_api_key": KEY, "ai_model": None, "openai_api_key": None}
    values.update(overrides)
    return ai_settings(**values)


def request(**kw) -> GenerationRequest:
    values = dict(
        model="gemini-2.5-flash-lite",
        messages=[Message("system", "rules"), Message("user", "evidence"), Message("assistant", "{}"),
                  Message("user", "fix it")],
        schema_name="story_intelligence",
        json_schema=SCHEMA,
        max_output_tokens=1500,
    )  # fmt: skip
    values.update(kw)
    return GenerationRequest(**values)


def call(handler, settings=None, req=None):
    async def go():
        provider = GeminiProvider(settings or gemini_settings(), transport=httpx.MockTransport(handler))
        try:
            return await provider.generate(req or request())
        finally:
            await provider.aclose()

    return asyncio.run(go())


def ok_body(text: str | None = None, finish="STOP", **extra) -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text or json.dumps(good_draft())}]},
                "finishReason": finish,
            }
        ],
        "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 500, "thoughtsTokenCount": 30},
        "modelVersion": "gemini-2.5-flash-lite",
        "responseId": "resp_1",
        **extra,
    }


def test_gemini_is_the_default_provider_with_flash_lite_and_its_own_endpoint():
    s = make_settings(database_url=None, gemini_api_key=KEY)
    assert s.ai_provider == "gemini" and s.ai_model == "gemini-2.5-flash-lite"
    assert s.ai_endpoint == "https://generativelanguage.googleapis.com/v1beta"
    assert s.ai_requests_per_minute == 10 and s.ai_thinking_budget == 0
    assert s.ai_api_key_name == "GEMINI_API_KEY" and s.ai_api_key.get_secret_value() == KEY
    assert isinstance(build_provider(s), GeminiProvider)
    # OpenAI keeps its own endpoint and still has no default model.
    o = make_settings(database_url=None, ai_provider="openai", openai_api_key="sk-x")
    assert o.ai_model is None and o.ai_endpoint == "https://api.openai.com/v1"


def test_missing_gemini_key_is_a_clear_configuration_error():
    s = gemini_settings(gemini_api_key=" ")
    assert s.gemini_api_key is None
    with pytest.raises(IntelligenceConfigError) as info:
        check_ready(s)
    assert "aistudio.google.com/apikey" in info.value.safe_message
    with pytest.raises(IntelligenceConfigError):
        build_provider(s)


def test_request_uses_native_json_schema_header_key_and_roles():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["key"] = req.headers["x-goog-api-key"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ok_body())

    result = call(handler)
    body = seen["body"]
    assert (
        seen["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
    )
    assert seen["key"] == KEY and KEY not in seen["url"]  # never in the URL (logs, proxies)
    assert body["systemInstruction"] == {"parts": [{"text": "rules"}]}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json" and config["maxOutputTokens"] == 1500
    assert config["thinkingConfig"] == {"thinkingBudget": 0} and "temperature" not in config
    schema_text = json.dumps(config["responseJsonSchema"])
    assert "$ref" not in schema_text and "$defs" not in schema_text
    classification = config["responseJsonSchema"]["properties"]["classification"]
    assert classification["properties"]["primary"]["enum"] == CATEGORIES.keys
    assert result.usage.input_tokens == 1200 and result.usage.output_tokens == 530  # thinking tokens count
    assert result.model == "gemini-2.5-flash-lite" and result.request_id == "resp_1" and result.finish_reason == "stop"
    assert json.loads(result.content)["headline"]


def test_thinking_budget_only_for_gemini_2_5_and_unset_means_model_default():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content)["generationConfig"])
        return httpx.Response(200, json=ok_body())

    call(handler, req=request(model="gemini-3-flash", temperature=0.2))
    assert "thinkingConfig" not in seen and seen["temperature"] == 0.2
    seen.clear()
    call(handler, settings=gemini_settings(ai_thinking_budget=None))
    assert "thinkingConfig" not in seen


def test_inline_refs_flattens_the_schema_without_changing_it():
    flat = inline_refs(SCHEMA)
    assert flat["properties"]["entities"]["properties"]["people"]["type"] == "array"
    assert "$defs" in SCHEMA  # the original is untouched


def test_invalid_model_names_never_reach_the_url():
    with pytest.raises(ProviderError) as info:
        call(lambda req: httpx.Response(200, json=ok_body()), req=request(model="../../evil?x=1"))
    assert info.value.kind == "bad_request"


def google_error(code: int, status: str, *details) -> dict:
    return {"error": {"code": code, "message": "x", "status": status, "details": list(details)}}


PER_MINUTE = {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
              "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}  # fmt: skip
PER_DAY = {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
           "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}  # fmt: skip
RETRY = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}
BAD_KEY = {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}


@pytest.mark.parametrize(
    ("status", "body", "kind", "retry_after"),
    [
        (429, google_error(429, "RESOURCE_EXHAUSTED", PER_MINUTE, RETRY), "rate_limited", 37.0),
        (429, google_error(429, "RESOURCE_EXHAUSTED", PER_DAY, RETRY), "quota", None),
        (400, google_error(400, "INVALID_ARGUMENT", BAD_KEY), "auth", None),
        (403, google_error(403, "PERMISSION_DENIED"), "auth", None),
        (404, google_error(404, "NOT_FOUND"), "auth", None),
        (400, google_error(400, "FAILED_PRECONDITION"), "auth", None),
        (503, google_error(503, "UNAVAILABLE"), "unavailable", None),
        (400, google_error(400, "INVALID_ARGUMENT"), "bad_request", None),
    ],
)
def test_google_errors_map_to_structured_provider_errors(status, body, kind, retry_after):
    with pytest.raises(ProviderError) as info:
        call(lambda req: httpx.Response(status, json=body))
    err = info.value
    assert err.kind == kind and err.status_code == status and KEY not in err.safe_message
    assert err.retry_after == retry_after


def test_safety_blocks_and_truncation_reach_the_engine_as_refusal_and_length():
    blocked = call(lambda req: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    assert blocked.content is None and "SAFETY" in blocked.refusal
    filtered = call(lambda req: httpx.Response(200, json=ok_body(finish="SAFETY")))
    assert filtered.refusal
    truncated = call(lambda req: httpx.Response(200, json=ok_body(text='{"head', finish="MAX_TOKENS")))
    assert truncated.finish_reason == "length" and truncated.refusal is None


def test_thought_parts_are_not_part_of_the_answer():
    body = ok_body()
    body["candidates"][0]["content"]["parts"].insert(0, {"text": "thinking...", "thought": True})
    assert json.loads(call(lambda req: httpx.Response(200, json=body)).content)["headline"]


@pytest.mark.parametrize("payload", [b"not json", b'{"candidates": [{"content": 5}]}'])
def test_unreadable_success_bodies_are_invalid_responses(payload):
    with pytest.raises(ProviderError) as info:
        call(lambda req: httpx.Response(200, content=payload))
    assert info.value.kind == "invalid_response"


def test_pacer_spaces_requests_to_the_per_minute_limit():
    sleep = NoSleep()
    now = [0.0]

    async def fake_sleep(seconds):
        await sleep(seconds)
        now[0] += seconds

    pacer = RequestPacer(10, fake_sleep, clock=lambda: now[0])

    async def go():
        for _ in range(3):
            await pacer.acquire()

    asyncio.run(go())
    assert sleep.delays == [6.0, 6.0]  # 10/minute = one start every 6 s
    off = RequestPacer(0, fake_sleep)
    asyncio.run(off.acquire())
    assert sleep.delays == [6.0, 6.0]


def analyser(provider, **settings):
    sleep = NoSleep()
    return StoryAnalyzer(ai_settings(**settings), provider, CATEGORIES, sleep=sleep, gate=RateGate(sleep))


def test_daily_quota_switches_to_the_fallback_model_for_the_rest_of_the_run():
    provider = FakeProvider(ProviderError("quota", "used up"), response(good_draft()))
    analyzer = analyser(provider, ai_model="gemini-2.5-flash-lite", ai_fallback_model="gemini-2.5-flash")
    cluster = clusters()[0]
    outcome = asyncio.run(analyzer.analyse(cluster, NOW))
    assert outcome.status is IntelligenceStatus.ANALYSED and outcome.calls == 2
    assert [r.model for r in provider.requests] == ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
    asyncio.run(analyzer.analyse(cluster, NOW))
    assert provider.requests[-1].model == "gemini-2.5-flash"  # later stories go straight to the fallback
    assert analyzer.exhausted_models == {"gemini-2.5-flash-lite"}


def test_rate_limit_that_outlasts_retries_switches_to_fallback_once():
    limited = ProviderError("rate_limited", "429", retry_after=5.0)
    provider = FakeProvider(limited, limited, limited, response(good_draft()))
    analyzer = analyser(provider, ai_model="a", ai_fallback_model="b", ai_max_retries=2)
    outcome = asyncio.run(analyzer.analyse(clusters()[0], NOW))
    assert outcome.status is IntelligenceStatus.ANALYSED
    assert [r.model for r in provider.requests] == ["a", "a", "a", "b"]


def test_without_a_fallback_or_after_it_is_used_up_quota_stays_fatal():
    outcome = asyncio.run(analyser(FakeProvider(ProviderError("quota", "x"))).analyse(clusters()[0], NOW))
    assert outcome.fatal and outcome.error.kind == "quota"
    provider = FakeProvider(ProviderError("quota", "x"))
    outcome = asyncio.run(analyser(provider, ai_fallback_model="b").analyse(clusters()[0], NOW))
    assert outcome.fatal and [r.model for r in provider.requests] == ["test-model", "b"]  # no loop


def test_blank_env_values_fall_back_to_defaults(monkeypatch):
    for name, value in {"AI_BASE_URL": "", "AI_MODEL": "", "AI_THINKING_BUDGET": "", "AI_FALLBACK_MODEL": " ",
                        "GEMINI_API_KEY": KEY}.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    s = make_settings(database_url=None)
    assert s.ai_base_url is None and s.ai_endpoint.endswith("/v1beta")
    assert s.ai_model == "gemini-2.5-flash-lite" and s.ai_thinking_budget is None and s.ai_fallback_model is None
