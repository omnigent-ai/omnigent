"""E2E: MCP tool calls must survive a remote MCP server restart.

When a remote streamable-HTTP MCP server restarts (new
process, session ids lost), the next ``tools/call`` on the stale
``Mcp-Session-Id`` fails with ``McpError: Session terminated``
(code ``32600``). ``_is_connection_error`` in ``omnigent/tools/mcp.py``
only classifies ``CONNECTION_CLOSED`` (``-32000``) as a connection
error, so ``_call_tool_with_reconnect`` re-raises immediately instead
of reconnecting — every subsequent tool call in the conversation fails
permanently until the user starts a new conversation.

Journey (all user-observable):

1. Start a session on an agent whose only tool is a remote
   streamable-HTTP MCP server (an echo tool).
2. Ask the agent to call the tool — the tool round-trips fine.
3. The MCP server restarts (redeploy: old process gone, new process
   on the same URL, all session ids lost).
4. Ask the agent to call the tool again — the tool call must succeed
   (the client should reconnect and retry), but on the broken build
   it fails permanently with the generic runner-dispatch error.

The mock LLM scripts both turns (tool call, then a per-turn wrap-up
sentence), so the only nondeterminism is the MCP transport itself.
"""

from __future__ import annotations

import json as _json
import re
import socket
import subprocess
import sys
import time
import uuid
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
)

_COMPOSER = "Send a message…"

# Wrap-up sentences are unique per turn so waiting for them proves the
# specific turn settled (never a stale bubble from the prior turn).
_TURN1_DONE = "first-echo-complete"
_TURN2_DONE = "second-echo-complete"

# Same-length match tokens: the mock LLM routes by longest match then
# rightmost position, so turn 2's token (appearing later in the replayed
# conversation) wins its own queue (see test_multi_turn_chat.py).
_TURN1_TOKEN = "mcp-echo-turn-ONE"
_TURN2_TOKEN = "mcp-echo-turn-TWO"

# Minimal streamable-HTTP MCP server: one ``echo`` tool. Launched as a
# subprocess (``python -c``) so killing/relaunching it is a faithful
# "remote server redeploy": a NEW process on the same URL with all
# Mcp-Session-Ids lost.
_MCP_SERVER_CODE = """
import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-http", host="127.0.0.1", port=int(sys.argv[1]))

@mcp.tool()
def echo(text: str) -> str:
    return f"echo: {text}"

mcp.run(transport="streamable-http")
"""

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic echo assistant. When the user asks you to
  echo a string, call the ``echomcp__echo`` tool with that exact string
  as the ``text`` argument, then reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents

tools:
  echomcp:
    type: mcp
    url: http://127.0.0.1:{port}/mcp
"""


def _find_free_port() -> int:
    """Bind port 0 and return the OS-assigned free port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_mcp_server(port: int, timeout_s: float = 30.0) -> subprocess.Popen[bytes]:
    """Start the echo MCP server subprocess and wait until it serves /mcp.

    Readiness is a real ``initialize`` POST (``trust_env=False`` so an
    ambient CI proxy can't intercept the loopback probe), not a fixed
    sleep — uvicorn boot time varies under CI load.

    :param port: Loopback port to serve on.
    :param timeout_s: Max seconds to wait for readiness.
    :returns: The running server process.
    :raises RuntimeError: If the server does not become ready in time.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _MCP_SERVER_CODE, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "readiness-probe", "version": "0"},
        },
    }
    deadline = time.monotonic() + timeout_s
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"MCP server exited early (code {proc.returncode})")
            try:
                resp = client.post(
                    f"http://127.0.0.1:{port}/mcp",
                    json=init_body,
                    headers={"Accept": "application/json, text/event-stream"},
                    timeout=2.0,
                )
                if resp.status_code == 200:
                    return proc
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"MCP server on port {port} not ready within {timeout_s:.0f}s")


def _stop_mcp_server(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the MCP server process, escalating to SIGKILL."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _echo_tool_outputs(base_url: str, session_id: str) -> list[str]:
    """Return the raw outputs of every ``echomcp__echo`` call, in order.

    :param base_url: Spawned server base URL.
    :param session_id: The session whose items to read.
    :returns: List of ``function_call_output`` strings for the echo tool.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0)
    resp.raise_for_status()
    items = resp.json()["data"]

    call_ids: set[str] = set()
    for item in items:
        data = item.get("data") or {}
        name = item.get("name") or data.get("name")
        call_id = item.get("call_id") or data.get("call_id")
        if item.get("type") == "function_call" and name == "echomcp__echo" and call_id:
            call_ids.add(call_id)

    outputs: list[str] = []
    for item in items:
        data = item.get("data") or {}
        call_id = item.get("call_id") or data.get("call_id")
        if item.get("type") == "function_call_output" and call_id in call_ids:
            outputs.append(str(item.get("output") or data.get("output") or ""))
    return outputs


