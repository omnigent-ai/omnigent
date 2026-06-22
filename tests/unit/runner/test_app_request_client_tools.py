"""Tests for the runner-native client-side tool plumbing helpers.

``_merge_request_client_tools`` lets request-supplied client-side tools
reach non-native harnesses: the session path otherwise builds the harness
tool list from the agent spec's builtin + MCP schemas only, so the model
never sees a REPL's ``Read`` / ``Write`` / ``Glob`` and can't tunnel a
client-side call. ``_should_dispatch_tool_locally`` then decides, per tool
call, whether the runner dispatches locally or relays the
``action_required`` event upstream to tunnel. These tests fail loudly if a
refactor drops the merge, lets a request tool shadow a policy-enforced
builtin, or regresses the client-side dispatch bypass.
"""

from __future__ import annotations

from typing import Any

from omnigent.runner.app import (
    TurnDispatch,
    _merge_request_client_tools,
    _schema_tool_name,
    _should_dispatch_tool_locally,
)


def _schema(name: str) -> dict[str, Any]:
    """Nested OpenAI function-tool schema, as produced by Tool.get_schema()."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_client_tools_appended_after_spec_tools() -> None:
    """Client tools land AFTER the spec builtins, preserving both.

    A failure here means the merge dropped one side: if the spec
    builtins vanish the agent loses its system tools; if the client
    tools vanish (the bug this fix addresses) the model can never
    invoke ``Read`` / ``Glob`` and the call never tunnels.
    """
    spec = [_schema("load_skill"), _schema("sys_session_send")]
    client = [_schema("Read"), _schema("Glob")]

    merged = _merge_request_client_tools(spec, client)

    assert [_schema_tool_name(t) for t in merged] == [
        "load_skill",
        "sys_session_send",
        "Read",
        "Glob",
    ]


def test_builtin_wins_on_name_clash() -> None:
    """A request tool may not shadow a builtin of the same name.

    If this fails, a caller could override a policy-enforced
    server-side builtin (e.g. ``sys_os_read``) with an unguarded
    client-side definition — the merge must keep the builtin and
    drop the colliding client tool.
    """
    spec = [_schema("sys_os_read")]
    client = [_schema("sys_os_read"), _schema("Read")]

    merged = _merge_request_client_tools(spec, client)

    names = [_schema_tool_name(t) for t in merged]
    # Builtin kept exactly once (the spec copy); the colliding client
    # copy dropped; the non-colliding client tool appended.
    assert names == ["sys_os_read", "Read"]
    assert names.count("sys_os_read") == 1
    # The retained sys_os_read is the spec object, not the client one.
    assert merged[0] is spec[0]


def test_empty_client_tools_returns_spec_only() -> None:
    """No request tools → the spec schemas pass through unchanged."""
    spec = [_schema("load_skill")]

    merged = _merge_request_client_tools(spec, [])

    assert [_schema_tool_name(t) for t in merged] == ["load_skill"]


def test_empty_spec_returns_client_tools() -> None:
    """A spec with no builtins still forwards the caller's client tools.

    Guards the path where the agent declares no MCP/builtin surface —
    the client tools must still reach the harness on their own.
    """
    client = [_schema("Read"), _schema("Write")]

    merged = _merge_request_client_tools([], client)

    assert [_schema_tool_name(t) for t in merged] == ["Read", "Write"]


def test_inputs_not_mutated() -> None:
    """The merge returns a fresh list and never mutates its arguments.

    ``spec_tools`` is the per-conversation cache; mutating it would
    leak one turn's client tools into every later turn on the session.
    """
    spec = [_schema("load_skill")]
    client = [_schema("Read")]

    merged = _merge_request_client_tools(spec, client)

    assert len(spec) == 1, "spec_tools (the session cache) must not grow"
    assert len(client) == 1
    assert merged is not spec


def test_malformed_client_tools_dropped() -> None:
    """Non-dict and nameless client entries are dropped; valid ones kept.

    Both a bare string and a ``function``-less dict are malformed — the
    executor rejects an unnamed FunctionTool — so the merge must forward
    neither while still carrying the one valid tool through.
    """
    spec = [_schema("load_skill")]
    client: list[Any] = ["not-a-dict", {"type": "function"}, _schema("Read")]

    merged = _merge_request_client_tools(spec, client)

    # Exactly the spec tool + the one well-formed client tool survive; the
    # bare string and the nameless dict are both dropped. A wrong length
    # here means a malformed entry leaked through to the harness.
    assert merged == [_schema("load_skill"), _schema("Read")]


def test_client_side_tool_relays_not_dispatched() -> None:
    """A request-supplied client-side tool relays upstream, never local.

    This is the core regression: before the fix, the runner dispatched
    every action_required tool locally and a client tool errored "not in
    local dispatch table". A return of True here means the bypass broke
    and client tools would hang/error instead of tunneling.
    """
    dispatch = TurnDispatch(client_side_tool_names=frozenset({"Read"}))

    assert (
        _should_dispatch_tool_locally(
            "Read",
            dispatch=dispatch,
            is_mcp=False,
            is_runner_builtin=False,
            is_spec_local=False,
        )
        is False
    )


def test_client_side_bypass_wins_over_other_signals() -> None:
    """The client-side bypass takes precedence over every dispatch signal.

    If a tool is registered as client-side, it must relay even when other
    dispatch signals are set — otherwise the runner would execute it
    locally and the caller's tunnel would never fire.
    """
    dispatch = TurnDispatch(client_side_tool_names=frozenset({"Read"}))

    # Every other signal True; the client-side bypass still wins.
    assert (
        _should_dispatch_tool_locally(
            "Read",
            dispatch=dispatch,
            is_mcp=True,
            is_runner_builtin=True,
            is_spec_local=True,
        )
        is False
    )


def test_non_client_side_tool_dispatches_in_session_native_mode() -> None:
    """With a TurnDispatch present, a non-client-side tool dispatches locally.

    The ``dispatch is not None`` catch-all keeps spec-local / UC /
    spec-callable tools dispatching on the runner in session-native mode.
    A False here would relay a runner-owned tool upstream and break it.
    """
    dispatch = TurnDispatch(client_side_tool_names=frozenset({"Read"}))

    assert (
        _should_dispatch_tool_locally(
            "sys_session_send",
            dispatch=dispatch,
            is_mcp=False,
            is_runner_builtin=False,
            is_spec_local=False,
        )
        is True
    )


def test_legacy_path_relays_unknown_tool() -> None:
    """On the legacy path (no TurnDispatch), an unknown tool relays upstream.

    Without a dispatch context and with no dispatchability signal set, the
    tool (e.g. a client tool) relays. A True here would resurrect the old
    over-eager dispatch that errored "not in local dispatch table".
    """
    assert (
        _should_dispatch_tool_locally(
            "Read",
            dispatch=None,
            is_mcp=False,
            is_runner_builtin=False,
            is_spec_local=False,
        )
        is False
    )


def test_legacy_path_dispatches_builtin() -> None:
    """A runner builtin dispatches locally even on the legacy path.

    Guards that narrowing the dispatch condition didn't accidentally stop
    dispatching genuine builtins (``is_runner_builtin``) when no
    TurnDispatch is present.
    """
    assert (
        _should_dispatch_tool_locally(
            "load_skill",
            dispatch=None,
            is_mcp=False,
            is_runner_builtin=True,
            is_spec_local=False,
        )
        is True
    )
