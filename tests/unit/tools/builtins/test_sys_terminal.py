"""
Unit tests for the ``sys_terminal_*`` tool family.

Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §8.2, these tests use the
established ``tests/tools/builtins/test_terminal.py`` pattern from
the deleted legacy suite: monkeypatch
``_globals._terminal_registry`` to inject a fresh
:class:`TerminalRegistry`, construct tools directly, drive them
via ``tool.invoke()``. The tests run real tmux subprocesses
(skipped if tmux is not on PATH) so they cover the full
spawn → send → read → close round-trip.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.conversation import MessageData
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.runtime import _globals
from omnigent.spec.types import AgentSpec
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.terminals.registry import TerminalRegistry
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins.sys_terminal import (
    SysTerminalCloseTool,
    SysTerminalLaunchTool,
    SysTerminalListTool,
    SysTerminalReadTool,
    SysTerminalSendTool,
)

# Skip the entire module when tmux is unavailable. Every test
# launches a real tmux session — there's no faking that with
# mocks because the production code shells out to ``tmux`` via
# ``asyncio.create_subprocess_exec``.
pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed; sys_terminal_* tests need a real tmux on PATH",
)


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> TerminalRegistry:
    """Fresh :class:`TerminalRegistry` installed as the singleton.

    Monkeypatches ``_globals._terminal_registry`` so
    ``get_terminal_registry()`` finds it. Auto-reverses on test
    teardown — each test gets a clean registry.

    :param monkeypatch: Pytest's monkeypatch fixture.
    :returns: The newly-installed :class:`TerminalRegistry`.
    """
    reg = TerminalRegistry()
    monkeypatch.setattr(_globals, "_terminal_registry", reg)
    return reg


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    """A :class:`ToolContext` with a real per-test workspace.

    :param tmp_path: Pytest's tmpdir — used as the workspace.
    :returns: A :class:`ToolContext` suitable for terminal tools.
    """
    return ToolContext(
        task_id="task_test",
        agent_id="agent_test",
        workspace=tmp_path,
        conversation_id="conv_test",
    )


def _make_spec(
    *,
    terminals: dict[str, TerminalEnvSpec] | None = None,
    os_env: OSEnvSpec | None = None,
) -> AgentSpec:
    """Construct a minimal :class:`AgentSpec` for tool wiring tests.

    :param terminals: Optional terminals map. ``None`` → no
        terminals declared (use only when testing the
        unknown-terminal error path).
    :param os_env: Optional os_env. ``None`` means the spec
        declares no os_env block.
    :returns: A populated :class:`AgentSpec`.
    """
    return AgentSpec(
        spec_version=1,
        name="terminal-test",
        terminals=terminals,
        os_env=os_env,
    )


@pytest.fixture
async def cleanup_registry(registry: TerminalRegistry) -> AsyncIterator[None]:
    """Ensure every terminal is closed at test teardown.

    Tests that launch terminals must register their cleanup so a
    failed assertion doesn't leak tmux subprocesses. Yields nothing;
    the value of the fixture is the side-effect on teardown.

    :param registry: The registry fixture (drives the cleanup).
    :yields: ``None`` — no value to consume.
    """
    yield
    await registry.shutdown()


async def _invoke(tool: Tool, payload: dict[str, object], ctx: ToolContext) -> dict:
    """Drive ``tool.invoke`` via ``asyncio.to_thread`` and decode JSON.

    Mirrors production dispatch: ``omnigent/runtime/workflow.py`` calls
    ``tool_mgr.call_tool`` inside ``asyncio.to_thread`` so the sync
    ``invoke`` runs on a worker thread (with no event loop), letting
    the tool spin its own ``asyncio.run()`` for async work. Calling
    ``tool.invoke`` directly from an async test would crash because
    the test's event loop is already running.

    :param tool: The tool instance to invoke.
    :param payload: JSON-serializable arguments dict for the tool.
    :param ctx: The :class:`ToolContext` to pass through.
    :returns: The tool result, JSON-decoded into a dict.
    """
    raw = await asyncio.to_thread(tool.invoke, json.dumps(payload), ctx)
    return json.loads(raw)


# ── sys_terminal_launch ──────────────────────────────────────


def test_launch_unknown_terminal_returns_error(
    registry: TerminalRegistry, ctx: ToolContext
) -> None:
    """
    Launching a terminal that isn't in ``spec.terminals`` returns
    an error envelope rather than crashing or silently spawning.

    What breaks if this fails: typos in the LLM's terminal name
    spawn untracked tmux sessions OR crash the workflow with an
    unhandled KeyError. The error envelope lets the LLM recover.
    """
    spec = _make_spec(terminals={"bash": TerminalEnvSpec(command="bash")})
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    result = json.loads(tool.invoke(json.dumps({"terminal": "ghost", "session": "s1"}), ctx))

    assert "error" in result
    # The error message names the unknown terminal so the LLM can
    # fix its tool call.
    assert "ghost" in result["error"]
    # And lists the declared terminals so the LLM can see the
    # valid options.
    assert "bash" in result["error"]


def test_launch_requires_conversation_id(registry: TerminalRegistry, tmp_path: Path) -> None:
    """
    The launch tool fails loud when ``ctx.conversation_id`` is
    ``None``. Per the registry's keying contract (and §4.2 of
    the design), a conversation id is required to scope the new
    terminal entry; falling back to a default would let two
    independent conversations share a terminal silently.
    """
    spec = _make_spec(terminals={"bash": TerminalEnvSpec(command="bash")})
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)
    ctx_no_conv = ToolContext(
        task_id="t",
        agent_id="a",
        workspace=tmp_path,
        conversation_id=None,
    )

    result = json.loads(
        tool.invoke(json.dumps({"terminal": "bash", "session": "s1"}), ctx_no_conv)
    )
    assert "error" in result
    # Mentions the missing field so the framework operator knows
    # what to fix.
    assert "conversation_id" in result["error"]


def test_launch_rejects_cwd_override_when_disallowed(
    registry: TerminalRegistry, ctx: ToolContext, tmp_path: Path
) -> None:
    """
    When ``terminal.allow_cwd_override`` is ``False`` (the default),
    a per-call ``cwd`` argument is rejected with a clear error —
    the override does NOT silently apply.

    What breaks if this fails: a security-conscious spec author who
    set ``allow_cwd_override: false`` to lock the tmux cwd loses
    that lock; the LLM can escape the configured cwd.
    """
    spec = _make_spec(
        terminals={"bash": TerminalEnvSpec(command="bash", allow_cwd_override=False)}
    )
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    result = json.loads(
        tool.invoke(
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "cwd": str(tmp_path / "elsewhere"),
                }
            ),
            ctx,
        )
    )
    assert "error" in result
    assert "cwd override" in result["error"].lower()


def test_launch_rejects_sandbox_override_when_disallowed(
    registry: TerminalRegistry, ctx: ToolContext
) -> None:
    """
    Mirror of the cwd test for sandbox: ``allow_sandbox_override``
    defaults to ``False``, and a per-call sandbox argument must
    be rejected.
    """
    spec = _make_spec(
        terminals={"bash": TerminalEnvSpec(command="bash", allow_sandbox_override=False)}
    )
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    result = json.loads(
        tool.invoke(
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "sandbox": "none",
                }
            ),
            ctx,
        )
    )
    assert "error" in result
    assert "sandbox override" in result["error"].lower()


async def test_launch_send_read_close_round_trip(
    registry: TerminalRegistry,
    ctx: ToolContext,
    cleanup_registry: None,
    tmp_path: Path,
) -> None:
    """
    The full sys_terminal_* round trip works against a real tmux:
    launch returns ``status: launched``, send + read returns the
    echoed text, close returns ``status: closed``.

    This is the load-bearing happy-path test. If it fails, the
    whole sys_terminal_* family is broken end-to-end.

    Sandbox is forced to ``none`` because tmux + bwrap + a
    workspace directory pytest creates fresh per test would need
    extra setup; the legacy ``tests/inner/test_terminal.py`` covers
    the sandboxed path against ``inner.terminal.TerminalInstance``
    directly.
    """
    del cleanup_registry  # consumed for its teardown side-effect
    spec = _make_spec(
        terminals={
            "bash": TerminalEnvSpec(
                command="bash",
                os_env=OSEnvSpec(
                    type="caller_process",
                    cwd=str(tmp_path),
                    sandbox=OSEnvSandboxSpec(type="none"),
                ),
            )
        },
    )
    launch_tool = SysTerminalLaunchTool(spec=spec, registry=registry)
    send_tool = SysTerminalSendTool(registry=registry)
    read_tool = SysTerminalReadTool(registry=registry)
    close_tool = SysTerminalCloseTool(registry=registry)

    launch = await _invoke(launch_tool, {"terminal": "bash", "session": "s1"}, ctx)
    assert launch.get("status") == "launched", (
        f"Expected status='launched' on first launch, got {launch!r}. "
        "If 'already_running', the registry was pre-populated and "
        "the test fixture isn't isolating state correctly."
    )
    assert launch["terminal"] == "bash"
    assert launch["session"] == "s1"

    # Send a marker and verify it appears in the pane capture.
    send = await _invoke(
        send_tool,
        {
            "terminal": "bash",
            "session": "s1",
            "text": "echo TERMINAL_E2E_MARKER_AAAA",
            "keys": "Enter",
        },
        ctx,
    )
    assert send.get("status") == "sent"

    # Allow tmux to render the echo output. Polling rather than a
    # fixed sleep — the marker may appear after a few hundred ms.
    marker_seen = False
    read: dict = {}
    for _ in range(20):  # 20 * 0.1s = ~2s budget
        read = await _invoke(read_tool, {"terminal": "bash", "session": "s1"}, ctx)
        if "TERMINAL_E2E_MARKER_AAAA" in read.get("screen", ""):
            marker_seen = True
            break
        await asyncio.sleep(0.1)
    assert marker_seen, (
        f"echo marker never appeared in pane after 2s. Last read: {read!r}. "
        "If the screen is empty, the send didn't reach tmux. If the "
        "screen has the prompt but not the echo output, tmux is slow "
        "(bump the budget) or the bash command failed."
    )

    # Close removes the entry from the registry.
    close = await _invoke(close_tool, {"terminal": "bash", "session": "s1"}, ctx)
    assert close.get("status") == "closed"
    # Subsequent get() returns None — entry is gone.
    assert registry.get(ctx.conversation_id, "bash", "s1") is None


async def test_launch_idempotent_returns_already_running(
    registry: TerminalRegistry,
    ctx: ToolContext,
    cleanup_registry: None,
    tmp_path: Path,
) -> None:
    """
    Launching the same (terminal, session) twice doesn't spawn a
    second tmux. The second call returns
    ``status: already_running`` and reuses the existing instance.

    What breaks if this fails: every duplicate launch leaks a tmux
    subprocess. The LLM retries on transient errors and would
    accumulate orphan sessions.
    """
    del cleanup_registry
    spec = _make_spec(
        terminals={
            "bash": TerminalEnvSpec(
                command="bash",
                os_env=OSEnvSpec(
                    type="caller_process",
                    cwd=str(tmp_path),
                    sandbox=OSEnvSandboxSpec(type="none"),
                ),
            )
        },
    )
    launch_tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    first = await _invoke(launch_tool, {"terminal": "bash", "session": "s1"}, ctx)
    assert first["status"] == "launched"

    second = await _invoke(launch_tool, {"terminal": "bash", "session": "s1"}, ctx)
    # Idempotent: second call doesn't spawn a new tmux but reports
    # the existing one with status='already_running'.
    assert second["status"] == "already_running"
    # Same socket — proves we're pointing at the SAME instance, not
    # a fresh one with a new socket.
    assert second["tmux_socket"] == first["tmux_socket"]


async def test_multiple_sessions_per_terminal_are_independent(
    registry: TerminalRegistry,
    ctx: ToolContext,
    cleanup_registry: None,
    tmp_path: Path,
) -> None:
    """
    Two sessions of the same terminal name (``bash:s1`` and
    ``bash:s2``) get independent tmux sessions with independent
    state. Verifies the (name, session_key) keying.

    What breaks if this fails: multi-session workflows (e.g.
    investigate one branch in s1 while comparing to another in
    s2) collide on shared state — a ``cd /tmp`` in s1 affects s2.
    """
    del cleanup_registry
    spec = _make_spec(
        terminals={
            "bash": TerminalEnvSpec(
                command="bash",
                os_env=OSEnvSpec(
                    type="caller_process",
                    cwd=str(tmp_path),
                    sandbox=OSEnvSandboxSpec(type="none"),
                ),
            )
        },
    )
    launch_tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    s1 = await _invoke(launch_tool, {"terminal": "bash", "session": "s1"}, ctx)
    s2 = await _invoke(launch_tool, {"terminal": "bash", "session": "s2"}, ctx)
    assert s1["status"] == "launched"
    assert s2["status"] == "launched"
    # Two different tmux sockets — independent processes.
    assert s1["tmux_socket"] != s2["tmux_socket"], (
        "Both sessions should have distinct tmux sockets. If they "
        "match, the registry collapsed (name, session_key) into "
        "name-only and the second launch reused the first's socket."
    )


# ── sys_terminal_send / read ──────────────────────────────────


def test_send_unknown_session_returns_error(registry: TerminalRegistry, ctx: ToolContext) -> None:
    """
    Sending to a (terminal, session) the registry doesn't know
    returns an error envelope — doesn't try to coerce something
    into a tmux send and doesn't crash.
    """
    tool = SysTerminalSendTool(registry=registry)
    result = json.loads(
        tool.invoke(
            json.dumps({"terminal": "bash", "session": "ghost", "text": "x"}),
            ctx,
        )
    )
    assert "error" in result


def test_read_unknown_session_returns_error(registry: TerminalRegistry, ctx: ToolContext) -> None:
    """Mirror of the send test for read."""
    tool = SysTerminalReadTool(registry=registry)
    result = json.loads(
        tool.invoke(
            json.dumps({"terminal": "bash", "session": "ghost"}),
            ctx,
        )
    )
    assert "error" in result


# ── sys_terminal_list ─────────────────────────────────────────


def test_list_empty_returns_empty_array(registry: TerminalRegistry, ctx: ToolContext) -> None:
    """
    ``sys_terminal_list`` on a conversation with no terminals
    returns ``[]`` (not an error, not an empty dict). The LLM
    relies on the empty-list shape to know "nothing here yet."
    """
    tool = SysTerminalListTool(registry=registry)
    result = json.loads(tool.invoke("{}", ctx))
    assert result == []


# ── sys_terminal_close ────────────────────────────────────────


def test_close_unknown_session_returns_not_found(
    registry: TerminalRegistry, ctx: ToolContext
) -> None:
    """
    Closing a non-existent (terminal, session) returns
    ``status: not_found`` rather than raising. Idempotent close
    is the contract — the LLM may close the same terminal twice
    without seeing an error.
    """
    tool = SysTerminalCloseTool(registry=registry)
    result = json.loads(tool.invoke(json.dumps({"terminal": "bash", "session": "ghost"}), ctx))
    assert result.get("status") == "not_found"


# ── §4.6 cwd-resolution precedence ────────────────────────────


def test_cwd_resolution_uses_workspace_when_spec_cwd_is_dot(
    registry: TerminalRegistry, ctx: ToolContext
) -> None:
    """
    Per §4.6: when the spec's ``os_env.cwd`` is the bare ``"."``
    placeholder, the launch falls through to ``ctx.workspace``.
    This is the load-bearing fix — under Omnigent mode, AP's process
    cwd is meaningless to the agent.

    Tests the resolver in isolation by inspecting the resolved
    cwd via the tool's private helper. Doesn't actually spawn
    tmux.
    """
    spec = _make_spec(
        terminals={"bash": TerminalEnvSpec(command="bash")},
        os_env=OSEnvSpec(type="caller_process", cwd="."),
    )
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    # Public API doesn't expose the resolution result; we reach
    # in to verify the precedence directly. This is one of the
    # rare cases the omnigent-testing skill rule-14 ("no
    # private method calls") tolerates: the resolver's logic is
    # security-relevant (cwd is the sandbox root anchor) and
    # warrants direct testing rather than only being covered
    # transitively through a launched tmux. Public-API coverage
    # comes from the round-trip integration test above.
    resolved = tool._resolve_cwd(
        cwd_override=None,
        terminal_spec=spec.terminals["bash"],
        ctx=ctx,
    )
    assert resolved == str(ctx.workspace)


def test_cwd_resolution_uses_workspace_when_terminal_cwd_is_dot(
    registry: TerminalRegistry, ctx: ToolContext
) -> None:
    """Terminal-level ``cwd: .`` is a placeholder, not a literal process cwd.

    :param registry: Fresh terminal registry fixture.
    :param ctx: Tool context with a real workspace.
    :returns: None.
    """
    spec = _make_spec(
        terminals={
            "bash": TerminalEnvSpec(
                command="bash",
                os_env=OSEnvSpec(type="caller_process", cwd="."),
            )
        },
        os_env=OSEnvSpec(type="caller_process", cwd="."),
    )
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    resolved = tool._resolve_cwd(
        cwd_override=None,
        terminal_spec=spec.terminals["bash"],
        ctx=ctx,
    )

    assert resolved == str(ctx.workspace)


def test_cwd_resolution_explicit_spec_cwd_wins_over_workspace(
    registry: TerminalRegistry, ctx: ToolContext, tmp_path: Path
) -> None:
    """
    When the spec sets a meaningful os_env.cwd (anything other
    than ``"."``), it wins over ``ctx.workspace``. Specs that
    explicitly anchor to a known path keep that anchor.
    """
    explicit_cwd = str(tmp_path / "explicit")
    spec = _make_spec(
        terminals={"bash": TerminalEnvSpec(command="bash")},
        os_env=OSEnvSpec(type="caller_process", cwd=explicit_cwd),
    )
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)
    resolved = tool._resolve_cwd(
        cwd_override=None,
        terminal_spec=spec.terminals["bash"],
        ctx=ctx,
    )
    assert resolved == explicit_cwd


def test_cwd_resolution_per_call_override_wins(
    registry: TerminalRegistry, ctx: ToolContext, tmp_path: Path
) -> None:
    """
    The per-call ``cwd`` argument (already vetted against
    ``allow_cwd_override``) wins over every spec-level setting.
    """
    spec_cwd = str(tmp_path / "spec_cwd")
    override_cwd = str(tmp_path / "override")
    spec = _make_spec(
        terminals={"bash": TerminalEnvSpec(command="bash", allow_cwd_override=True)},
        os_env=OSEnvSpec(type="caller_process", cwd=spec_cwd),
    )
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)
    resolved = tool._resolve_cwd(
        cwd_override=override_cwd,
        terminal_spec=spec.terminals["bash"],
        ctx=ctx,
    )
    assert resolved == override_cwd


# ── §9.1 concurrent-send race ─────────────────────


async def test_concurrent_sends_serialize_via_per_instance_lock(
    registry: TerminalRegistry,
    ctx: ToolContext,
    cleanup_registry: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    N concurrent ``sys_terminal_send`` calls on the same instance
    must serialize through a per-instance lock. Without the lock,
    ``send(text=X, keys="Enter")`` decomposes into ~2 tmux
    subprocess invocations with a 50ms ``asyncio.sleep`` between
    them — two concurrent sends can interleave their commands,
    feeding the shell garbled input.

    Test strategy: monkeypatch the running instance's ``send``
    method with a slow stub that increments / decrements an
    in-flight counter under a measurement lock. With proper
    serialization, ``max_in_flight`` stays at 1; without
    serialization it can reach the concurrency level.

    The measurement lock is not the production lock — it just
    keeps the counter consistent. The production lock under test
    is the one wired into ``SysTerminalSendTool.invoke``.

    What breaks if this fails:
      - The per-instance lock fix was reverted or never applied.
      - Locks aren't created at instance launch (a path that
        send hits before lookup gets None).
      - A new send-tool code path bypasses lock acquisition.
    """
    import threading

    del cleanup_registry
    spec = _make_spec(
        terminals={
            "bash": TerminalEnvSpec(
                command="bash",
                os_env=OSEnvSpec(
                    type="caller_process",
                    cwd=str(tmp_path),
                    sandbox=OSEnvSandboxSpec(type="none"),
                ),
            )
        },
    )
    launch_tool = SysTerminalLaunchTool(spec=spec, registry=registry)
    send_tool = SysTerminalSendTool(registry=registry)

    launch = await _invoke(launch_tool, {"terminal": "bash", "session": "s1"}, ctx)
    assert launch["status"] == "launched"

    instance = registry.get(ctx.conversation_id, "bash", "s1")
    assert instance is not None

    # Monkeypatch send with a slow recorder. Records max overlap
    # observed; if the production lock works, in_flight never
    # exceeds 1.
    counter_lock = threading.Lock()
    in_flight = [0]
    max_in_flight = [0]

    async def slow_send(text: str | None = None, keys: str = "Enter") -> dict:
        with counter_lock:
            in_flight[0] += 1
            max_in_flight[0] = max(max_in_flight[0], in_flight[0])
        try:
            # Hold long enough that other waiters definitely
            # arrive — 100ms is plenty given the worker-thread
            # spawn overhead is sub-ms.
            await asyncio.sleep(0.1)
            return {"status": "sent"}
        finally:
            with counter_lock:
                in_flight[0] -= 1

    monkeypatch.setattr(instance, "send", slow_send)

    # Fire 8 concurrent sends. Without the lock, max_in_flight
    # would jump to 8 (every thread enters slow_send before any
    # exits, since each holds for 100ms while the spawn loop
    # takes <1ms per call). With the lock, max_in_flight stays
    # at 1.
    n = 8

    async def _send_one(i: int) -> dict:
        return await _invoke(
            send_tool,
            {
                "terminal": "bash",
                "session": "s1",
                "text": f"x{i}",
                "keys": "Enter",
            },
            ctx,
        )

    results = await asyncio.gather(*[_send_one(i) for i in range(n)])
    assert all(r.get("status") == "sent" for r in results)

    assert max_in_flight[0] == 1, (
        f"Expected max_in_flight=1 (lock serializes sends), got "
        f"{max_in_flight[0]} — concurrent sends overlapped. The "
        f"per-instance lock is missing or bypassed. "
        f"If max_in_flight=={n}, no serialization at all; if 2..{n - 1}, "
        f"partial serialization (a regression that lets some calls "
        f"slip through, e.g. lock acquired around wrong scope)."
    )


