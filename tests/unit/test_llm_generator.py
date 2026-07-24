from __future__ import annotations

import json

import httpx
import pytest

from db_sanitizer.errors import GenerationError
from db_sanitizer.llm import (
    FakeProvider,
    GeneratedItem,
    GenerationResponse,
    OllamaProvider,
    OpenRouterProvider,
    ReplacementGenerator,
)
from db_sanitizer.mapping import HMACKey, MappingRegistry, RunMetadata
from db_sanitizer.policy.models import ConsistencyGroup

_SECRET = b"k" * 32


def _group() -> ConsistencyGroup:
    return ConsistencyGroup.model_validate(
        {
            "entity_type": "email",
            "locale": "en-US",
            "normalization": "email",
            "generation": {
                "description": "Synthetic email at example.test",
                "min_length": 8,
                "max_length": 80,
                "regex": r"^[a-z]+@example\.test$",
            },
            "columns": [{"schema": "public", "table": "users", "column": "email"}],
        }
    )


def _metadata() -> RunMetadata:
    return RunMetadata(
        run_id="llm-unit-run",
        policy_sha256="a" * 64,
        source_schema_sha256="b" * 64,
        llm_provider="fake",
        llm_model="fake-model",
        hmac_key_fingerprint=HMACKey(_SECRET).fingerprint,
    )


def _generator(registry: MappingRegistry, provider: FakeProvider, *, retries: int = 1):
    return ReplacementGenerator(
        provider=provider,
        registry=registry,
        hmac_key=HMACKey(_SECRET),
        batch_size=2,
        max_retries=retries,
    )


def test_generation_prompt_and_provider_request_never_leak_source_data(tmp_path) -> None:
    raw_source = "RAW-PII-MUST-NOT-REACH-LLM"
    source_hmac = HMACKey(_SECRET).digest("email", raw_source.casefold())
    provider = FakeProvider([GenerationResponse(items=[GeneratedItem(value="ada@example.test")])])

    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        registry.insert_source_key("email", source_hmac)
        _generator(registry, provider).generate_group("email", _group())

    request_text = json.dumps(provider.requests[0].model_dump(), default=str)
    assert raw_source not in request_text
    assert source_hmac not in request_text
    assert provider.requests[0].count == 3  # one required mapping plus bounded surplus


def test_duplicate_values_are_retried_before_stable_assignment(tmp_path) -> None:
    provider = FakeProvider(
        [
            GenerationResponse(
                items=[
                    GeneratedItem(value="ada@example.test"),
                    GeneratedItem(value="ada@example.test"),
                ]
            ),
            GenerationResponse(
                items=[
                    GeneratedItem(value="ada@example.test"),
                    GeneratedItem(value="bea@example.test"),
                ]
            ),
        ]
    )
    key = HMACKey(_SECRET)

    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        first = key.digest("email", "first@source.test")
        second = key.digest("email", "second@source.test")
        registry.insert_source_keys("email", [second, first])
        assert _generator(registry, provider).generate_group("email", _group()) == 2
        assert registry.lookup("email", first) == "ada@example.test"
        assert registry.lookup("email", second) == "bea@example.test"

    assert len(provider.requests) == 2


def test_source_collisions_are_dropped_and_only_missing_count_is_retried(tmp_path) -> None:
    provider = FakeProvider(
        [
            GenerationResponse(
                items=[
                    GeneratedItem(value="taken@example.test"),
                    GeneratedItem(value="fresh@example.test"),
                ]
            ),
            GenerationResponse(items=[GeneratedItem(value="other@example.test")]),
        ]
    )
    key = HMACKey(_SECRET)

    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        registry.insert_source_keys(
            "email",
            [
                key.digest("email", "taken@example.test"),
                key.digest("email", "source@example.test"),
            ],
        )
        assert _generator(registry, provider).generate_group("email", _group()) == 2
        assert registry.mapping_count("email", assigned_only=True) == 2

    assert [request.count for request in provider.requests] == [4, 3]


def test_malformed_structured_output_retries_then_fails_closed(tmp_path) -> None:
    provider = FakeProvider([{"items": "not-a-list"}, {"wrong": []}])

    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        registry.insert_source_key("email", HMACKey(_SECRET).digest("email", "source@example.test"))
        with pytest.raises(GenerationError, match="valid replacement batch"):
            _generator(registry, provider).generate_group("email", _group())
        assert registry.mapping_count("email", assigned_only=True) == 0

    assert len(provider.requests) == 2


def test_openrouter_uses_api_key_and_json_schema_without_network() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"items":[{"value":"ada@example.test"}]}'}}]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenRouterProvider(
        base_url="https://openrouter.example/api/v1",
        api_key="test-api-key",
        model="deepseek/deepseek-v4-flash",
        client=client,
    )
    response = provider.generate(_generator_request := _generator_request_for_test())

    assert response.items[0].value == "ada@example.test"
    assert captured["headers"]["authorization"] == "Bearer test-api-key"
    body = captured["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert _generator_request.model_dump()["locale"] == "en-US"
    client.close()


def test_ollama_uses_schema_without_source_data_or_an_api_key() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"message": {"content": '{"items":[{"value":"ada@example.test"}]}'}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OllamaProvider(
        base_url="http://ollama.example:11434",
        model="qwen3:4b",
        client=client,
    )
    request = _generator_request_for_test()
    response = provider.generate(request)

    assert response.items[0].value == "ada@example.test"
    assert captured["url"] == "http://ollama.example:11434/api/chat"
    body = captured["body"]
    assert body["stream"] is False
    assert body["format"]["type"] == "object"
    assert body["options"]["temperature"] == pytest.approx(1.15)
    assert "authorization" not in captured["headers"]
    request_text = json.dumps(body, ensure_ascii=False)
    assert "RAW-PII-MUST-NOT-REACH-LLM" not in request_text
    client.close()


def _generator_request_for_test():
    from db_sanitizer.llm import GenerationRequest

    group = _group()
    return GenerationRequest(
        entity_type=group.entity_type, locale=group.locale, constraints=group.generation, count=1
    )
