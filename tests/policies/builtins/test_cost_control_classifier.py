"""Tests for :func:`omnigent.policies.builtins.routing.cost_control_classifier`.

Covers:

- First request classifies and writes ``cost_control.tier`` / ``cost_control.model`` labels.
- Second request (already judged) short-circuits — no LLM call.
- Non-request event returns ``None`` (abstain).
- Missing ``llm_client`` returns ``None`` (abstain).
- LLM failure degrades gracefully (returns ALLOW, no labels).
- Empty user text returns ``None`` (abstain).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.policies.builtins.routing import (
    _CC_JUDGED_KEY,
    cost_control_classifier,
)
from omnigent.policies.schema import PolicyEvent

# ── Helpers ──────────────────────────────────────────────────────────────────


class _StubResponse:
    """Minimal response stub exposing ``output_text``.

    Production code reads ``output_text`` via :func:`_extract_response_text`.
    This stub mirrors the ``output_text`` property of a real LLM response.

    :param text: The raw text the "LLM" returned, e.g.
        ``'{"tier": "cheap"}'``.
    """

    def __init__(self, text: str) -> None:
        self.output_text = text


class _StubLLMClient:
    """Stub ``PolicyLLMClient`` that returns a fixed response.

    Does NOT use MagicMock — all attributes are explicit. Tracks
    call count so tests can verify whether the classifier invoked
    the LLM.

    :param response_text: The JSON string the "LLM" should return.
    """

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.call_count = 0

    async def create(self, **kwargs: Any) -> _StubResponse:
        """Return the fixed response and increment the call counter.

        :param kwargs: Forwarded (ignored); mirrors the real signature.
        :returns: A :class:`_StubResponse`.
        """
        self.call_count += 1
        return _StubResponse(self._response_text)


class _FailingLLMClient:
    """Stub ``PolicyLLMClient`` that raises on every call.

    Used to test graceful degradation when the classifier LLM call fails.
    Does NOT use MagicMock.
    """

    async def create(self, **kwargs: Any) -> None:
        """Always raises RuntimeError.

        :raises RuntimeError: Unconditionally.
        """
        raise RuntimeError("simulated LLM failure")


class _RaisesIfCalledLLMClient:
    """Stub that fails the test if ``create()`` is called.

    Used in tests where the classifier should short-circuit before
    reaching the LLM client.
    """

    async def create(self, **kwargs: Any) -> None:
        """Fail the test — the client should never be reached.

        :raises AssertionError: Unconditionally.
        """
        raise AssertionError(
            "LLM client was called unexpectedly — the code path that should short-circuit did not."
        )


def _request_event(
    user_text: str = "Help me refactor this module",
    *,
    llm_client: Any = None,
    session_state: dict[str, Any] | None = None,
) -> PolicyEvent:
    """Build a ``request`` event for the cost-control classifier.

    :param user_text: The user's message text, placed in ``data``.
    :param llm_client: The LLM client stub (or None).
    :param session_state: Session state dict (or None for empty).
    :returns: A ``request`` :class:`PolicyEvent`.
    """
    return {
        "type": "request",
        "target": "",
        "data": user_text,
        "context": {"actor": {}, "usage": {}},
        "session_state": session_state or {},
        "llm_client": llm_client,
    }


_CHEAP = "databricks-claude-haiku-4-5"
_EXPENSIVE = "databricks-claude-opus-4-7"
_RUBRIC = "Use expensive for multi-step coding; cheap for simple lookups."


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_request_classifies_and_writes_labels() -> None:
    """First request classifies the user message and writes tier + model labels.

    The classifier should invoke the LLM exactly once, return ALLOW,
    and include ``set_labels`` with ``cost_control.tier`` and
    ``cost_control.model`` plus a ``state_updates`` entry that gates
    future evaluations.

    A failure here means the classifier either did not call the LLM,
    did not parse the structured output, or did not emit the correct
    labels — any of which would leave the session un-routed.
    """
    client = _StubLLMClient(json.dumps({"tier": "cheap"}))
    event = _request_event(llm_client=client)

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "expensive": _EXPENSIVE},
    )
    result = await evaluate(event)

    # LLM was called exactly once for the classification.
    assert client.call_count == 1, (
        f"Expected 1 LLM call for classification, got {client.call_count}. "
        "If 0, the classifier short-circuited when it should not have."
    )
    assert result is not None, "Classifier must return a response on the first request, not None."
    # ALLOW — cost control is advisory, never blocks.
    assert result["result"] == "ALLOW", (
        f"Cost-control classifier must always ALLOW, got {result['result']!r}."
    )
    # Labels carry the tier verdict and the resolved model.
    labels = result.get("set_labels", {})
    assert labels.get("cost_control.tier") == "cheap", (
        f"Expected tier label 'cheap', got {labels.get('cost_control.tier')!r}. "
        "The classifier did not write the tier label correctly."
    )
    assert labels.get("cost_control.model") == _CHEAP, (
        f"Expected model label '{_CHEAP}', got {labels.get('cost_control.model')!r}. "
        "The tier-to-model mapping is broken."
    )
    # State update gates subsequent evaluations.
    state_updates = result.get("state_updates", [])
    judged_updates = [u for u in state_updates if u.get("key") == _CC_JUDGED_KEY]
    assert len(judged_updates) == 1, (
        f"Expected exactly 1 state_update for {_CC_JUDGED_KEY}, "
        f"got {len(judged_updates)}. Without this, the classifier will "
        "re-classify on every turn instead of once per session."
    )
    assert judged_updates[0]["value"] == "1"


@pytest.mark.asyncio
async def test_first_request_expensive_tier() -> None:
    """First request classified as 'expensive' maps to the expensive model.

    A failure here means the tier-to-model mapping is wrong for the
    expensive tier specifically.
    """
    client = _StubLLMClient(json.dumps({"tier": "expensive"}))
    event = _request_event(llm_client=client)

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "expensive": _EXPENSIVE},
    )
    result = await evaluate(event)

    assert result is not None
    labels = result.get("set_labels", {})
    assert labels.get("cost_control.tier") == "expensive"
    assert labels.get("cost_control.model") == _EXPENSIVE, (
        f"Expected model '{_EXPENSIVE}' for expensive tier, "
        f"got {labels.get('cost_control.model')!r}."
    )


@pytest.mark.asyncio
async def test_already_judged_short_circuits() -> None:
    """Second request (session already classified) short-circuits — no LLM call.

    The ``_cost_control_judged`` session state key gates re-evaluation.
    A failure here means the classifier re-classifies on every turn,
    wasting LLM calls and potentially changing the tier mid-session.
    """
    client = _RaisesIfCalledLLMClient()
    event = _request_event(
        llm_client=client,
        session_state={_CC_JUDGED_KEY: "1"},
    )

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "expensive": _EXPENSIVE},
    )
    result = await evaluate(event)

    # None = abstain; the once-per-session gate fired.
    assert result is None, (
        f"Expected None (abstain) for already-judged session, got {result!r}. "
        "The classifier did not check the session_state gate."
    )


@pytest.mark.asyncio
async def test_non_request_event_returns_none() -> None:
    """Non-request events (e.g. tool_call) are ignored — returns None.

    The classifier only fires on ``request`` events. A failure here
    means the classifier processes event types it should ignore.
    """
    client = _RaisesIfCalledLLMClient()
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "some_tool",
        "data": {"name": "some_tool", "arguments": {}},
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
        "llm_client": client,
    }

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "expensive": _EXPENSIVE},
    )
    result = await evaluate(event)

    assert result is None, (
        f"Expected None for non-request event type, got {result!r}. "
        "The classifier should only fire on type='request'."
    )


@pytest.mark.asyncio
async def test_missing_llm_client_returns_none() -> None:
    """Missing llm_client (no server llm: config) returns None.

    Without a PolicyLLMClient the classifier cannot classify. It
    should abstain rather than crash. A failure here means the
    classifier does not guard against a None llm_client.
    """
    event = _request_event(llm_client=None)

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "expensive": _EXPENSIVE},
    )
    result = await evaluate(event)

    assert result is None, (
        f"Expected None when llm_client is None, got {result!r}. "
        "The classifier should abstain when no LLM client is available."
    )


@pytest.mark.asyncio
async def test_llm_failure_degrades_gracefully() -> None:
    """LLM call failure returns ALLOW with no labels.

    The classifier must not crash the policy engine on transient
    LLM errors. It degrades to ALLOW (no routing) so the session
    runs on the agent's declared model. A failure here means the
    classifier propagates the exception instead of catching it.
    """
    client = _FailingLLMClient()
    event = _request_event(llm_client=client)

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "expensive": _EXPENSIVE},
    )
    result = await evaluate(event)

    assert result is not None, "On LLM failure the classifier must return ALLOW, not None."
    assert result["result"] == "ALLOW", (
        f"On LLM failure the classifier must ALLOW, got {result['result']!r}."
    )
    # No labels — the classification did not complete, so no routing.
    assert (
        "set_labels" not in result or result["set_labels"] is None or result["set_labels"] == {}
    ), (
        f"On LLM failure there must be no labels, got {result.get('set_labels')!r}. "
        "Emitting labels on a failed classification would route to a "
        "tier chosen by garbage data."
    )


@pytest.mark.asyncio
async def test_empty_user_text_returns_none() -> None:
    """Empty (whitespace-only) user text returns None.

    An empty message has nothing to classify. A failure here means
    the classifier sends an empty string to the LLM, wasting a call.
    """
    client = _RaisesIfCalledLLMClient()
    event = _request_event(user_text="   ", llm_client=client)

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "expensive": _EXPENSIVE},
    )
    result = await evaluate(event)

    assert result is None, (
        f"Expected None for empty user text, got {result!r}. "
        "The classifier should not call the LLM for empty messages."
    )


@pytest.mark.asyncio
async def test_harness_written_when_configured() -> None:
    """When a tier has a harness, the classifier writes ``cost_control.harness``.

    Cross-harness routing requires the classifier to write the harness
    label so the sessions route can inject ``harness_override`` into
    the runner body. A failure here means the runner stays on the
    spec's declared harness regardless of the tier.
    """
    client = _StubLLMClient(json.dumps({"tier": "cheap"}))
    event = _request_event(llm_client=client)

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={
            "cheap": {"model": _CHEAP, "harness": "pi"},
            "expensive": {"model": _EXPENSIVE, "harness": "claude-sdk"},
        },
    )
    result = await evaluate(event)

    assert result is not None
    labels = result.get("set_labels", {})
    # The cheap tier was selected, so the cheap harness should be written.
    assert labels.get("cost_control.harness") == "pi", (
        f"Expected harness label 'pi', got {labels.get('cost_control.harness')!r}. "
        "The classifier did not write the harness for the cheap tier."
    )


@pytest.mark.asyncio
async def test_harness_omitted_when_not_configured() -> None:
    """When no tier harness is configured, ``cost_control.harness`` is absent.

    Model-only routing (no cross-harness) should not emit a harness
    label. A failure here means the runner would try to respawn onto
    an empty/None harness string.
    """
    client = _StubLLMClient(json.dumps({"tier": "cheap"}))
    event = _request_event(llm_client=client)

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "expensive": _EXPENSIVE},
        # Bare strings — no harness, model-only routing.
    )
    result = await evaluate(event)

    assert result is not None
    labels = result.get("set_labels", {})
    assert "cost_control.harness" not in labels, (
        f"Expected no harness label for model-only routing, "
        f"but got {labels.get('cost_control.harness')!r}."
    )


_MEDIUM = "databricks-claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_three_tier_classification() -> None:
    """Three-tier config routes to the middle tier when the LLM picks it.

    Proves the classifier isn't hardcoded to cheap/expensive — it
    accepts arbitrary tier names from the ``tiers`` dict. A failure
    here means the classifier rejects valid tier names outside the
    original two.
    """
    client = _StubLLMClient(json.dumps({"tier": "medium"}))
    event = _request_event(llm_client=client)

    evaluate = cost_control_classifier(
        rubric=_RUBRIC,
        tiers={"cheap": _CHEAP, "medium": _MEDIUM, "expensive": _EXPENSIVE},
    )
    result = await evaluate(event)

    assert result is not None
    labels = result.get("set_labels", {})
    assert labels.get("cost_control.tier") == "medium", (
        f"Expected tier 'medium', got {labels.get('cost_control.tier')!r}."
    )
    assert labels.get("cost_control.model") == _MEDIUM, (
        f"Expected model '{_MEDIUM}', got {labels.get('cost_control.model')!r}."
    )
