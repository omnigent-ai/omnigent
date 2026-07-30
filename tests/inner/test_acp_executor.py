"""Tests for the generic ACP executor (:mod:`omnigent.inner.acp_executor`).

Two layers:

* **Unit** — construction/argv, both ``session/new`` shapes, tool-call → event
  mapping, permission-outcome mapping, interrupt via ``session/cancel``, and the
  harness-wrap env parsing — all with a mocked transport.
* **Hermetic e2e** — a tiny fake ACP agent (a Python script speaking ACP over
  stdio, written to a temp file) that the executor spawns for real and drives
  through a full turn: initialize → session/new → session/prompt → streaming
  (thought + text + tool card) → request_permission → completion. No real
  vendor binary is required.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent.inner._acp_omnigent_mcp import OmnigentAcpMcp, _to_acp_mcp_servers
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.executor import (
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)

# ---------------------------------------------------------------------------
# Construction / argv
# ---------------------------------------------------------------------------


def test_command_is_shlex_split_into_argv() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="gemini --experimental-acp --model gemini-2.5-pro"))
    assert ex._argv == ["gemini", "--experimental-acp", "--model", "gemini-2.5-pro"]


def test_quoted_command_argv() -> None:
    ex = AcpExecutor(AcpAgentConfig(command='npx -y "@zed-industries/claude-code-acp"'))
    assert ex._argv == ["npx", "-y", "@zed-industries/claude-code-acp"]


def test_empty_command_rejected() -> None:
    with pytest.raises(ValueError):
        AcpExecutor(AcpAgentConfig(command="   "))


def test_handles_tools_internally_and_streaming() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex.handles_tools_internally() is True
    assert ex.supports_streaming() is True


# ---------------------------------------------------------------------------
# session/new shapes (server- vs client-assigned id, optional model)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_new_server_mode_adopts_returned_id() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"result": {"sessionId": "srv-42"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    sid = await ex._ensure_session()
    assert sid == "srv-42"
    assert "sessionId" not in captured["params"]  # server assigns it
    assert captured["params"]["cwd"] == ex._cwd
    assert captured["params"]["mcpServers"] == []


@pytest.mark.asyncio
async def test_session_new_client_mode_generates_and_sends_id() -> None:
    ex = AcpExecutor(
        AcpAgentConfig(
            command="x", session_id_mode="client", model="m1", send_model_in_session_new=True
        )
    )

    sent: dict = {}

    async def capture_rpc(method, params, timeout=30.0):
        sent.update(params)
        return {"result": {}}

    ex._rpc = capture_rpc  # type: ignore[assignment]
    sid = await ex._ensure_session()
    assert sid == sent["sessionId"] and sid  # our generated id is used
    assert sent["model"] == "m1"  # model sent because send_model_in_session_new


# ---------------------------------------------------------------------------
# Tool-call extraction + permission outcome
# ---------------------------------------------------------------------------


def test_extract_tool_call_prefers_title() -> None:
    name, args = AcpExecutor._extract_tool_call(
        {"toolCall": {"title": "shell", "kind": "execute", "rawInput": {"command": "ls"}}}
    )
    assert name == "shell"
    assert args == {"command": "ls"}


def test_extract_tool_call_falls_back_to_kind() -> None:
    name, args = AcpExecutor._extract_tool_call({"toolCall": {"kind": "read"}})
    assert name == "read"
    assert args == {}


def test_permission_outcome_allow_prefers_once() -> None:
    params = {
        "options": [
            {"optionId": "a1", "kind": "allow_always"},
            {"optionId": "a2", "kind": "allow_once"},
            {"optionId": "r1", "kind": "reject_once"},
        ]
    }
    out = AcpExecutor._permission_outcome(params, allow=True)
    assert out == {"outcome": {"outcome": "selected", "optionId": "a2"}}


def test_permission_outcome_deny_picks_reject() -> None:
    params = {"options": [{"optionId": "r", "kind": "reject_once"}]}
    out = AcpExecutor._permission_outcome(params, allow=False)
    assert out == {"outcome": {"outcome": "selected", "optionId": "r"}}


def test_permission_outcome_cancelled_when_no_option() -> None:
    out = AcpExecutor._permission_outcome({"options": []}, allow=True)
    assert out == {"outcome": {"outcome": "cancelled"}}


# ---------------------------------------------------------------------------
# session/update → ExecutorEvent mapping
# ---------------------------------------------------------------------------


def test_update_agent_message_chunk_to_text() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    events = ex._handle_session_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    )
    assert len(events) == 1 and isinstance(events[0], TextChunk) and events[0].text == "hi"


def test_update_thought_chunk_to_reasoning() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    events = ex._handle_session_update(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}}
    )
    assert len(events) == 1 and isinstance(events[0], ReasoningChunk) and events[0].delta == "hmm"


def test_tool_call_and_update_emit_cards() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    started = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "title": "shell",
            "rawInput": {"command": "ls"},
        }
    )
    assert len(started) == 1
    req = started[0]
    assert isinstance(req, ToolCallRequest)
    assert req.name == "shell" and req.metadata == {"call_id": "c1"}
    assert ex._tool_names["c1"] == "shell"

    done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}
    )
    assert len(done) == 1
    comp = done[0]
    assert isinstance(comp, ToolCallComplete)
    assert comp.name == "shell" and comp.status is ToolCallStatus.SUCCESS
    assert comp.metadata == {"call_id": "c1"}
    assert "c1" not in ex._tool_names  # popped


def test_tool_call_update_failed_maps_to_error() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update({"sessionUpdate": "tool_call", "toolCallId": "c2", "title": "t"})
    done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "failed"}
    )
    assert done[0].status is ToolCallStatus.ERROR


def test_usage_update_sets_context_window() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex.max_context_tokens() is None
    ex._handle_session_update({"sessionUpdate": "usage_update", "size": 200000})
    assert ex.max_context_tokens() == 200000


def test_in_progress_tool_update_emits_nothing() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update({"sessionUpdate": "tool_call", "toolCallId": "c3", "title": "t"})
    assert (
        ex._handle_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "c3", "status": "in_progress"}
        )
        == []
    )


# ---------------------------------------------------------------------------
# Permission decision (policy + elicitation gates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_permission_allows_with_no_gates() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) is True


@pytest.mark.asyncio
async def test_decide_permission_denies_on_policy_deny() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_DENY"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) is False


@pytest.mark.asyncio
async def test_decide_permission_ask_defers_to_elicitation() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_ASK"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    ex._elicitation_handler = AsyncMock(return_value=True)
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) is True
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_permission_ask_without_handler_fails_closed() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_ASK"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) is False


# ---------------------------------------------------------------------------
# interrupt → session/cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_sends_session_cancel() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._session_id = "s1"
    ex._proc = type("P", (), {"returncode": None})()  # type: ignore[assignment]
    sent: list[dict] = []
    ex._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    assert await ex.interrupt_session("ignored") is True
    assert sent == [{"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}]


@pytest.mark.asyncio
async def test_interrupt_noop_without_session() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert await ex.interrupt_session("ignored") is False


# ---------------------------------------------------------------------------
# Harness wrap env parsing
# ---------------------------------------------------------------------------


def test_harness_wrap_requires_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import acp_harness

    monkeypatch.delenv("HARNESS_ACP_COMMAND", raising=False)
    with pytest.raises(RuntimeError, match="HARNESS_ACP_COMMAND"):
        acp_harness._build_acp_executor()


def test_harness_wrap_builds_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "goose acp")
    monkeypatch.setenv("HARNESS_ACP_NAME", "Goose")
    monkeypatch.setenv("HARNESS_ACP_SESSION_ID_MODE", "client")
    monkeypatch.setenv("HARNESS_ACP_SEND_MODEL", "1")
    monkeypatch.setenv("HARNESS_ACP_MODEL", "gpt-5.3")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.command == "goose acp"
    assert ex._config.name == "Goose"
    assert ex._config.session_id_mode == "client"
    assert ex._config.send_model_in_session_new is True
    assert ex._config.model == "gpt-5.3"


# ---------------------------------------------------------------------------
# Hermetic end-to-end: drive a real fake ACP agent over stdio
# ---------------------------------------------------------------------------

# A minimal ACP agent: JSON-RPC 2.0 over newline-delimited stdio. It answers the
# handshake, then on session/prompt streams a thought + text + a tool card, asks
# the client for permission, and — once the client answers — finishes the tool
# card, streams closing text, and returns the prompt with a stop reason + usage.
_FAKE_ACP_AGENT = r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid, "update": upd}})

def chunk(sid, kind, text):
    update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

pending_prompt_id = None
pending_sid = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "fake-session-1"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        chunk(sid, "agent_thought_chunk", "planning")
        chunk(sid, "agent_message_chunk", "Hello ")
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "shell",
                     "kind": "execute", "status": "pending", "rawInput": {"command": "echo hi"}})
        send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
            "sessionId": sid,
            "toolCall": {"title": "shell", "kind": "execute", "rawInput": {"command": "echo hi"}},
            "options": [{"optionId": "ok", "kind": "allow_once"},
                        {"optionId": "no", "kind": "reject_once"}],
        }})
        pending_prompt_id, pending_sid = mid, sid
    elif mid == 900 and method is None:
        # The client's permission reply — finish the turn.
        update(pending_sid, {"sessionUpdate": "tool_call_update",
                             "toolCallId": "t1", "status": "completed"})
        chunk(pending_sid, "agent_message_chunk", "done")
        send({"jsonrpc": "2.0", "id": pending_prompt_id, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        }})
"""


