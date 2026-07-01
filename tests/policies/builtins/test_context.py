"""Unit tests for omnigent.policies.builtins.context."""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.context import (
    _TASK_SWITCH_HISTORY_KEY,
    _strip_code_fences,
    detect_task_switch,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _event(
    message: str,
    *,
    history: list[str] | None = None,
    phase: str = "request",
) -> dict:
    return {
        "type": phase,
        "data": message,
        "session_state": {_TASK_SWITCH_HISTORY_KEY: history or []},
    }


# ── _strip_code_fences ───────────────────────────────────────────────────────


def test_strip_code_fences_plain_json() -> None:
    assert _strip_code_fences('{"verdict":"CONTINUATION"}') == '{"verdict":"CONTINUATION"}'


def test_strip_code_fences_with_fence() -> None:
    assert (
        _strip_code_fences('```json\n{"verdict":"TASK_SWITCH"}\n```')
        == '{"verdict":"TASK_SWITCH"}'
    )


def test_strip_code_fences_bare_fence() -> None:
    assert _strip_code_fences('```\n{"v":"x"}\n```') == '{"v":"x"}'


# ── non-gated phases abstain ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_request_phases_abstain() -> None:
    """Only ``request`` events are evaluated; all others abstain."""
    policy = detect_task_switch()
    for phase in ("tool_call", "tool_result", "response", "llm_request"):
        result = await policy(_event("hello", phase=phase))
        assert result is None, f"expected None for phase={phase}"


# ── accumulation (below min_turns) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_message_accumulates_no_history() -> None:
    """First message (history empty) → ALLOW and writes message into state."""
    policy = detect_task_switch(min_turns=1)
    result = await policy(_event("fix the login bug", history=[]))
    assert result is not None
    assert result["result"] == "ALLOW"
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    assert _TASK_SWITCH_HISTORY_KEY in updates
    assert "fix the login bug" in updates[_TASK_SWITCH_HISTORY_KEY][0]


@pytest.mark.asyncio
async def test_below_min_turns_accumulates_without_classifying() -> None:
    """With min_turns=2, two messages accumulate before classification fires."""
    policy = detect_task_switch(min_turns=2)
    # Message 1 — history empty
    r1 = await policy(_event("first task", history=[]))
    assert r1["result"] == "ALLOW"
    # Message 2 — one prior message, still below min_turns=2
    r2 = await policy(_event("second message", history=["first task"]))
    assert r2["result"] == "ALLOW"
    # Both must have stored the new message into state
    for r in (r1, r2):
        assert any(u["key"] == _TASK_SWITCH_HISTORY_KEY for u in r["state_updates"])


@pytest.mark.asyncio
async def test_empty_message_abstains() -> None:
    """Blank / whitespace-only messages abstain (nothing to classify)."""
    policy = detect_task_switch()
    assert await policy(_event("")) is None
    assert await policy(_event("   ")) is None


# ── no llm_client abstains ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_llm_client_abstains_after_min_turns() -> None:
    """When min_turns is satisfied but llm_client is absent, fail-open (None)."""
    policy = detect_task_switch(min_turns=1)
    event = _event("brand new topic", history=["fix the login bug"])
    # no llm_client key → abstain
    result = await policy(event)
    assert result is None


# ── CONTINUATION path (mocked llm_client) ───────────────────────────────────


class _MockLLMClient:
    """Stub PolicyLLMClient that returns a fixed verdict."""

    def __init__(self, verdict: str) -> None:
        self._verdict = verdict
        self.calls: int = 0

    async def create(self, **_kwargs: object) -> object:
        self.calls += 1

        class _Resp:
            output_text = f'{{"verdict": "{self._verdict}"}}'

        return _Resp()


@pytest.mark.asyncio
async def test_continuation_updates_history_and_allows() -> None:
    """A CONTINUATION verdict writes the new message into history and ALLOWs."""
    client = _MockLLMClient("CONTINUATION")
    policy = detect_task_switch(min_turns=1)
    event = {
        "type": "request",
        "data": "also fix the logout bug",
        "session_state": {_TASK_SWITCH_HISTORY_KEY: ["fix the login bug"]},
        "llm_client": client,
    }
    result = await policy(event)
    assert result is not None
    assert result["result"] == "ALLOW"
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    history = updates[_TASK_SWITCH_HISTORY_KEY]
    assert "fix the login bug" in history
    assert "also fix the logout bug" in history
    assert client.calls == 1


# ── TASK_SWITCH path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_switch_ask_returns_ask_and_resets_window() -> None:
    """A TASK_SWITCH verdict with action=ASK returns ASK and resets the window."""
    client = _MockLLMClient("TASK_SWITCH")
    policy = detect_task_switch(min_turns=1, action="ASK")
    event = {
        "type": "request",
        "data": "write me a poem",
        "session_state": {_TASK_SWITCH_HISTORY_KEY: ["fix the login bug"]},
        "llm_client": client,
    }
    result = await policy(event)
    assert result is not None
    assert result["result"] == "ASK"
    assert "reason" in result
    # Window must be reset to contain only the switching message
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    assert updates[_TASK_SWITCH_HISTORY_KEY] == ["write me a poem"]


@pytest.mark.asyncio
async def test_task_switch_deny_returns_deny_and_resets_window() -> None:
    """A TASK_SWITCH verdict with action=DENY returns DENY and resets the window."""
    client = _MockLLMClient("TASK_SWITCH")
    policy = detect_task_switch(min_turns=1, action="DENY")
    event = {
        "type": "request",
        "data": "write me a poem",
        "session_state": {_TASK_SWITCH_HISTORY_KEY: ["fix the login bug"]},
        "llm_client": client,
    }
    result = await policy(event)
    assert result["result"] == "DENY"
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    assert updates[_TASK_SWITCH_HISTORY_KEY] == ["write me a poem"]


# ── code-fence robustness ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fenced_json_response_is_parsed() -> None:
    """JSON wrapped in code fences is handled (provider-robustness)."""

    class _FencedClient:
        async def create(self, **_kwargs: object) -> object:
            class _R:
                output_text = '```json\n{"verdict": "CONTINUATION"}\n```'

            return _R()

    policy = detect_task_switch(min_turns=1)
    event = {
        "type": "request",
        "data": "follow-up question",
        "session_state": {_TASK_SWITCH_HISTORY_KEY: ["prior message"]},
        "llm_client": _FencedClient(),
    }
    result = await policy(event)
    assert result is not None
    assert result["result"] == "ALLOW"


# ── min_turns boundary ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_min_turns_zero_classifies_from_first_message() -> None:
    """min_turns=0 means classify even the very first message (no accumulation)."""
    client = _MockLLMClient("CONTINUATION")
    policy = detect_task_switch(min_turns=0)
    event = {
        "type": "request",
        "data": "hello",
        "session_state": {_TASK_SWITCH_HISTORY_KEY: []},
        "llm_client": client,
    }
    result = await policy(event)
    # With empty history, prior_context is empty but the call still fires
    assert client.calls == 1
    assert result is not None
