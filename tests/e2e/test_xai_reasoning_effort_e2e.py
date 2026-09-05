"""E2E coverage for xAI ``reasoning_effort`` gating.

The Chat Completions path in ``omnigent/llms/client.py`` used to
forward ``reasoning_effort`` to every non-OpenAI provider
unconditionally. xAI accepts the parameter on only a subset of Grok
models, so a reasoning-configured call to ``grok-4``,
``grok-code-fast-1``, or ``grok-4-fast-reasoning`` was rejected by
api.x.ai with HTTP 400 ("Argument not supported on this model:
reasoning_effort") and the turn failed.

These tests drive the REAL ``omnigent.llms.Client`` over live HTTP
against the repo's mock OpenAI-compatible provider (the same
``mock_llm_server_url`` rig the rest of ``tests/e2e`` uses, standing
in for ``https://api.x.ai/v1`` via ``connection_params``) and assert
on the outbound ``/v1/chat/completions`` request body the provider
actually receives:

- Grok models with known rejections must NOT receive
  ``reasoning_effort``.
- Supported Grok models and non-xAI providers must keep receiving it
  (guards against over-fixing).

Runs entirely against the mock LLM server — no real API key needed::

    pytest tests/e2e/test_xai_reasoning_effort_e2e.py -v
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.conftest import configure_mock_llm, get_mock_requests

# Grok models that api.x.ai rejects ``reasoning_effort`` on (HTTP 400).
UNSUPPORTED_GROK_MODELS = [
    "grok-4",
    "grok-code-fast-1",
    "grok-4-fast-reasoning",
]

# A Grok model that accepts ``reasoning_effort`` — the fix must keep
# sending it here.
SUPPORTED_GROK_MODEL = "grok-3-mini"


@pytest.fixture(autouse=True)
def _fresh_rejection_cache() -> None:
    """Isolate the learned-rejection cache across tests.

    Import-tolerant so the suite still runs — and fails on the observed
    behavior, not on a missing module — against a tree without the fix.
    """
    try:
        from omnigent.llms.reasoning_effort_support import clear_learned_rejections
    except ImportError:
        return
    clear_learned_rejections()


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the client's localhost calls off any ambient HTTP proxy.

    The adapter's ``httpx.AsyncClient`` honours proxy env vars
    (``trust_env=True``); on proxied CI hosts that would bounce the
    mock-server request through a corporate proxy and 502.
    """
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
    """Drive one reasoning-enabled ``responses.create`` call at *model*.

    Uses the real multi-provider client — the exact request-construction
    path the bug lives in — with ``connection_params`` pointing the
    provider adapter at the mock server instead of the vendor endpoint.

    :param mock_llm_server_url: The mock provider's base URL.
    :param model: Provider-prefixed model string, e.g. ``"xai/grok-4"``.
    """
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
    """Return the latest captured provider request for *bare_model*.

    :param mock_llm_server_url: The mock provider's base URL.
    :param bare_model: Model id without provider prefix, e.g. ``"grok-4"``.
    :returns: The captured Chat Completions request body.
    """
    requests = get_mock_requests(mock_llm_server_url, key=bare_model)
    assert requests, f"no captured provider request for model {bare_model!r}"
    return requests[-1]


@pytest.mark.parametrize("bare_model", UNSUPPORTED_GROK_MODELS)
async def test_xai_unsupported_grok_models_omit_reasoning_effort(
    mock_llm_server_url: str,
    bare_model: str,
) -> None:
    """Unsupported Grok models must not receive ``reasoning_effort``.

    api.x.ai rejects the parameter on these models with HTTP 400, so
    forwarding it fails every reasoning-enabled turn.
    """
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
    """A Grok model that accepts the parameter must keep receiving it.

    Guards the fix's allowlist: gating must not strip reasoning from
    the Grok models that do support ``reasoning_effort``.
    """
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
    """An unlisted model that rejects the param strips it and retries.

    The seed set is an optimization, not a correctness dependency: when
    a model outside it returns the xAI-style HTTP 400 naming
    ``reasoning_effort``, the client must retry once without the param
    (the turn succeeds) and skip it on subsequent calls to that model.
    """
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

    # First turn: optimistic send -> 400 -> stripped retry succeeds.
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

    # Second turn: the rejection is remembered — no wasted round trip.
    await _send_reasoning_turn(mock_llm_server_url, f"xai/{bare_model}")
    requests = get_mock_requests(mock_llm_server_url, key=bare_model)
    assert len(requests) == 3, "the learned rejection must skip the wasted round trip"
    assert "reasoning_effort" not in requests[2], (
        "a learned rejection must omit reasoning_effort up front"
    )


async def test_non_xai_provider_keeps_reasoning_effort_passthrough(
    mock_llm_server_url: str,
) -> None:
    """Every other provider keeps the prior pass-through behaviour.

    The gating is seeded per-model; a non-xAI OpenAI-compatible
    provider (groq here) must still receive ``reasoning_effort``.
    """
    bare_model = "llama-3.3-70b-versatile"
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}], key=bare_model)

    await _send_reasoning_turn(mock_llm_server_url, f"groq/{bare_model}")

    body = _last_request_for(mock_llm_server_url, bare_model)
    assert body.get("reasoning_effort") == "low", (
        f"non-xAI provider lost its reasoning_effort pass-through: "
        f"{body.get('reasoning_effort')!r}"
    )
