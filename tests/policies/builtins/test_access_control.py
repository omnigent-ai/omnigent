"""Unit tests for omnigent.policies.builtins.access_control — Bell-LaPadula."""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.access_control import _BLP_READ_MARK_KEY, bell_lapadula

# ── helpers ──────────────────────────────────────────────────────────────────

_LEVELS = ["public", "internal", "confidential", "secret"]
# Indices:   0         1           2               3


def _tool_call(tool: str, *, read_mark: int | None = None) -> dict:
    state = {_BLP_READ_MARK_KEY: read_mark} if read_mark is not None else {}
    return {
        "type": "tool_call",
        "target": tool,
        "data": {"name": tool, "arguments": {}},
        "context": {},
        "session_state": state,
    }


def _tool_result(tool: str, *, read_mark: int | None = None) -> dict:
    state = {_BLP_READ_MARK_KEY: read_mark} if read_mark is not None else {}
    return {
        "type": "tool_result",
        "target": tool,
        "data": {},
        "context": {},
        "session_state": state,
    }


def _other_phase(phase: str) -> dict:
    return {"type": phase, "target": "some_tool", "data": {}, "context": {}, "session_state": {}}


# ── factory validation ────────────────────────────────────────────────────────


def test_invalid_clearance_raises() -> None:
    with pytest.raises(ValueError, match="clearance"):
        bell_lapadula(levels=_LEVELS, clearance="top_secret", tool_levels={})


def test_invalid_tool_level_raises() -> None:
    with pytest.raises(ValueError, match="tool_levels"):
        bell_lapadula(
            levels=_LEVELS,
            clearance="internal",
            tool_levels={"sys_tool": "ultra_secret"},
        )


def test_write_tool_not_in_tool_levels_raises() -> None:
    with pytest.raises(ValueError, match="write_tools"):
        bell_lapadula(
            levels=_LEVELS,
            clearance="internal",
            tool_levels={"sys_read": "internal"},
            write_tools=["sys_write"],  # not in tool_levels
        )


def test_empty_levels_raises() -> None:
    with pytest.raises(ValueError, match="levels"):
        bell_lapadula(levels=[], clearance="public", tool_levels={})


# ── non-tool phases abstain ───────────────────────────────────────────────────


def test_non_tool_phases_allow() -> None:
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="public",
        tool_levels={"secret_tool": "secret"},
    )
    for phase in ("request", "response", "llm_request", "llm_response"):
        assert policy(_other_phase(phase)) == {"result": "ALLOW"}


# ── no-read-up ────────────────────────────────────────────────────────────────


def test_tool_at_clearance_level_allows() -> None:
    """Tool at exactly the agent's clearance level is allowed."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="internal",
        tool_levels={"docs_reader": "internal"},
    )
    assert policy(_tool_call("docs_reader"))["result"] == "ALLOW"


def test_tool_below_clearance_allows() -> None:
    """Tool below the agent's clearance is allowed (no read-up violation)."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="confidential",
        tool_levels={"public_api": "public"},
    )
    assert policy(_tool_call("public_api"))["result"] == "ALLOW"


def test_tool_above_clearance_denies() -> None:
    """Tool classified above the agent's clearance → DENY (no-read-up)."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="internal",
        tool_levels={"hr_system": "confidential"},
    )
    result = policy(_tool_call("hr_system"))
    assert result["result"] == "DENY"
    assert "no-read-up" in result["reason"]
    assert "confidential" in result["reason"]
    assert "internal" in result["reason"]


def test_unclassified_tool_always_allows_regardless_of_clearance() -> None:
    """A tool absent from tool_levels is unclassified and never restricted."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="public",
        tool_levels={"classified": "secret"},
    )
    assert policy(_tool_call("unclassified_tool"))["result"] == "ALLOW"


# ── tool_result advances read mark ───────────────────────────────────────────


def test_tool_result_sets_read_mark_when_higher() -> None:
    """A tool_result from a classified tool advances the read mark."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"confidential_db": "confidential"},
    )
    result = policy(_tool_result("confidential_db", read_mark=0))
    assert result["result"] == "ALLOW"
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    assert updates[_BLP_READ_MARK_KEY] == 2  # "confidential" is index 2


def test_tool_result_does_not_lower_read_mark() -> None:
    """A lower-classified tool_result must not decrease the read mark."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"public_api": "public"},
    )
    # Read mark already at confidential (2); public (0) must not lower it
    result = policy(_tool_result("public_api", read_mark=2))
    assert result == {"result": "ALLOW"}  # no state_updates


def test_unclassified_tool_result_leaves_mark_unchanged() -> None:
    """A tool_result from an unclassified tool does not touch the read mark."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"classified": "confidential"},
    )
    result = policy(_tool_result("unknown_tool"))
    assert result == {"result": "ALLOW"}


# ── no-write-down ─────────────────────────────────────────────────────────────


def test_write_tool_at_read_mark_allows() -> None:
    """Writing to a sink at exactly the read-mark level is permitted."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"db_write": "confidential"},
        write_tools=["db_write"],
    )
    # read_mark = confidential (2), write target = confidential (2) → OK
    result = policy(_tool_call("db_write", read_mark=2))
    assert result["result"] == "ALLOW"


