"""Tests for the ``detect_loop`` builtin policy.

Covers:

- Basic loop detection when a tool call repeats ≥ threshold times.
- Distinct calls within the window do not trigger.
- Sliding window eviction: old entries drop off and no longer count.
- Different arguments produce different hashes (no false positives).
- Non-tool_call phases pass through.
- State updates are always emitted (the window advances on every call).
- Custom window/threshold parameters.
"""

from __future__ import annotations

from omnigent.policies.builtins.safety import _LOOP_STATE_KEY, _args_hash, detect_loop
from tests.policies.builtins.helpers import tool_call_event as tc


def _state_with_hashes(hashes: list[str]) -> dict:
    return {_LOOP_STATE_KEY: list(hashes)}


# ── Basic detection ─────────────────────────────────────────────────────────


def test_detect_loop_triggers_on_repeated_calls() -> None:
    """Three identical calls in a row trigger ASK."""
    policy = detect_loop(window=10, threshold=3)
    h = _args_hash("sys_os_shell", {"command": "ls"})

    result = policy(tc("sys_os_shell", {"command": "ls"}, _state_with_hashes([h, h])))
    assert result["result"] == "ASK"
    assert "retry loop" in result["reason"]
    assert "sys_os_shell" in result["reason"]


def test_detect_loop_allows_below_threshold() -> None:
    """Two identical calls (below threshold=3) are allowed."""
    policy = detect_loop(window=10, threshold=3)
    h = _args_hash("sys_os_shell", {"command": "ls"})

    result = policy(tc("sys_os_shell", {"command": "ls"}, _state_with_hashes([h])))
    assert result["result"] == "ALLOW"


def test_detect_loop_first_call_allows() -> None:
    """The very first call (empty state) is allowed."""
    policy = detect_loop(window=10, threshold=3)
    result = policy(tc("sys_os_shell", {"command": "ls"}))
    assert result["result"] == "ALLOW"


# ── Different args ──────────────────────────────────────────────────────────


def test_detect_loop_different_args_no_trigger() -> None:
    """Same tool with different arguments does not trigger."""
    policy = detect_loop(window=10, threshold=3)
    h1 = _args_hash("sys_os_shell", {"command": "ls"})
    h2 = _args_hash("sys_os_shell", {"command": "pwd"})

    result = policy(tc("sys_os_shell", {"command": "cat foo"}, _state_with_hashes([h1, h2])))
    assert result["result"] == "ALLOW"


def test_detect_loop_different_tools_no_trigger() -> None:
    """Different tools with same arguments do not trigger."""
    policy = detect_loop(window=10, threshold=3)
    h1 = _args_hash("Read", {"path": "/tmp/f"})
    h2 = _args_hash("Write", {"path": "/tmp/f"})

    result = policy(tc("Edit", {"path": "/tmp/f"}, _state_with_hashes([h1, h2])))
    assert result["result"] == "ALLOW"


# ── Sliding window ──────────────────────────────────────────────────────────


def test_detect_loop_window_eviction() -> None:
    """Old entries outside the window no longer count.

    With window=4 and threshold=3, two old matching hashes plus
    two intervening different calls means only one match remains
    in the window when the third identical call arrives.
    """
    policy = detect_loop(window=4, threshold=3)
    h_target = _args_hash("Bash", {"command": "fail"})
    h_other = _args_hash("Read", {"path": "x"})

    # History: [target, target, other, other] — window=4 keeps all four.
    # The current call adds a third target, but the window trims to last 4:
    # [target, other, other, target] → only 2 matches, below threshold=3.
    state = _state_with_hashes([h_target, h_target, h_other, h_other])
    result = policy(tc("Bash", {"command": "fail"}, state))
    assert result["result"] == "ALLOW"


def test_detect_loop_window_keeps_recent() -> None:
    """Matches within the window still trigger.

    With window=4, threshold=3: history is [other, target, target],
    current call is target → window is [other, target, target, target]
    → 3 matches → ASK.
    """
    policy = detect_loop(window=4, threshold=3)
    h_target = _args_hash("Bash", {"command": "fail"})
    h_other = _args_hash("Read", {"path": "x"})

    state = _state_with_hashes([h_other, h_target, h_target])
    result = policy(tc("Bash", {"command": "fail"}, state))
    assert result["result"] == "ASK"


