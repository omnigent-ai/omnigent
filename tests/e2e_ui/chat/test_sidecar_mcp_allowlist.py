"""E2E: a sidecar ``tools/mcp/*.yaml`` per-server ``tools:`` allow-list
must actually restrict the agent's tool surface.

A per-server ``tools:`` allow-list is honoured when an MCP server is
declared **inline** under ``tools:`` in ``config.yaml``, but was
**silently ignored** when the same server is declared in a **sidecar**
``tools/mcp/*.yaml`` file. The inline parser reads and validates the
key; both sidecar parsers (``_parse_stdio_mcp_server`` /
``_parse_http_mcp_server`` in ``omnigent/spec/parser.py``) build the
``MCPServerConfig`` without ``tools=``, so the allow-list disappears
before it reaches the enforcement seam. The failure is *open*: the
agent gets MORE tools than the spec asked for, with no warning.

Journey (all user-observable):

1. A spec author writes an agent whose only tool is an MCP server
   declared in the SIDECAR form (``tools/mcp/probe.yaml``), with a
   restrictive ``tools:`` allow-list that excludes a destructive tool
   (here ``danger``, standing in for the real ``sandbox_clear``).
2. Deploy the agent and start a session on it.
3. Drive a turn in which the model attempts the *excluded* ``danger``
   tool.
4. On the broken build the excluded tool is advertised to the model and
   executes anyway (the chat shows ``danger-executed: ...``); the
   allow-list was silently dropped. When the allow-list is honoured the
   excluded tool is absent from the advertised surface and never runs.

The mock LLM scripts the turn (call ``danger``, then a wrap-up
sentence), so the only thing under test is which tools the sidecar
allow-list exposes.

Robustness: the guard reads the tool surface the harness advertises to
the model (captured on the FIRST LLM request, before any tool dispatch),
so it settles cleanly on both the broken and the fixed build regardless
of how a filtered tool call is handled downstream.
"""

from __future__ import annotations

import contextlib
import io
import json as _json
import sys
import tarfile
import time
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
)

# The two-tool stdio MCP fixture: a SAFE ``echo`` and a forbidden
# ``danger`` that returns a distinctive marker when it runs.
_ALLOWLIST_MCP_SERVER = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "tools"
    / "fixtures"
    / "allowlist_stdio_mcp_server.py"
)

_PROBE = "sidecar-allowlist-probe"
_DANGER_MARKER = f"danger-executed: {_PROBE}"
_TURN_DONE = "sidecar-allowlist-turn-complete"

_COMPOSER = "Send a message…"

# Agent whose ONLY tool is the sidecar-declared MCP server. Note there is
# NO inline ``tools:`` block here — the allow-list lives in the sidecar
# ``tools/mcp/probe.yaml`` file, which is exactly the form that dropped it.
_CONFIG_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic tool assistant. When the user sends a turn you
  MUST call the ``probe__danger`` tool once with the text they provide,
  then reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents
"""

# Sidecar MCP config with a restrictive allow-list: only ``echo`` is
# permitted; ``danger`` must never be exposed. This is the file whose
# ``tools:`` key was silently discarded.
_SIDECAR_MCP_YAML = """\
name: probe
transport: stdio
command: {python}
args:
  - {server}
tools:
  - echo
