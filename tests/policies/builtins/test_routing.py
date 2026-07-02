"""
Tests for :mod:`omnigent.policies.builtins.routing`.

Covers:

- ``deny_trivial_to_expensive_model`` factory returns an async callable.
- TRIVIAL classification denies the call with a reason and caches.
- COMPLEX classification allows with a cache state_update.
- Cached TRIVIAL/COMPLEX skip the classifier call.
- Non-expensive models are not gated.
- Non-``llm_request`` events are abstained on.
- Missing ``llm_client`` abstains with a warning.
- Empty / missing ``last_user_message`` abstains.
- Classification failure (exception) abstains.
- Structured output schema is forwarded via ``text`` kwarg.
- Registry entry is well-formed with ``expensive_models`` required.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.policies.builtins.routing import (
    _CACHE_KEY_PREFIX,
    _CLASSIFICATION_SCHEMA,
    _INTENT_CHECK_PREFIX,
    _INTENT_KEY,
    POLICY_REGISTRY,
    deny_trivial_to_expensive_model,
    intent_gate,
)

from .helpers import llm_request_event

# ── Helpers ──────────────────────────────────────────────────────────────────

_EXPENSIVE = ["databricks-claude-opus-4-6", "openai/o3"]


class _FakeResponse:
    """
    Minimal stand-in for ``omnigent.llms.types.Response``.

    Exposes ``output_text`` which is what the routing policy reads.
    For structured output, the text is a JSON string.

    :param output_text: The text the classifier "returned", e.g.
        ``'{"difficulty": "TRIVIAL"}'``.
    """

    def __init__(self, output_text: str) -> None:
        self.output_text = output_text


def _trivial_response() -> _FakeResponse:
    """Build a structured TRIVIAL classification response.

    :returns: A :class:`_FakeResponse` with
        ``'{"difficulty": "TRIVIAL"}'``.
    """
    return _FakeResponse(json.dumps({"difficulty": "TRIVIAL"}))


def _complex_response() -> _FakeResponse:
    """Build a structured COMPLEX classification response.

    :returns: A :class:`_FakeResponse` with
        ``'{"difficulty": "COMPLEX"}'``.
    """
    return _FakeResponse(json.dumps({"difficulty": "COMPLEX"}))


class _FakePolicyLLMClient:
    """
    Stub ``PolicyLLMClient`` that returns a fixed response.

    Does not use MagicMock — attributes are explicit.

    :param response: The :class:`_FakeResponse` to return from
        ``create()``.
    """

    def __init__(self, response: _FakeResponse) -> None:
        self._mock_create = AsyncMock(return_value=response)

    async def create(self, **kwargs: Any) -> _FakeResponse:
        """Forward to the mock so tests can assert on calls.

        :param kwargs: Forwarded to the mock.
        :returns: A :class:`_FakeResponse`.
        """
        return await self._mock_create(**kwargs)


def _llm_request_with_client(
    client: _FakePolicyLLMClient | None,
    *,
    model: str = "databricks-claude-opus-4-6",
    last_user_message: str = "What is 2+2?",
) -> dict[str, Any]:
    """
    Build an ``llm_request`` event with an ``llm_client`` attached.

    :param client: The fake LLM client (or ``None``).
    :param model: Model name in the event data.
    :param last_user_message: The user message to classify.
    :returns: An event dict with ``llm_client`` set.
    """
    event = llm_request_event(
        model=model,
        last_user_message=last_user_message,
    )
    event["llm_client"] = client
    return event


# ── Factory ──────────────────────────────────────────────────────────────────


def test_factory_returns_callable() -> None:
    """
    ``deny_trivial_to_expensive_model(expensive_models=...)`` returns
    a callable.

    What breaks if this fails: the factory is not producing the
    inner evaluate function, so the policy engine can't invoke it.
    """
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)
    assert callable(policy)


def test_factory_requires_expensive_models() -> None:
    """
    ``deny_trivial_to_expensive_model()`` without
    ``expensive_models`` raises ``TypeError``.

    What breaks if this fails: the factory silently uses an empty
    gate list, which would never deny anything.
    """
    with pytest.raises(TypeError, match="expensive_models"):
        deny_trivial_to_expensive_model()  # type: ignore[call-arg]


# ── TRIVIAL classification → DENY ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_trivial_classification_denies() -> None:
    """
    When the classifier responds with ``{"difficulty": "TRIVIAL"}``
    and the model is expensive, the policy returns DENY with a
    reason.

    What breaks if this fails: trivial tasks are not blocked from
    expensive models — the whole point of this policy.
    """
    client = _FakePolicyLLMClient(_trivial_response())
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(client, model="databricks-claude-opus-4-6")
    result = await policy(event)

    assert result is not None
    assert result["result"] == "DENY"
    assert "trivial" in result["reason"].lower()
    assert "databricks-claude-opus-4-6" in result["reason"]
    # Caches the result in session_state.
    assert result["state_updates"][0]["action"] == "set"
    assert result["state_updates"][0]["value"] == "TRIVIAL"
    client._mock_create.assert_awaited_once()


# ── COMPLEX classification → abstain ────────────────────────────────────────


@pytest.mark.asyncio
async def test_complex_classification_allows_and_caches() -> None:
    """
    When the classifier responds with ``{"difficulty": "COMPLEX"}``,
    the policy returns ALLOW with a ``state_updates`` entry to cache
    the result.

    What breaks if this fails: legitimate complex tasks are
    incorrectly blocked, or the cache is not populated so the next
    round-trip re-classifies.
    """
    client = _FakePolicyLLMClient(_complex_response())
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(client)
    result = await policy(event)

    assert result is not None
    assert result["result"] == "ALLOW"
    assert result["state_updates"][0]["action"] == "set"
    assert result["state_updates"][0]["value"] == "COMPLEX"


# ── Non-expensive model → skip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_expensive_model_abstains() -> None:
    """
    When the model is not in the ``expensive_models`` list, the
    policy abstains without classifying.

    What breaks if this fails: cheap models are unnecessarily
    gated, wasting a classification call on every request.
    """
    client = _FakePolicyLLMClient(_trivial_response())
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(client, model="openai/gpt-4o-mini")
    result = await policy(event)

    assert result is None
    # No classification call was made — skipped early.
    client._mock_create.assert_not_awaited()


# ── Malformed responses ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_json_abstains() -> None:
    """
    When the classifier returns non-JSON text, the policy abstains
    (the ``json.loads`` fails, caught by the exception handler).

    What breaks if this fails: malformed classifier output causes
    an unhandled exception that propagates to the engine.
    """
    client = _FakePolicyLLMClient(_FakeResponse("not json"))
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(client)
    result = await policy(event)

    assert result is None


@pytest.mark.asyncio
async def test_missing_difficulty_key_abstains() -> None:
    """
    When the classifier returns valid JSON but without the
    ``difficulty`` key, the policy abstains.

    What breaks if this fails: a ``KeyError`` propagates or the
    policy incorrectly denies.
    """
    client = _FakePolicyLLMClient(_FakeResponse(json.dumps({"other": "value"})))
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(client)
    result = await policy(event)

    assert result is None


# ── Non-llm_request phases ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_llm_request_phase_abstains() -> None:
    """
    Non-``llm_request`` events are abstained on — the policy only
    fires on ``llm_request``.

    What breaks if this fails: the policy interferes with tool
    calls, user messages, or other phases.
    """
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    for phase in ("request", "tool_call", "tool_result", "response"):
        event: dict[str, Any] = {
            "type": phase,
            "target": None,
            "data": "hello",
            "context": {"actor": {}, "usage": {}},
            "session_state": {},
        }
        result = await policy(event)
        assert result is None, f"Expected None for phase {phase!r}"


# ── Missing llm_client ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_llm_client_abstains() -> None:
    """
    When ``llm_client`` is ``None`` (server has no ``llm:`` config),
    the policy abstains.

    What breaks if this fails: the policy crashes with
    ``AttributeError`` instead of gracefully degrading.
    """
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(None)
    result = await policy(event)

    assert result is None


# ── Edge cases ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_user_message_abstains() -> None:
    """
    When ``last_user_message`` is empty, the policy abstains —
    nothing to classify.

    What breaks if this fails: the policy sends an empty string
    to the classifier LLM, wasting a call and getting a garbage
    response.
    """
    client = _FakePolicyLLMClient(_trivial_response())
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(client, last_user_message="")
    result = await policy(event)

    assert result is None
    client._mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_classification_failure_abstains() -> None:
    """
    When the classifier call raises, the policy abstains (fail
    open) rather than blocking the request.

    What breaks if this fails: a transient LLM error blocks ALL
    agent requests to expensive models.
    """
    client = _FakePolicyLLMClient(_trivial_response())
    client._mock_create.side_effect = RuntimeError("LLM timeout")

    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)
    event = _llm_request_with_client(client)
    result = await policy(event)

    assert result is None


# ── Structured output forwarding ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_structured_output_schema_forwarded() -> None:
    """
    The classifier call passes ``text=_CLASSIFICATION_SCHEMA`` so
    the LLM is constrained to respond with structured JSON.

    What breaks if this fails: the LLM returns free-text instead
    of ``{"difficulty": "TRIVIAL"|"COMPLEX"}``, making parsing
    fragile.
    """
    client = _FakePolicyLLMClient(_trivial_response())
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(client)
    await policy(event)

    call_kwargs = client._mock_create.call_args.kwargs
    assert call_kwargs["text"] is _CLASSIFICATION_SCHEMA


@pytest.mark.asyncio
async def test_custom_classification_prompt_forwarded() -> None:
    """
    The custom ``classification_prompt`` is passed as
    ``instructions`` to the LLM client.

    What breaks if this fails: custom prompts are silently ignored,
    so operators can't tune the classifier.
    """
    client = _FakePolicyLLMClient(_complex_response())
    custom_prompt = "Classify the task difficulty."
    policy = deny_trivial_to_expensive_model(
        expensive_models=_EXPENSIVE,
        classification_prompt=custom_prompt,
    )

    event = _llm_request_with_client(client)
    await policy(event)

    call_kwargs = client._mock_create.call_args.kwargs
    assert call_kwargs["instructions"] == custom_prompt


# ── Cache hits ───────────────────────────────────────────────────────────────


def _cache_key_for(message: str) -> str:
    """Compute the session_state cache key for a message.

    :param message: The user message text.
    :returns: The cache key, e.g.
        ``"_routing_classification:a1b2c3..."``.
    """
    h = hashlib.sha256(message.encode()).hexdigest()[:16]
    return f"{_CACHE_KEY_PREFIX}{h}"


@pytest.mark.asyncio
async def test_cached_trivial_denies_without_llm_call() -> None:
    """
    When ``session_state`` already has a TRIVIAL cache entry for
    the message, the policy denies without calling the classifier.

    What breaks if this fails: every round-trip in a turn (and
    every turn with the same message) makes a redundant LLM call.
    """
    client = _FakePolicyLLMClient(_complex_response())
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    msg = "What is 2+2?"
    event = _llm_request_with_client(client, last_user_message=msg)
    event["session_state"] = {_cache_key_for(msg): "TRIVIAL"}

    result = await policy(event)

    assert result is not None
    assert result["result"] == "DENY"
    # No classification call — served from cache.
    client._mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_complex_allows_without_llm_call() -> None:
    """
    When ``session_state`` already has a COMPLEX cache entry for
    the message, the policy abstains without calling the classifier.

    What breaks if this fails: same redundant-call issue as above.
    """
    client = _FakePolicyLLMClient(_trivial_response())
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    msg = "Refactor the entire auth system"
    event = _llm_request_with_client(client, last_user_message=msg)
    event["session_state"] = {_cache_key_for(msg): "COMPLEX"}

    result = await policy(event)

    assert result is None
    client._mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_message_not_cached() -> None:
    """
    A cache entry for message A does not affect message B — the
    cache is keyed by message hash.

    What breaks if this fails: a stale cache entry from a prior
    turn incorrectly gates a new message.
    """
    client = _FakePolicyLLMClient(_complex_response())
    policy = deny_trivial_to_expensive_model(expensive_models=_EXPENSIVE)

    event = _llm_request_with_client(client, last_user_message="New question")
    event["session_state"] = {_cache_key_for("Old question"): "TRIVIAL"}

    result = await policy(event)

    # Not cached — the classifier was called.
    client._mock_create.assert_awaited_once()
    # COMPLEX → ALLOW with cache update.
    assert result is not None
    assert result["result"] == "ALLOW"


# ── Registry ─────────────────────────────────────────────────────────────────


def test_registry_entry_well_formed() -> None:
    """
    The ``POLICY_REGISTRY`` has entries with the expected handler
    paths and kinds, and ``expensive_models`` is required for
    ``deny_trivial_to_expensive_model``.

    What breaks if this fails: the server startup scan won't
    discover the routing policy, or operators can omit the
    required ``expensive_models`` parameter.
    """
    handlers = {e["handler"] for e in POLICY_REGISTRY}
    assert "omnigent.policies.builtins.routing.deny_trivial_to_expensive_model" in handlers
    assert "omnigent.policies.builtins.routing.intent_gate" in handlers

    trivial_entry = next(
        e
        for e in POLICY_REGISTRY
        if e["handler"] == "omnigent.policies.builtins.routing.deny_trivial_to_expensive_model"
    )
    assert trivial_entry["kind"] == "factory"
    schema = trivial_entry["params_schema"]
    assert "expensive_models" in schema["properties"]
    assert "expensive_models" in schema["required"]

    intent_entry = next(
        e
        for e in POLICY_REGISTRY
        if e["handler"] == "omnigent.policies.builtins.routing.intent_gate"
    )
    assert intent_entry["kind"] == "factory"
    assert intent_entry["params_schema"]["required"] == []


# ── intent_gate ───────────────────────────────────────────────────────────────


def _request_event(message: str, *, state: dict | None = None) -> dict[str, Any]:
    return {
        "type": "request",
        "target": None,
        "data": message,
        "context": {},
        "session_state": state or {},
    }


def _tool_call_event(
    tool: str,
    args: dict | None = None,
    *,
    state: dict | None = None,
    llm_client: Any = None,
) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "target": tool,
        "data": {"name": tool, "arguments": args or {}},
        "context": {},
        "session_state": state or {},
        "llm_client": llm_client,
    }


def _on_task_response() -> _FakeResponse:
    return _FakeResponse(json.dumps({"verdict": "ON_TASK"}))


def _off_task_response() -> _FakeResponse:
    return _FakeResponse(json.dumps({"verdict": "OFF_TASK"}))


@pytest.mark.asyncio
async def test_intent_gate_captures_first_request() -> None:
    """First request records intent in session_state."""
    policy = intent_gate()
    result = await policy(_request_event("fix the login bug"))
    assert result is not None
    assert result["result"] == "ALLOW"
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    assert updates[_INTENT_KEY] == "fix the login bug"


@pytest.mark.asyncio
async def test_intent_gate_ignores_subsequent_requests() -> None:
    """Once intent is recorded, further request events are ignored."""
    policy = intent_gate()
    result = await policy(
        _request_event(
            "now write a poem",
            state={_INTENT_KEY: "fix the login bug"},
        )
    )
    assert result is None


@pytest.mark.asyncio
async def test_intent_gate_empty_request_abstains() -> None:
    """Blank first message abstains — nothing to record."""
    policy = intent_gate()
    assert await policy(_request_event("   ")) is None


@pytest.mark.asyncio
async def test_intent_gate_non_tool_phases_abstain() -> None:
    """Non-request, non-tool_call phases are ignored."""
    policy = intent_gate()
    for phase in ("tool_result", "response", "llm_request", "llm_response"):
        event: dict[str, Any] = {
            "type": phase,
            "target": None,
            "data": {},
            "context": {},
            "session_state": {_INTENT_KEY: "fix bug"},
        }
        assert await policy(event) is None, f"expected None for phase={phase}"


@pytest.mark.asyncio
async def test_intent_gate_on_task_allows_and_caches() -> None:
    """ON_TASK verdict allows and caches in session_state."""
    client = _FakePolicyLLMClient(_on_task_response())
    policy = intent_gate()
    result = await policy(
        _tool_call_event(
            "read_file",
            state={_INTENT_KEY: "fix the login bug"},
            llm_client=client,
        )
    )
    assert result is not None
    assert result["result"] == "ALLOW"
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    cache_key = next(k for k in updates if k.startswith(_INTENT_CHECK_PREFIX))
    assert updates[cache_key] == "ON_TASK"
    client._mock_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_intent_gate_off_task_denies_and_caches() -> None:
    """OFF_TASK verdict denies with reason and caches."""
    client = _FakePolicyLLMClient(_off_task_response())
    policy = intent_gate()
    result = await policy(
        _tool_call_event(
            "send_email",
            state={_INTENT_KEY: "fix the login bug"},
            llm_client=client,
        )
    )
    assert result is not None
    assert result["result"] == "DENY"
    assert "send_email" in result["reason"]
    assert "fix the login bug" in result["reason"]
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    cache_key = next(k for k in updates if k.startswith(_INTENT_CHECK_PREFIX))
    assert updates[cache_key] == "OFF_TASK"


@pytest.mark.asyncio
async def test_intent_gate_cached_on_task_skips_llm() -> None:
    """Cached ON_TASK allows without calling the classifier."""
    client = _FakePolicyLLMClient(_off_task_response())
    policy = intent_gate()

    intent = "fix the login bug"
    tool = "read_file"
    args_repr = json.dumps({}, sort_keys=True, default=str)
    check_hash = hashlib.sha256(f"{intent}\x00{tool}\x00{args_repr}".encode()).hexdigest()[:16]
    cache_key = f"{_INTENT_CHECK_PREFIX}{check_hash}"

    result = await policy(
        _tool_call_event(
            tool,
            state={_INTENT_KEY: intent, cache_key: "ON_TASK"},
            llm_client=client,
        )
    )
    assert result is None
    client._mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_intent_gate_cached_off_task_denies_without_llm() -> None:
    """Cached OFF_TASK denies without calling the classifier."""
    client = _FakePolicyLLMClient(_on_task_response())
    policy = intent_gate()

    intent = "fix the login bug"
    tool = "send_email"
    args_repr = json.dumps({}, sort_keys=True, default=str)
    check_hash = hashlib.sha256(f"{intent}\x00{tool}\x00{args_repr}".encode()).hexdigest()[:16]
    cache_key = f"{_INTENT_CHECK_PREFIX}{check_hash}"

    result = await policy(
        _tool_call_event(
            tool,
            state={_INTENT_KEY: intent, cache_key: "OFF_TASK"},
            llm_client=client,
        )
    )
    assert result is not None
    assert result["result"] == "DENY"
    client._mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_intent_gate_no_intent_abstains() -> None:
    """tool_call without a stored intent abstains (fail-open)."""
    client = _FakePolicyLLMClient(_off_task_response())
    policy = intent_gate()
    result = await policy(_tool_call_event("send_email", llm_client=client))
    assert result is None
    client._mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_intent_gate_no_llm_client_abstains() -> None:
    """tool_call with no llm_client abstains (fail-open)."""
    policy = intent_gate()
    result = await policy(
        _tool_call_event(
            "send_email",
            state={_INTENT_KEY: "fix the login bug"},
            llm_client=None,
        )
    )
    assert result is None


@pytest.mark.asyncio
async def test_intent_gate_classifier_failure_abstains() -> None:
    """LLM exception during classification abstains (fail-open)."""
    client = _FakePolicyLLMClient(_on_task_response())
    client._mock_create.side_effect = RuntimeError("timeout")
    policy = intent_gate()
    result = await policy(
        _tool_call_event(
            "read_file",
            state={_INTENT_KEY: "fix the login bug"},
            llm_client=client,
        )
    )
    assert result is None