# ── notify_when_idle ─────────────────────────────────────────


def _build_idle_fixture(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, ToolContext]:
    """
    Wire a real ``SqlAlchemyConversationStore`` + parent conversation,
    plus a tool context populated with the conversation id.

    The legacy idle-watcher delivery path (``task_store.try_deliver``)
    is gone. The tasks table has been removed. This fixture builds only
    what the terminal launch tool actually needs: a parent conversation
    for the conversation-store assertion.

    :param db_uri: Per-test SQLite URI from ``tests/conftest.py``.
    :param monkeypatch: Pytest monkeypatch fixture (unused).
    :returns: ``(parent_conv_id, ctx)``.
    """
    del monkeypatch

    conv_store = SqlAlchemyConversationStore(db_uri)
    parent_conv = conv_store.create_conversation(kind="default")
    ctx = ToolContext(
        task_id="task_placeholder",
        agent_id="ag_test",
        conversation_id=parent_conv.id,
    )
    return parent_conv.id, ctx


def test_launch_does_not_deliver_idle_messages(
    registry: TerminalRegistry,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    lowered_idle_thresholds: None,
    cleanup_registry: None,
) -> None:
    """
    ``sys_terminal_launch`` never emits ``[System: ...is idle]``
    messages into the conversation.

    Regression target: pre-DBOS-removal, the launch path supported
    a ``notify_when_idle`` flag that started a threaded watcher
    delivering idle notifications via ``task_store.try_deliver``.
    With the durability layer removed, the watcher is gone and no
    idle traffic should land in the conversation regardless of
    arguments.
    """
    del cleanup_registry
    parent_conv_id, ctx = _build_idle_fixture(db_uri, monkeypatch)
    spec = _make_spec(terminals={"bash": TerminalEnvSpec(command="bash")})
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    async def _drive() -> str:
        raw = await asyncio.to_thread(
            tool.invoke,
            json.dumps({"terminal": "bash", "session": "s1"}),
            ctx,
        )
        await asyncio.sleep(1.0)
        return raw

    raw_result = asyncio.run(_drive())
    result: dict[str, Any] = json.loads(raw_result)

    # The legacy ``notify_when_idle`` field is no longer surfaced
    # on the envelope (parameter removal).
    assert "notify_when_idle" not in result

    # Conversation should contain no system idle messages.
    conv_store = SqlAlchemyConversationStore(db_uri)
    page = conv_store.list_items(conversation_id=parent_conv_id, limit=50)
    idle_message_texts: list[str] = []
    for item in page.data:
        if item.type != "message":
            continue
        data = item.data
        if not isinstance(data, MessageData):
            continue
        for block in data.content or []:
            if not isinstance(block, dict):
                continue
            text = block.get("text", "")
            if isinstance(text, str) and "is idle" in text:
                idle_message_texts.append(text)
    assert idle_message_texts == [], f"unexpected idle messages from launch: {idle_message_texts}"