def test_mcp_tool_call_recovers_after_mcp_server_restart(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: Any,
) -> None:
    """A tool call after the MCP server restarts must reconnect, not fail.

    On the broken build the second call surfaces the stale-session
    ``Session terminated`` failure (relayed as the generic runner-dispatch
    error) instead of the echo output, because ``_is_connection_error``
    does not treat ``McpError`` code ``32600`` as a connection error and
    the reconnect/retry path never runs.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    port = _find_free_port()
    mcp_proc = _start_mcp_server(port)
    session_id: str | None = None
    try:
        model = f"mcp-restart-{uuid.uuid4().hex[:8]}"
        # Turn 1: call echo("before-restart"), then wrap up.
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_echo_1",
                            "name": "echomcp__echo",
                            "arguments": _json.dumps({"text": "before-restart"}),
                        }
                    ]
                },
                {"text": _TURN1_DONE},
            ],
            key=f"{model}-t1",
            match=_TURN1_TOKEN,
        )
        # Turn 2 (after the restart): call echo("after-restart"), then wrap up.
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_echo_2",
                            "name": "echomcp__echo",
                            "arguments": _json.dumps({"text": "after-restart"}),
                        }
                    ]
                },
                {"text": _TURN2_DONE},
            ],
            key=f"{model}-t2",
            match=_TURN2_TOKEN,
        )

        yaml_text = _AGENT_YAML.format(
            name=f"mcp-restart-probe-{uuid.uuid4().hex[:6]}",
            model=model,
            port=port,
        )
        session_id = _create_bundled_session(live_server, runner_id, yaml_text)

        page.goto(f"{live_server}/c/{session_id}")

        # ── Turn 1: the MCP tool round-trips while the server is up ──
        _send(page, f"Echo the string 'before-restart' for me. {_TURN1_TOKEN}")
        expect(page.get_by_text(_TURN1_DONE)).to_be_visible(timeout=120_000)

        outputs = _echo_tool_outputs(live_server, session_id)
        assert len(outputs) == 1 and "echo: before-restart" in outputs[0], (
            f"Turn 1 echo tool call did not round-trip: outputs={outputs!r}"
        )

        # ── The remote MCP server restarts (redeploy) ──
        # New process on the same URL: every Mcp-Session-Id is lost,
        # exactly what a FastMCP redeploy behind a proxy does.
        _stop_mcp_server(mcp_proc)
        mcp_proc = _start_mcp_server(port)

        # ── Turn 2: the tool call must transparently reconnect ──
        _send(page, f"Echo the string 'after-restart' for me. {_TURN2_TOKEN}")
        expect(page.get_by_text(_TURN2_DONE)).to_be_visible(timeout=120_000)

        # Best-effort: expand the settled turn's process trace so the tool
        # result is on screen (the recording then ends showing the actual
        # tool output — the echo on a fixed build, the dispatch error on a
        # broken one). Never let expansion flake mask the real assertion.
        try:
            page.get_by_role("button", name=re.compile(r"^Worked")).last.click(timeout=5_000)
            page.get_by_role("button", name=re.compile(r"Called 1 tool")).last.click(timeout=5_000)
            page.wait_for_timeout(2_000)
        except Exception:
            pass

        outputs = _echo_tool_outputs(live_server, session_id)
        assert len(outputs) == 2, (
            f"Expected two echo tool outputs (one per turn), got {len(outputs)}: {outputs!r}"
        )
        assert "echo: after-restart" in outputs[1], (
            "MCP tool call after the MCP server restart did not recover: "
            "the stale-session 'Session terminated' (32600) error was not "
            "classified as a connection error, so the reconnect path never "
            f"ran. Tool output was: {outputs[1]!r}"
        )
    finally:
        _stop_mcp_server(mcp_proc)
        if session_id is not None:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)