# ── State updates ───────────────────────────────────────────────────────────


def test_detect_loop_emits_state_updates_on_allow() -> None:
    """ALLOW results still carry state_updates to advance the window."""
    policy = detect_loop(window=10, threshold=3)
    result = policy(tc("web_search", {"query": "hello"}))
    assert result["result"] == "ALLOW"
    updates = result.get("state_updates", [])
    assert len(updates) == 1
    assert updates[0]["key"] == _LOOP_STATE_KEY
    assert updates[0]["action"] == "set"


def test_detect_loop_emits_state_updates_on_ask() -> None:
    """ASK results carry state_updates too (the window still advances)."""
    policy = detect_loop(window=10, threshold=3)
    h = _args_hash("Bash", {"command": "fail"})
    state = _state_with_hashes([h, h])
    result = policy(tc("Bash", {"command": "fail"}, state))
    assert result["result"] == "ASK"
    updates = result.get("state_updates", [])
    assert len(updates) == 1
    assert updates[0]["key"] == _LOOP_STATE_KEY


def test_detect_loop_window_trimmed_in_state() -> None:
    """The state_updates value is trimmed to the window size."""
    policy = detect_loop(window=3, threshold=3)
    h = _args_hash("Bash", {"command": "x"})
    # Pre-fill with 5 entries — more than the window.
    state = _state_with_hashes([h, h, h, h, h])
    result = policy(tc("Bash", {"command": "x"}, state))
    updates = result.get("state_updates", [])
    stored = updates[0]["value"]
    assert len(stored) == 3


# ── Phase filtering ─────────────────────────────────────────────────────────


def test_detect_loop_ignores_non_tool_call_phase() -> None:
    """Non-tool_call phases pass through with ALLOW."""
    policy = detect_loop()
    result = policy(
        {
            "type": "response",
            "target": None,
            "data": "some response",
            "context": {"actor": {}, "usage": {}},
            "session_state": {},
        }
    )
    assert result["result"] == "ALLOW"


def test_detect_loop_ignores_non_dict_data() -> None:
    """tool_call with non-dict data passes through."""
    policy = detect_loop()
    result = policy(
        {
            "type": "tool_call",
            "target": None,
            "data": "not a dict",
            "context": {"actor": {}, "usage": {}},
            "session_state": {},
        }
    )
    assert result["result"] == "ALLOW"


# ── Custom parameters ──────────────────────────────────────────────────────


def test_detect_loop_custom_threshold() -> None:
    """Higher threshold requires more repeats."""
    policy = detect_loop(window=20, threshold=5)
    h = _args_hash("Bash", {"command": "x"})

    # 4 prior calls + current = 5 → triggers at threshold=5.
    state = _state_with_hashes([h, h, h, h])
    result = policy(tc("Bash", {"command": "x"}, state))
    assert result["result"] == "ASK"

    # 3 prior + current = 4 → below threshold=5.
    state = _state_with_hashes([h, h, h])
    result = policy(tc("Bash", {"command": "x"}, state))
    assert result["result"] == "ALLOW"


# ── Args hash determinism ──────────────────────────────────────────────────


def test_args_hash_deterministic() -> None:
    """Same inputs produce the same hash."""
    h1 = _args_hash("tool", {"a": 1, "b": "x"})
    h2 = _args_hash("tool", {"b": "x", "a": 1})
    assert h1 == h2


def test_args_hash_different_tools() -> None:
    """Different tool names produce different hashes."""
    h1 = _args_hash("tool_a", {"x": 1})
    h2 = _args_hash("tool_b", {"x": 1})
    assert h1 != h2


def test_args_hash_different_args() -> None:
    """Different arguments produce different hashes."""
    h1 = _args_hash("tool", {"x": 1})
    h2 = _args_hash("tool", {"x": 2})
    assert h1 != h2
