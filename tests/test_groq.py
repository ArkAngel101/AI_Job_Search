"""The Groq client: request shape, strict schema, response parsing, and error mapping (mocked transport)."""
import json
from typing import Any

import httpx
import pytest

from app.adapter import llm
from app.adapter.schema import CVStructured, TailoredCV

KEY = "gsk_TESTKEY-1234567890"


def client_for(handler: Any) -> llm.GroqClient:
    return llm.GroqClient(KEY, transport=httpx.MockTransport(handler))


def reply(content: str | None, finish: str = "stop", usage: dict[str, int] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}],
                                     "usage": usage if usage is not None else {"prompt_tokens": 12, "completion_tokens": 7}})


def tailored_json() -> str:
    return json.dumps({"headline": "H", "summary": "S", "experience": [{"company": "A", "role": "R", "start": "2024", "bullets": ["b"]}]})


def test_request_goes_to_groq_with_a_strict_json_schema_and_the_key_in_the_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(tailored_json())

    result = client_for(handler).messages.parse(model="openai/gpt-oss-120b", max_tokens=1234, system="SYS",
                                                messages=[{"role": "user", "content": "USER"}], output_format=TailoredCV)
    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}" and KEY not in request.content.decode()
    assert body["model"] == "openai/gpt-oss-120b" and body["max_completion_tokens"] == 1234
    assert body["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
    spec = body["response_format"]
    assert spec["type"] == "json_schema" and spec["json_schema"]["strict"] is True and spec["json_schema"]["name"] == "TailoredCV"
    assert not {"temperature", "stream", "tools"} & set(body)
    assert result.parsed_output == TailoredCV.model_validate_json(tailored_json())
    assert (result.usage.input_tokens, result.usage.output_tokens, result.stop_reason) == (12, 7, "end_turn")


@pytest.mark.parametrize("model", [CVStructured, TailoredCV])
def test_schemas_are_strict_everywhere(model: type) -> None:
    schema = llm.strict_schema(model)
    objects = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                objects.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    assert len(objects) >= 2
    for obj in objects:
        assert obj["additionalProperties"] is False and obj["required"] == list(obj["properties"])
    text = json.dumps(schema)
    assert '"title"' not in text and '"default"' not in text


def test_finish_reasons_and_missing_usage_are_handled() -> None:
    assert client_for(lambda r: reply(tailored_json(), "length")).messages.parse(
        model="m", max_tokens=1, system="", messages=[], output_format=TailoredCV).stop_reason == "max_tokens"
    empty = client_for(lambda r: reply(None)).messages.parse(model="m", max_tokens=1, system="", messages=[], output_format=TailoredCV)
    assert empty.parsed_output is None
    no_usage = client_for(lambda r: reply(tailored_json(), usage={})).messages.parse(
        model="m", max_tokens=1, system="", messages=[], output_format=TailoredCV)
    assert (no_usage.usage.input_tokens, no_usage.usage.output_tokens) == (0, 0)


def test_an_answer_that_does_not_match_the_schema_raises_a_validation_error() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        client_for(lambda r: reply('{"headline": 1}')).messages.parse(model="m", max_tokens=1, system="", messages=[], output_format=TailoredCV)


@pytest.mark.parametrize(("status", "headers", "body"), [
    (401, {}, {"error": {"message": f"Invalid API Key {KEY}", "type": "invalid_request_error"}}),
    (429, {}, {"error": {"message": "slow down"}}),
    (400, {"x-request-id": "req_77"}, {"error": {"message": "Jane Q. Doe jane@example.com", "type": "invalid_request_error"}}),
    (500, {}, "not json"),
])
def test_http_errors_become_provider_errors_without_the_message(status: int, headers: dict[str, str], body: Any) -> None:
    response = httpx.Response(status, headers=headers, json=body) if isinstance(body, dict) else httpx.Response(status, content=body)
    with pytest.raises(llm.ProviderError) as exc:
        client_for(lambda r: response).messages.parse(model="m", max_tokens=1, system="", messages=[], output_format=TailoredCV)
    text = llm.describe_provider_error(exc.value) + repr(exc.value) + str(exc.value)
    assert exc.value.status == status and KEY not in text and "Jane" not in text and "jane@" not in text
    if status == 400:
        assert exc.value.request_id == "req_77" and exc.value.kind == "invalid_request_error"


def test_network_failures_and_odd_responses() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect {KEY}", request=request)

    with pytest.raises(llm.ProviderError) as exc:
        client_for(boom).messages.parse(model="m", max_tokens=1, system="", messages=[], output_format=TailoredCV)
    assert exc.value.status == 0 and llm.describe_provider_error(exc.value) == "Could not reach the Groq API." and KEY not in str(exc.value)
    for payload in ({}, {"choices": []}, {"choices": [None]}):
        with pytest.raises(llm.ProviderError) as odd:
            client_for(lambda r, p=payload: httpx.Response(200, json=p)).messages.parse(
                model="m", max_tokens=1, system="", messages=[], output_format=TailoredCV)
        assert odd.value.kind == "unexpected_response"


def test_get_client_needs_the_key_and_model_name_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(llm.NotConfigured, match="GROQ_API_KEY"):
        llm.get_client()
    assert llm.api_key_set() is False
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    assert isinstance(llm.get_client(), llm.GroqClient) and llm.api_key_set() is True
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    assert llm.model_name() == "openai/gpt-oss-120b" and llm.model_from_env() is False
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")  # a leftover Claude name from the old setup is ignored
    assert llm.model_name() == "openai/gpt-oss-120b"
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")
    assert llm.model_name() == "openai/gpt-oss-20b" and llm.model_from_env() is True
