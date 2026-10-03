"""Exercise Grok capability gating over HTTP through the real LLM client.

The mock provider captures outbound Chat Completions requests; no xAI key is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.conftest import configure_mock_llm, get_mock_requests

# Models observed to reject ``reasoning_effort`` at api.x.ai.
UNSUPPORTED_GROK_MODELS = [
    "grok-4",
    "grok-code-fast-1",
    "grok-4-fast-reasoning",
]

# A Grok model that accepts the parameter.
SUPPORTED_GROK_MODEL = "grok-3-mini"


@pytest.fixture(autouse=True)
def _fresh_rejection_cache() -> None:
    """Keep learned rejections from leaking between tests."""
    try:
        from omnigent.llms.reasoning_effort_support import clear_learned_rejections
    except ImportError:
        return
    clear_learned_rejections()


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep localhost mock requests off ambient HTTP proxies."""
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")


async def _send_reasoning_turn(mock_llm_server_url: str, model: str) -> None:
    """Send one reasoning-enabled turn to the mock provider."""
    from omnigent.llms import Client

    client = Client()
    await client.responses.create(
        input=[{"role": "user", "content": f"reasoning-effort probe for {model}"}],
        model=model,
        reasoning={"effort": "low"},
        connection_params={
            "base_url": f"{mock_llm_server_url}/v1",
            "api_key": "mock-key",
        },
    )


def _last_request_for(mock_llm_server_url: str, bare_model: str) -> dict[str, Any]:
    """Return the latest captured request for *bare_model*."""
    requests = get_mock_requests(mock_llm_server_url, key=bare_model)
    assert requests, f"no captured provider request for model {bare_model!r}"
    return requests[-1]


@pytest.mark.parametrize("bare_model", UNSUPPORTED_GROK_MODELS)
async def test_xai_unsupported_grok_models_omit_reasoning_effort(
    mock_llm_server_url: str,
    bare_model: str,
) -> None:
    """Unsupported Grok models must not receive ``reasoning_effort``."""
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}], key=bare_model)

    await _send_reasoning_turn(mock_llm_server_url, f"xai/{bare_model}")

    body = _last_request_for(mock_llm_server_url, bare_model)
    assert "reasoning_effort" not in body, (
        f"xai/{bare_model} does not support 'reasoning_effort', but the "
        f"request body carried reasoning_effort={body.get('reasoning_effort')!r} "
        f"— api.x.ai rejects this with HTTP 400 and the turn fails"
    )


async def test_xai_supported_grok_model_keeps_reasoning_effort(
    mock_llm_server_url: str,
) -> None:
    """A supported Grok model must still receive ``reasoning_effort``."""
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}], key=SUPPORTED_GROK_MODEL)

    await _send_reasoning_turn(mock_llm_server_url, f"xai/{SUPPORTED_GROK_MODEL}")

    body = _last_request_for(mock_llm_server_url, SUPPORTED_GROK_MODEL)
    assert body.get("reasoning_effort") == "low", (
        f"xai/{SUPPORTED_GROK_MODEL} supports 'reasoning_effort' but the "
        f"request body dropped it: {body.get('reasoning_effort')!r}"
    )


async def test_unlisted_model_self_heals_on_live_rejection(
    mock_llm_server_url: str,
) -> None:
    """An unlisted rejection triggers one retry and then a learned skip."""
    bare_model = "grok-experimental-reasoner"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "error": "Argument not supported on this model: reasoning_effort",
                "status_code": 400,
            },
            {"text": "ok"},
            {"text": "ok again"},
        ],
        key=bare_model,
    )

    # First turn: optimistic send, then stripped retry.
    await _send_reasoning_turn(mock_llm_server_url, f"xai/{bare_model}")
    requests = get_mock_requests(mock_llm_server_url, key=bare_model)
    assert len(requests) == 2, (
        f"expected an optimistic send plus one stripped retry, got {len(requests)} requests"
    )
    assert requests[0].get("reasoning_effort") == "low", (
        "the first attempt must send reasoning_effort optimistically"
    )
    assert "reasoning_effort" not in requests[1], (
        f"the retry must strip the rejected param, but the body carried "
        f"reasoning_effort={requests[1].get('reasoning_effort')!r}"
    )

    # Second turn skips the rejected param.
    await _send_reasoning_turn(mock_llm_server_url, f"xai/{bare_model}")
    requests = get_mock_requests(mock_llm_server_url, key=bare_model)
    assert len(requests) == 3, "the learned rejection must skip the wasted round trip"
    assert "reasoning_effort" not in requests[2], (
        "a learned rejection must omit reasoning_effort up front"
    )


async def test_non_xai_provider_keeps_reasoning_effort_passthrough(
    mock_llm_server_url: str,
) -> None:
    """A non-xAI provider keeps the parameter."""
    bare_model = "llama-3.3-70b-versatile"
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}], key=bare_model)

    await _send_reasoning_turn(mock_llm_server_url, f"groq/{bare_model}")

    body = _last_request_for(mock_llm_server_url, bare_model)
    assert body.get("reasoning_effort") == "low", (
        f"non-xAI provider lost its reasoning_effort pass-through: "
        f"{body.get('reasoning_effort')!r}"
    )
