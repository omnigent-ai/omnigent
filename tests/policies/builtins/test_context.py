"""Unit tests for omnigent.policies.builtins.context."""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.context import (
    _TASK_SWITCH_HISTORY_KEY,
    _THRASHING_HISTORY_KEY,
    _looks_like_error,
    _strip_code_fences,
    detect_task_switch,
    detect_thrashing,
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


# ═══════════════════════════════════════════════════════════════════════════════
# detect_thrashing
# ═══════════════════════════════════════════════════════════════════════════════


def _result_event(
    result: str,
    *,
    history: list[int] | None = None,
    phase: str = "tool_result",
) -> dict:
    return {
        "type": phase,
        "data": {"result": result},
        "session_state": {_THRASHING_HISTORY_KEY: history or []},
    }


# ── _looks_like_error ───────────────────────────────────────────────────────


class TestLooksLikeError:
    def test_empty_string(self) -> None:
        assert _looks_like_error("") is False

    def test_success_result(self) -> None:
        assert _looks_like_error("file written successfully") is False

    def test_error_prefix(self) -> None:
        assert _looks_like_error("Error: file not found") is True

    def test_error_prefix_case_insensitive(self) -> None:
        assert _looks_like_error("ERROR: something went wrong") is True

    def test_traceback(self) -> None:
        assert _looks_like_error("Traceback (most recent call last)\n  File ...") is True

    def test_permission_denied(self) -> None:
        assert _looks_like_error("Permission denied: /etc/shadow") is True

    def test_no_such_file(self) -> None:
        assert _looks_like_error("No such file or directory: foo.py") is True

    def test_fatal_prefix(self) -> None:
        assert _looks_like_error("fatal: not a git repository") is True

    def test_json_error(self) -> None:
        assert _looks_like_error('{"error": "timeout"}') is True

    def test_json_non_error(self) -> None:
        assert _looks_like_error('{"result": "ok"}') is False

    def test_command_failed(self) -> None:
        assert _looks_like_error("command failed with exit code 1") is True

    def test_leading_whitespace(self) -> None:
        assert _looks_like_error("  Error: oops") is True


# ── non-gated phases abstain ─────────────────────────────────────────────────


class TestDetectThrashingPhases:
    def test_non_tool_result_phases_abstain(self) -> None:
        policy = detect_thrashing()
        for phase in ("request", "tool_call", "response", "llm_request"):
            result = policy(_result_event("error", phase=phase))
            assert result is None, f"expected None for phase={phase}"


# ── success tracking ─────────────────────────────────────────────────────────


class TestDetectThrashingSuccess:
    def test_success_records_zero(self) -> None:
        policy = detect_thrashing()
        result = policy(_result_event("file written successfully"))
        assert result is not None
        assert result["result"] == "ALLOW"
        updates = {u["key"]: u["value"] for u in result["state_updates"]}
        assert updates[_THRASHING_HISTORY_KEY] == [0]

    def test_error_records_one(self) -> None:
        policy = detect_thrashing()
        result = policy(_result_event("Error: file not found"))
        assert result is not None
        # Below threshold — still ALLOW
        assert result["result"] == "ALLOW"
        updates = {u["key"]: u["value"] for u in result["state_updates"]}
        assert updates[_THRASHING_HISTORY_KEY] == [1]


# ── consecutive threshold ────────────────────────────────────────────────────


class TestDetectThrashingConsecutive:
    def test_below_consecutive_threshold_allows(self) -> None:
        policy = detect_thrashing(consecutive_threshold=3)
        # 2 consecutive errors (below threshold of 3) → ALLOW
        result = policy(_result_event("Error: x", history=[1]))
        assert result["result"] == "ALLOW"

    def test_at_consecutive_threshold_fires(self) -> None:
        policy = detect_thrashing(consecutive_threshold=3, action="ASK")
        # 3rd consecutive error reaches threshold → ASK
        result = policy(_result_event("Error: oops", history=[1, 1]))
        assert result["result"] == "ASK"
        assert "3 consecutive" in result["reason"]

    def test_consecutive_threshold_deny(self) -> None:
        policy = detect_thrashing(consecutive_threshold=3, action="DENY")
        result = policy(_result_event("Error: oops", history=[1, 1]))
        assert result["result"] == "DENY"

    def test_success_breaks_consecutive_run(self) -> None:
        policy = detect_thrashing(consecutive_threshold=3)
        # History: error, success, error — then another error
        result = policy(_result_event("Error: x", history=[1, 0, 1]))
        # Only 2 consecutive errors (the 0 broke the run)
        assert result["result"] == "ALLOW"

    def test_consecutive_disabled_when_zero(self) -> None:
        policy = detect_thrashing(consecutive_threshold=0, window_error_rate=0.0)
        result = policy(_result_event("Error: x", history=[1, 1, 1, 1, 1]))
        assert result["result"] == "ALLOW"

    def test_consecutive_works_when_window_is_smaller(self) -> None:
        policy = detect_thrashing(consecutive_threshold=5, window=3, window_error_rate=0.0)
        # 5th consecutive error — window=3 is smaller but history retains enough
        result = policy(_result_event("Error: x", history=[1, 1, 1, 1]))
        assert result["result"] == "ASK"
        assert "5 consecutive" in result["reason"]


# ── window error rate ────────────────────────────────────────────────────────


class TestDetectThrashingWindowRate:
    def test_window_rate_fires_when_exceeded(self) -> None:
        policy = detect_thrashing(consecutive_threshold=0, window=5, window_error_rate=0.8)
        # 4 errors in history + 1 more = 5/5 = 100% > 80%
        result = policy(_result_event("Error: x", history=[1, 1, 1, 1]))
        assert result["result"] == "ASK"
        assert "100%" in result["reason"]

    def test_window_rate_fires_at_exact_threshold(self) -> None:
        policy = detect_thrashing(consecutive_threshold=0, window=5, window_error_rate=0.8)
        # 3 errors, 1 success + 1 error = 4/5 = 80% — exactly at threshold → fires
        result = policy(_result_event("Error: x", history=[1, 1, 0, 1]))
        assert result["result"] == "ASK"

    def test_window_rate_allows_when_under(self) -> None:
        policy = detect_thrashing(consecutive_threshold=0, window=5, window_error_rate=0.8)
        # 2 errors, 2 successes + 1 error = 3/5 = 60% < 80%
        result = policy(_result_event("Error: x", history=[1, 0, 1, 0]))
        assert result["result"] == "ALLOW"

    def test_window_rate_disabled_when_zero(self) -> None:
        policy = detect_thrashing(consecutive_threshold=0, window=5, window_error_rate=0.0)
        result = policy(_result_event("Error: x", history=[1, 1, 1, 1]))
        assert result["result"] == "ALLOW"

    def test_window_not_full_yet_skips_rate_check(self) -> None:
        policy = detect_thrashing(consecutive_threshold=0, window=10, window_error_rate=0.5)
        # Only 3 results, window is 10 — rate check doesn't fire
        result = policy(_result_event("Error: x", history=[1, 1]))
        assert result["result"] == "ALLOW"

    def test_rate_uses_window_not_full_history(self) -> None:
        policy = detect_thrashing(consecutive_threshold=8, window=4, window_error_rate=0.75)
        # History has 7 entries (retained because consecutive_threshold=8),
        # but rate check should use only the last window=4 results.
        # Last 4 of [..., 0, 0, 1, 1] + [1] = [0, 1, 1, 1] = 75% → fires
        result = policy(_result_event("Error: x", history=[1, 1, 1, 0, 0, 1, 1]))
        assert result["result"] == "ASK"
        assert "4 tool calls" in result["reason"]


# ── rolling window ───────────────────────────────────────────────────────────


class TestDetectThrashingWindow:
    def test_window_slides(self) -> None:
        policy = detect_thrashing(window=3, consecutive_threshold=3)
        # keep = max(3, 3) = 3 → [1, 1, 1] + [0] → keep last 3 → [1, 1, 0]
        result = policy(_result_event("ok", history=[1, 1, 1]))
        updates = {u["key"]: u["value"] for u in result["state_updates"]}
        assert updates[_THRASHING_HISTORY_KEY] == [1, 1, 0]

    def test_state_updates_always_written(self) -> None:
        policy = detect_thrashing()
        result = policy(_result_event("ok"))
        assert result is not None
        assert "state_updates" in result
        assert any(u["key"] == _THRASHING_HISTORY_KEY for u in result["state_updates"])


# ── action validation ────────────────────────────────────────────────────────


class TestDetectThrashingAction:
    def test_invalid_action_defaults_to_ask(self) -> None:
        policy = detect_thrashing(consecutive_threshold=1, action="INVALID")
        result = policy(_result_event("Error: x"))
        assert result["result"] == "ASK"

    def test_non_dict_data_abstains(self) -> None:
        policy = detect_thrashing()
        event = {"type": "tool_result", "data": "bare string", "session_state": {}}
        result = policy(event)
        assert result is None


# ── robustness ──────────────────────────────────────────────────────────────


class TestDetectThrashingRobustness:
    def test_corrupted_history_resets_to_empty(self) -> None:
        policy = detect_thrashing(consecutive_threshold=2)
        event = {
            "type": "tool_result",
            "data": {"result": "Error: x"},
            "session_state": {_THRASHING_HISTORY_KEY: {"bad": "data"}},
        }
        result = policy(event)
        assert result["result"] == "ALLOW"
        updates = {u["key"]: u["value"] for u in result["state_updates"]}
        assert updates[_THRASHING_HISTORY_KEY] == [1]

    def test_corrupted_history_list_of_strings_resets(self) -> None:
        policy = detect_thrashing(consecutive_threshold=2)
        event = {
            "type": "tool_result",
            "data": {"result": "Error: x"},
            "session_state": {_THRASHING_HISTORY_KEY: ["not", "ints"]},
        }
        result = policy(event)
        updates = {u["key"]: u["value"] for u in result["state_updates"]}
        assert updates[_THRASHING_HISTORY_KEY] == [1]

    def test_zero_window_does_not_divide_by_zero(self) -> None:
        policy = detect_thrashing(consecutive_threshold=0, window=0, window_error_rate=0.5)
        result = policy(_result_event("Error: x", history=[1, 1]))
        assert result is not None