@pytest.mark.asyncio
async def test_end_to_end_against_fake_acp_agent(tmp_path: Path) -> None:
    agent_path = tmp_path / "fake_acp_agent.py"
    agent_path.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Fake"))
    approvals: list[tuple[str, dict]] = []

    async def elicit(tool_name: str, tool_input: dict) -> bool:
        approvals.append((tool_name, tool_input))
        return True

    ex._elicitation_handler = elicit  # type: ignore[assignment]

    events = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], "you are a bot"):
            events.append(ev)
    finally:
        await ex.close()

    # The permission request surfaced through the elicitation handler.
    assert approvals == [("shell", {"command": "echo hi"})]

    kinds = [type(e).__name__ for e in events]
    assert "ReasoningChunk" in kinds
    assert "ToolCallRequest" in kinds
    assert "ToolCallComplete" in kinds

    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert text == "Hello done"

    completions = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completions) == 1
    assert completions[0].usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    tool_reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    assert tool_reqs[0].name == "shell" and tool_reqs[0].args == {"command": "echo hi"}
    tool_done = [e for e in events if isinstance(e, ToolCallComplete)]
    assert tool_done[0].status is ToolCallStatus.SUCCESS


# ---------------------------------------------------------------------------
# Omnigent MCP bridge (session/new.mcpServers via the shared serve-mcp relay)
# ---------------------------------------------------------------------------


