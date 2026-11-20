"""Tests for live Google Gemini Models API metadata normalization and caching."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from omnigent.llms.gemini_model_metadata import (
    _clear_model_metadata_cache,
    extract_gemini_model_id,
    gemini_models_url,
    get_gemini_model_metadata,
    lookup_cached_gemini_context_window,
    parse_gemini_model_metadata,
    supports_gemini_generate_content,
)
from omnigent.models.model_metadata import ModelCapability, ModelWireAPI


@pytest.fixture(autouse=True)
def _reset_gemini_metadata_cache() -> None:
    _clear_model_metadata_cache()


def test_parse_gemini_model_metadata_extracts_limits_thinking_and_wire_api() -> None:
    """Native Gemini model fields normalize into provider-neutral ``ModelMetadata``."""
    metadata = parse_gemini_model_metadata(
        {
            "name": "models/gemini-3.8-flash",
            "inputTokenLimit": 1_048_576,
            "outputTokenLimit": 65_536,
            "thinking": True,
            "supportedGenerationMethods": ["generateContent", "countTokens"],
        }
    )

    assert metadata.context_window == 1_048_576
    assert metadata.max_output_tokens == 65_536
    assert metadata.supports(ModelCapability.REASONING) is True
    assert metadata.wire_apis == frozenset({ModelWireAPI.GEMINI_GENERATE_CONTENT})


def test_parse_gemini_model_metadata_handles_tri_state_thinking_and_invalid_limits() -> None:
    """``thinking: false`` marks unsupported, omitted stays unknown, non-positive/bool ignored."""
    non_thinking = parse_gemini_model_metadata(
        {
            "name": "models/gemini-2.0-flash",
            "inputTokenLimit": True,
            "outputTokenLimit": 0,
            "thinking": False,
            "supportedGenerationMethods": ["embedContent"],
        }
    )
    assert non_thinking.context_window is None
    assert non_thinking.max_output_tokens is None
    assert non_thinking.supports(ModelCapability.REASONING) is False
    assert non_thinking.wire_apis == frozenset()

    unknown_thinking = parse_gemini_model_metadata({"name": "models/gemini-custom"})
    assert unknown_thinking.supports(ModelCapability.REASONING) is None
    assert unknown_thinking.wire_apis == frozenset({ModelWireAPI.GEMINI_GENERATE_CONTENT})


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"name": "models/gemini-3-pro-preview"}, "gemini-3-pro-preview"),
        ({"name": "/models/gemini-2.5-flash"}, "gemini-2.5-flash"),
        ({"name": "gemini//models/gemini-2.5-flash"}, "gemini-2.5-flash"),
        ({"baseModelId": "gemini-3.5-flash"}, "gemini-3.5-flash"),
        ({"name": "models/"}, None),
        ({}, None),
    ],
)
def test_extract_gemini_model_id(payload: dict[str, object], expected: str | None) -> None:
    assert extract_gemini_model_id(payload) == expected


def test_supports_gemini_generate_content() -> None:
    assert supports_gemini_generate_content({"supportedGenerationMethods": ["generateContent"]})
    assert not supports_gemini_generate_content({"supportedGenerationMethods": ["embedContent"]})
    assert supports_gemini_generate_content({})


@pytest.mark.parametrize(
    ("provider_base", "expected_url"),
    [
        (
            "https://generativelanguage.googleapis.com/v1beta",
            "https://generativelanguage.googleapis.com/v1beta/models",
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "https://generativelanguage.googleapis.com/v1beta/models",
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://generativelanguage.googleapis.com/v1beta/models",
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta/OpenAI?foo=bar#frag ",
            "https://generativelanguage.googleapis.com/v1beta/models",
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta?key=abc",
            "https://generativelanguage.googleapis.com/v1beta/models",
        ),
        (
            "https://generativelanguage.googleapis.com/v1",
            "https://generativelanguage.googleapis.com/v1/models",
        ),
        (
            "https://generativelanguage.googleapis.com",
            "https://generativelanguage.googleapis.com/v1beta/models",
        ),
        (
            "https://proxy.example.com/v1beta/models",
            "https://proxy.example.com/v1beta/models",
        ),
        (
            "https://proxy.example.com/v1alpha",
            "https://proxy.example.com/v1alpha/models",
        ),
        (
            "https://proxy.example.com/v٤",
            "https://proxy.example.com/v٤/v1beta/models",
        ),
        (
            "https://v1beta",
            "https://v1beta/v1beta/models",
        ),
    ],
)
def test_gemini_models_url_normalizes_bases(provider_base: str, expected_url: str) -> None:
    assert gemini_models_url(provider_base) == expected_url


@pytest.mark.asyncio
async def test_get_gemini_model_metadata_fetches_caches_and_populates_context_window() -> None:
    """Successful lookup caches per credential and seeds the context-window cache."""
    requests_seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "name": "models/gemini-3.8-flash",
                "inputTokenLimit": 1_048_576,
                "outputTokenLimit": 65_536,
                "thinking": True,
                "supportedGenerationMethods": ["generateContent"],
            },
        )

    transport = httpx.MockTransport(_handler)
    headers = {"x-goog-api-key": "test-key-1"}

    first = await get_gemini_model_metadata(
        headers,
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini/gemini-3.8-flash",
        transport=transport,
    )
    second = await get_gemini_model_metadata(
        headers,
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3.8-flash",
        transport=transport,
    )
    other_key = await get_gemini_model_metadata(
        {"x-goog-api-key": "test-key-2"},
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3.8-flash",
        transport=transport,
    )

    assert len(requests_seen) == 2
    assert (
        str(requests_seen[0].url)
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash"
    )
    assert requests_seen[0].headers["x-goog-api-key"] == "test-key-1"
    assert first == second == other_key
    assert first is not None
    assert first.context_window == 1_048_576
    assert first.max_output_tokens == 65_536
    assert first.supports(ModelCapability.REASONING) is True
    assert lookup_cached_gemini_context_window("gemini-3.8-flash") == 1_048_576
    assert lookup_cached_gemini_context_window("gemini/gemini-3.8-flash") == 1_048_576
    assert lookup_cached_gemini_context_window("vertex/gemini-3.8-flash") == 1_048_576


@pytest.mark.asyncio
async def test_get_gemini_model_metadata_does_not_cache_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transient lookup failures are not cached and retry on the next call."""
    calls = 0

    def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"name": "models/gemini-3-pro-preview", "inputTokenLimit": 1_048_576},
        )

    transport = httpx.MockTransport(_handler)
    headers = {"x-goog-api-key": "test-key"}
    with caplog.at_level(logging.WARNING, logger="omnigent.llms.gemini_model_metadata"):
        first = await get_gemini_model_metadata(
            headers,
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-3-pro-preview",
            transport=transport,
        )
    second = await get_gemini_model_metadata(
        headers,
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3-pro-preview",
        transport=transport,
    )

    assert first is None
    assert second is not None
    assert second.context_window == 1_048_576
    assert calls == 2
    assert "Gemini model metadata lookup failed" in caplog.text