def test_write_tool_above_read_mark_allows() -> None:
    """Writing to a higher-classified sink than the read mark is fine."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"secret_log": "secret"},
        write_tools=["secret_log"],
    )
    # read_mark = internal (1), writing to secret (3) → no write-down violation
    result = policy(_tool_call("secret_log", read_mark=1))
    assert result["result"] == "ALLOW"


def test_write_tool_below_read_mark_denies() -> None:
    """Writing to a lower-classified sink than the read mark → DENY (no-write-down)."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"shell": "public"},
        write_tools=["shell"],
    )
    # Agent read confidential (mark=2), trying to write to public shell (0)
    result = policy(_tool_call("shell", read_mark=2))
    assert result["result"] == "DENY"
    assert "no-write-down" in result["reason"]
    assert "confidential" in result["reason"]
    assert "public" in result["reason"]


def test_no_write_down_only_applies_to_write_tools() -> None:
    """A read tool below the read mark is allowed (only write sinks are checked)."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"public_api": "public"},
        # public_api is NOT in write_tools
    )
    # read_mark = confidential (2), calling public tool for reading → fine
    result = policy(_tool_call("public_api", read_mark=2))
    assert result["result"] == "ALLOW"


def test_no_read_mark_write_tool_allows() -> None:
    """Without a read mark (fresh session), write-down check always passes."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"shell": "public"},
        write_tools=["shell"],
    )
    # No read mark set → mark defaults to 0 (public) → no violation
    result = policy(_tool_call("shell"))
    assert result["result"] == "ALLOW"


# ── combined: no-read-up takes precedence over no-write-down ─────────────────


def test_above_clearance_write_tool_still_denies_via_no_read_up() -> None:
    """A write tool above clearance is denied for no-read-up, not write-down."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="internal",
        tool_levels={"secret_sink": "secret"},
        write_tools=["secret_sink"],
    )
    result = policy(_tool_call("secret_sink"))
    assert result["result"] == "DENY"
    assert "no-read-up" in result["reason"]


# ── write_tools=None disables no-write-down ───────────────────────────────────


def test_no_write_tools_disables_write_down_enforcement() -> None:
    """write_tools=None means only no-read-up is enforced."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"shell": "public"},
        write_tools=None,
    )
    # Even with a high read mark, no write-down check fires
    result = policy(_tool_call("shell", read_mark=3))
    assert result["result"] == "ALLOW"


# ── MCP prefix matching ───────────────────────────────────────────────────────


def test_mcp_prefixed_tool_call_matches_canonical_name() -> None:
    """Raw tool name with MCP prefix matches bare canonical name in tool_levels."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="internal",
        tool_levels={"hr_system": "confidential"},
    )
    # "mcp__omnigent__hr_system" should match canonical "hr_system" → DENY (no-read-up)
    event = {
        "type": "tool_call",
        "target": "mcp__omnigent__hr_system",
        "data": {"name": "mcp__omnigent__hr_system", "arguments": {}},
        "context": {},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "DENY"
    assert "no-read-up" in result["reason"]


def test_mcp_prefixed_tool_result_advances_read_mark() -> None:
    """tool_result with MCP prefix advances the read mark."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"confidential_db": "confidential"},
    )
    event = {
        "type": "tool_result",
        "target": "mcp__omnigent__confidential_db",
        "data": {},
        "context": {},
        "session_state": {_BLP_READ_MARK_KEY: 0},
    }
    result = policy(event)
    assert result["result"] == "ALLOW"
    updates = {u["key"]: u["value"] for u in result["state_updates"]}
    assert updates[_BLP_READ_MARK_KEY] == 2  # "confidential" is index 2


def test_mcp_prefixed_write_tool_triggers_no_write_down() -> None:
    """Write-down check fires when the write tool carries an MCP prefix."""
    policy = bell_lapadula(
        levels=_LEVELS,
        clearance="secret",
        tool_levels={"shell": "public"},
        write_tools=["shell"],
    )
    event = {
        "type": "tool_call",
        "target": "mcp__omnigent__shell",
        "data": {"name": "mcp__omnigent__shell", "arguments": {}},
        "context": {},
        "session_state": {_BLP_READ_MARK_KEY: 2},  # read_mark = confidential
    }
    result = policy(event)
    assert result["result"] == "DENY"
    assert "no-write-down" in result["reason"]


# ── registry ──────────────────────────────────────────────────────────────────


def test_registry_entry_present() -> None:
    from omnigent.policies.builtins.access_control import POLICY_REGISTRY

    handlers = {e["handler"] for e in POLICY_REGISTRY}
    assert "omnigent.policies.builtins.access_control.bell_lapadula" in handlers