def test_mcp_to_acp_servers_flattens_env_to_array() -> None:
    """ACP wants env as [{name,value}], not a dict; command/args pass through."""
    out = _to_acp_mcp_servers(
        {"mcpServers": {"omnigent": {"command": "/py", "args": ["-Im", "x"], "env": {"A": "1"}}}}
    )
    assert out == [
        {
            "name": "omnigent",
            "command": "/py",
            "args": ["-Im", "x"],
            "env": [{"name": "A", "value": "1"}],
        }
    ]


def test_mcp_disabled_returns_empty() -> None:
    m = OmnigentAcpMcp("t")
    assert (
        m.session_new_servers(
            tools=[{"name": "x"}], tool_executor=lambda *a: None, loop=None, enabled=False
        )
        == []
    )


def test_mcp_no_executor_returns_empty() -> None:
    m = OmnigentAcpMcp("t")
    assert m.session_new_servers(tools=[{"name": "x"}], tool_executor=None, loop=None) == []


def test_mcp_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_ACP_MCP", "0")
    m = OmnigentAcpMcp("t")
    assert (
        m.session_new_servers(tools=[{"name": "x"}], tool_executor=lambda *a: None, loop=None)
        == []
    )


@pytest.mark.asyncio
async def test_mcp_relay_starts_and_builds_serve_mcp_entry() -> None:
    """A real relay boots (writes bridge.json + tool_relay.json + HTTP server)
    and yields one ACP stdio server pointing at the shared serve-mcp."""
    m = OmnigentAcpMcp("t")

    async def fake_exec(name: str, args: dict) -> dict:
        return {"ok": True}

    loop = asyncio.get_event_loop()
    servers = m.session_new_servers(
        tools=[{"name": "sys_agent_list", "parameters": {"type": "object", "properties": {}}}],
        tool_executor=fake_exec,
        loop=loop,
    )
    try:
        assert len(servers) == 1
        entry = servers[0]
        assert entry["name"] == "omnigent"
        assert "serve-mcp" in entry["args"]
        assert "omnigent.claude_native_bridge" in entry["args"]
        assert all("name" in e and "value" in e for e in entry["env"])
        # Idempotent: a second call returns the cached relay, not a new one.
        assert m.session_new_servers(tools=[], tool_executor=fake_exec, loop=loop) is servers
    finally:
        m.close()


@pytest.mark.asyncio
async def test_acp_session_new_carries_mcp_servers() -> None:
    """When a tool executor + tools are present, session/new carries mcpServers."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._tool_executor = lambda n, a: None  # type: ignore[assignment]
    ex._omnigent_tools = [{"name": "sys_agent_list"}]
    sentinel = [{"name": "omnigent", "command": "/py", "args": [], "env": []}]
    ex._mcp.session_new_servers = lambda **kw: sentinel  # type: ignore[method-assign]
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["params"] = params
        return {"result": {"sessionId": "s1"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()
    assert captured["params"]["mcpServers"] is sentinel


@pytest.mark.asyncio
async def test_acp_session_new_omnigent_mcp_disabled_per_agent() -> None:
    """`omnigent_mcp=False` on the agent config → no mcpServers in session/new."""
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))
    ex._tool_executor = lambda n, a: None  # type: ignore[assignment]
    ex._omnigent_tools = [{"name": "sys_agent_list"}]
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["params"] = params
        return {"result": {"sessionId": "s1"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()
    assert captured["params"]["mcpServers"] == []


@pytest.mark.asyncio
async def test_end_to_end_denied_permission(tmp_path: Path) -> None:
    """A denied elicitation still completes the turn (the agent gets a reject)."""
    agent_path = tmp_path / "fake_acp_agent.py"
    agent_path.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Fake"))

    async def deny(tool_name: str, tool_input: dict) -> bool:
        return False

    ex._elicitation_handler = deny  # type: ignore[assignment]

    events = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
            events.append(ev)
    finally:
        await ex.close()

    # Turn still completes even though the tool was rejected.
    assert any(isinstance(e, TurnComplete) for e in events)