@pytest.mark.asyncio
async def test_get_gemini_model_metadata_deduplicates_concurrent_fetches() -> None:
    """Concurrent cold lookups for the same model share one HTTP request."""
    calls = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json={"name": "models/gemini-3-pro-preview", "inputTokenLimit": 1_048_576},
        )

    transport = httpx.MockTransport(_handler)
    headers = {"x-goog-api-key": "test-key"}
    first, second = await asyncio.gather(
        get_gemini_model_metadata(
            headers,
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-3-pro-preview",
            transport=transport,
        ),
        get_gemini_model_metadata(
            headers,
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-3-pro-preview",
            transport=transport,
        ),
    )

    assert first == second
    assert first is not None
    assert calls == 1


@pytest.mark.asyncio
async def test_get_gemini_model_metadata_header_partition_and_proxy_guard() -> None:
    """Header partitioning is case-insensitive and custom proxies skip global cache."""
    calls = 0

    def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"name": "models/gemini-proxy-test", "inputTokenLimit": 32_768},
        )

    transport = httpx.MockTransport(_handler)
    first = await get_gemini_model_metadata(
        {"X-Goog-Api-Key": "secret-a"},
        "https://proxy.example.com/v1beta",
        "gemini-proxy-test",
        transport=transport,
    )
    same_key_diff_case = await get_gemini_model_metadata(
        {"x-goog-api-key": "secret-a"},
        "https://proxy.example.com/v1beta",
        "gemini-proxy-test",
        transport=transport,
    )
    other_bearer = await get_gemini_model_metadata(
        {"Authorization": "Bearer secret-b"},
        "https://proxy.example.com/v1beta",
        "gemini-proxy-test",
        transport=transport,
    )

    assert calls == 2
    assert first == same_key_diff_case == other_bearer
    # Non-canonical proxy host must not populate the global context-window cache.
    assert lookup_cached_gemini_context_window("gemini-proxy-test") is None
