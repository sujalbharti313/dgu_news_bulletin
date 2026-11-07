"""The provider interface and the OpenAI implementation, against a mocked HTTP transport (no network, no key)."""

import asyncio
import json

import httpx
import pytest

from app.intelligence.draft import response_json_schema
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers import build_provider
from app.intelligence.providers.base import GenerationRequest, IntelligenceProvider, Message, ProviderError
from app.intelligence.providers.openai import OpenAIProvider
from tests.intelligence_helpers import CATEGORIES, FakeProvider, ai_settings, good_draft

SCHEMA = response_json_schema(CATEGORIES.keys, 3)


def request(**kw) -> GenerationRequest:
    values = dict(
        model="configured-model",
        messages=[Message("system", "rules"), Message("user", "evidence")],
        schema_name="story_intelligence",
        json_schema=SCHEMA,
        max_output_tokens=1500,
    )
    values.update(kw)
    return GenerationRequest(**values)


def call(handler, settings=None, req=None):
    async def go():
        provider = OpenAIProvider(settings or ai_settings(), transport=httpx.MockTransport(handler))
        try:
            return await provider.generate(req or request())
        finally:
            await provider.aclose()

    return asyncio.run(go())


def ok_body(content: str | None = None, **extra) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "configured-model-2026-01-01",
        "choices": [
            {
                "message": {"role": "assistant", "content": content or json.dumps(good_draft()), "refusal": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801},
        **extra,
    }


def test_fake_and_openai_providers_satisfy_the_interface():
    def implements(obj) -> bool:
        return all(callable(getattr(obj, m, None)) for m in ("generate", "aclose")) and isinstance(obj.name, str)

    assert implements(FakeProvider()) and implements(OpenAIProvider(ai_settings()))
    assert IntelligenceProvider  # the Protocol both follow


def test_request_payload_uses_strict_schema_configured_model_and_server_side_key():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ok_body(), headers={"x-request-id": "req_1"})

    result = call(handler, ai_settings(ai_base_url="https://llm.example.com/v1/"))
    body = seen["body"]
    assert seen["url"] == "https://llm.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-not-a-real-key"
    assert body["model"] == "configured-model"  # from the request (i.e. settings), never hard-coded
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_completion_tokens"] == 1500 and body["store"] is False
    assert "temperature" not in body  # provider default unless AI_TEMPERATURE is set
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert result.usage.input_tokens == 1234 and result.usage.output_tokens == 567
    assert result.model == "configured-model-2026-01-01" and result.request_id == "req_1"
    assert json.loads(result.content)["headline"]


def test_temperature_is_sent_only_when_configured():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=ok_body())

    call(handler, req=request(temperature=0.2))
    assert seen["temperature"] == 0.2


def test_strict_schema_requires_every_property_and_enumerates_categories():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for key in ("maxLength", "minLength", "maxItems", "minItems", "default"):
                assert key not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(SCHEMA)
    cls = SCHEMA["$defs"]["ClassificationDraft"]["properties"]
    assert cls["primary"]["enum"] == CATEGORIES.keys and cls["secondary"]["items"]["enum"] == CATEGORIES.keys


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind", "retryable", "fatal"),
    [
        (429, {"error": {"code": "rate_limit_exceeded"}}, {"retry-after": "7"}, "rate_limited", True, False),
        (429, {"error": {"code": "insufficient_quota"}}, {}, "quota", False, True),
        (401, {"error": {"code": "invalid_api_key"}}, {}, "auth", False, True),
        (404, {"error": {"code": "model_not_found"}}, {}, "auth", False, True),
        (500, {}, {}, "unavailable", True, False),
        (503, {}, {"retry-after-ms": "1500"}, "unavailable", True, False),
        (400, {"error": {"code": "invalid_request_error"}}, {}, "bad_request", False, False),
    ],
)
def test_http_errors_map_to_structured_provider_errors(status, body, headers, kind, retryable, fatal):
    with pytest.raises(ProviderError) as info:
        call(lambda req: httpx.Response(status, json=body, headers=headers))
    err = info.value
    assert (err.kind, err.retryable, err.fatal, err.status_code) == (kind, retryable, fatal, status)
    assert "sk-test" not in err.safe_message
    if "retry-after" in headers:
        assert err.retry_after == 7.0
    if "retry-after-ms" in headers:
        assert err.retry_after == 1.5


def test_timeouts_and_connection_errors_are_retryable():
    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    def refused(req):
        raise httpx.ConnectError("refused", request=req)

    for handler, kind in ((timeout, "timeout"), (refused, "unavailable")):
        with pytest.raises(ProviderError) as info:
            call(handler)
        assert info.value.kind == kind and info.value.retryable


@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"choices": []}'])
def test_unreadable_success_bodies_are_invalid_responses(payload):
    with pytest.raises(ProviderError) as info:
        call(lambda req: httpx.Response(200, content=payload))
    assert info.value.kind == "invalid_response"


def test_refusal_and_truncation_are_passed_through_for_the_engine():
    body = ok_body()
    body["choices"][0]["message"].update(content=None, refusal="I can't help with that.")
    body["choices"][0]["finish_reason"] = "length"
    result = call(lambda req: httpx.Response(200, json=body))
    assert result.content is None and result.refusal and result.finish_reason == "length"


def test_provider_needs_a_key_and_a_known_name():
    with pytest.raises(IntelligenceConfigError):
        build_provider(ai_settings(openai_api_key=None))
    with pytest.raises(IntelligenceConfigError):
        build_provider(ai_settings(ai_provider="other"))
    assert isinstance(build_provider(ai_settings()), OpenAIProvider)


def test_ai_settings_are_validated():
    with pytest.raises(ValueError):
        ai_settings(ai_base_url="http://llm.example.com/v1")  # plain http only for localhost
    assert ai_settings(ai_base_url="http://localhost:11434/v1").ai_base_url == "http://localhost:11434/v1"
    with pytest.raises(ValueError):
        ai_settings(ai_max_project_ideas=4)
    assert ai_settings(ai_model="  ").ai_model is None
    assert ai_settings(openai_api_key=" ").openai_api_key is None