"""


def _create_sidecar_allowlist_session(
    base_url: str,
    runner_id: str,
    *,
    agent_name: str,
    model: str,
) -> str:
    """Register + bind an agent whose sidecar MCP allow-lists only ``echo``.

    Packages a bundle with ``config.yaml`` at the root and the sidecar
    ``tools/mcp/probe.yaml`` beside it, then binds the created session to
    *runner_id* so ``POST /v1/responses`` dispatches.

    :param base_url: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind.
    :param agent_name: Display name for the agent.
    :param model: Model id baked into the spec (routes the mock queue).
    :returns: The new session/conversation id.
    """
    assert _ALLOWLIST_MCP_SERVER.is_file(), (
        f"Expected allow-list MCP fixture at {_ALLOWLIST_MCP_SERVER}; "
        f"update _ALLOWLIST_MCP_SERVER if the file moved."
    )
    config = _CONFIG_YAML.format(name=agent_name, model=model).encode()
    sidecar = _SIDECAR_MCP_YAML.format(
        python=sys.executable,
        server=str(_ALLOWLIST_MCP_SERVER),
    ).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        cfg_info = tarfile.TarInfo("config.yaml")
        cfg_info.size = len(config)
        tar.addfile(cfg_info, io.BytesIO(config))
        mcp_info = tarfile.TarInfo("tools/mcp/probe.yaml")
        mcp_info.size = len(sidecar)
        tar.addfile(mcp_info, io.BytesIO(sidecar))

    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


def _advertised_tool_names(mock_url: str) -> set[str]:
    """Collect every tool name the harness advertised to the model.

    Reads the captured request bodies from the mock LLM and unions the
    ``name`` of each entry in every request's ``tools`` array.

    :param mock_url: Mock LLM base URL.
    :returns: Set of advertised tool names (namespaced, e.g. ``probe__echo``).
    """
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    names: set[str] = set()
    for req in resp.json().get("requests", []):
        if not isinstance(req, dict):
            continue
        for tool in req.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            # openai-agents Responses format: {"type": "function", "name": ...}
            # or a nested {"function": {"name": ...}} chat-completions shape.
            name = tool.get("name")
            if name is None and isinstance(tool.get("function"), dict):
                name = tool["function"].get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _wait_for_advertised_tools(mock_url: str, *, timeout: float = 60.0) -> set[str]:
    """Poll the mock until a request carrying a tool surface is captured.

    The harness sends the model's available tools on the FIRST LLM
    request of the turn, before any tool is dispatched — so this settles
    on both the broken and the fixed build (it does not depend on the
    forbidden tool call succeeding).

    :param mock_url: Mock LLM base URL.
    :param timeout: Max seconds to wait.
    :returns: The union of advertised tool names once any appear.
    :raises AssertionError: If no tool surface is advertised in time.
    """
    deadline = time.monotonic() + timeout
    names: set[str] = set()
    while time.monotonic() < deadline:
        names = _advertised_tool_names(mock_url)
        if names:
            return names
        time.sleep(0.5)
    raise AssertionError(
        "The harness never advertised any tools to the model within "
        f"{timeout:.0f}s — the turn did not reach the LLM."
    )


def _tool_call_outputs(base_url: str, session_id: str) -> list[str]:
    """Return the raw outputs of every ``function_call_output`` item, in order.

    :param base_url: Spawned server base URL.
    :param session_id: Session whose items to read.
    :returns: List of tool-output strings.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0)
    resp.raise_for_status()
    items = resp.json()["data"]
    outputs: list[str] = []
    for item in items:
        data = item.get("data") or {}
        if item.get("type") == "function_call_output":
            outputs.append(str(item.get("output") or data.get("output") or ""))
    return outputs


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_sidecar_mcp_allowlist_excludes_forbidden_tool(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory,
) -> None:
    """A sidecar ``tools:`` allow-list must exclude the forbidden tool.

    On the broken build the sidecar parser drops the ``tools:`` key, so
    ``probe__danger`` is advertised to the model and executes (the chat
    shows ``danger-executed: ...``). When the allow-list is honoured the
    excluded tool is absent from the advertised surface and never runs.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    session_id: str | None = None
    try:
        model = f"mcp-allowlist-{uuid.uuid4().hex[:8]}"
        # Turn: the model calls the FORBIDDEN ``danger`` tool, then wraps up.
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_danger_1",
                            "name": "probe__danger",
                            "arguments": _json.dumps({"text": _PROBE}),
                        }
                    ]
                },
                {"text": _TURN_DONE},
            ],
            key=model,
        )

        session_id = _create_sidecar_allowlist_session(
            live_server,
            runner_id,
            agent_name=f"sidecar-allowlist-{uuid.uuid4().hex[:6]}",
            model=model,
        )

        page.goto(f"{live_server}/c/{session_id}")

        # Drive the turn: the model is scripted to attempt ``danger``.
        _send(page, "Run the tool on this turn.")

        # Best-effort: let the turn play out on screen so the recording
        # shows the excluded tool executing on the broken build. Swallowed
        # so a fixed build (where the excluded call is refused) can't hang.
        with contextlib.suppress(Exception):
            expect(page.get_by_text(_TURN_DONE)).to_be_visible(timeout=60_000)

        # ── The exact reported symptom: the excluded tool is advertised ──
        # Read the tool surface the harness sent the model (captured on the
        # first LLM request, before any dispatch) — deterministic on both
        # the broken and the fixed build.
        advertised = _wait_for_advertised_tools(mock_llm_server_url, timeout=60.0)
        assert "probe__echo" in advertised, (
            f"Sanity check failed: the allow-listed ``echo`` tool should be "
            f"advertised to the model, but the advertised tools were: {advertised!r}"
        )
        assert "probe__danger" not in advertised, (
            "SIDECAR ALLOW-LIST IGNORED: the excluded ``danger`` tool was "
            "advertised to the model. The sidecar tools/mcp/*.yaml ``tools:`` "
            "allow-list ([echo]) was silently dropped by the sidecar parser, "
            f"so the model saw the destructive tool. Advertised: {advertised!r}"
        )

        # ── And it must never actually run ──
        outputs = _tool_call_outputs(live_server, session_id)
        assert not any(_DANGER_MARKER in o for o in outputs), (
            "SIDECAR ALLOW-LIST IGNORED: the excluded ``danger`` tool "
            f"executed ({_DANGER_MARKER!r} appeared in a tool output). The "
            "sidecar allow-list should have prevented it from being callable. "
            f"Tool outputs: {outputs!r}"
        )
    finally:
        if session_id is not None:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)
