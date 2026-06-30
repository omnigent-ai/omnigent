"""E2E test: session-discovery tools in claude-native mode.

Verifies that ``sys_session_get_history`` and ``sys_session_list`` are visible
to Claude Code when running in an Omnigent claude-native session.
These tools are advertised via the MCP tool relay
(``tool_relay.json``) that the runner writes before Claude Code
starts. The test launches Claude Code in a headless tmux window,
sends a prompt asking it to list its omnigent MCP tools, and
asserts that the response mentions ``sys_session_get_history``.

Requirements
------------
- ``claude`` CLI on PATH and authenticated.
- ``tmux`` on PATH (needed to run Claude Code headlessly in a test).

Usage::

    OMNIGENT_E2E_CLAUDE_NATIVE_SESSION_TOOLS=1 \\
        pytest tests/e2e/test_session_tools_claude_native.py \\
            --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.claude_native_bridge import (
    augment_claude_args,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
    write_tmux_target,
)
from tests.e2e.conftest import (
    create_runner_bound_session,
    send_user_message_to_session,
    upload_agent,
)

_CLAUDE_NATIVE_UI_AGENT_NAME = "claude-native-ui"

_BRIDGE_READY_TIMEOUT_S = 90.0

_RESPONSE_POLL_TIMEOUT_S = 120.0
_RESPONSE_POLL_INTERVAL_S = 3.0

_NESTED_SESSION_ENV = "CLAUDECODE"

_RUN_GATE_ENV = "OMNIGENT_E2E_CLAUDE_NATIVE_SESSION_TOOLS"


@pytest.fixture(scope="module")
def claude_native_ui_agent(
    http_client: httpx.Client,
) -> str:
    """
    Upload the ``claude-native-ui`` agent spec and return its name.

    Mirrors what ``omnigent claude`` materialises at runtime (harness
    ``claude-native``, ``os_env.type: caller_process`` with no sandbox)
    EXCEPT that it deliberately omits the wrapper's ``spawn: true``
    opt-in — this module's relay test pins the no-opt-in gate (spawn
    writes absent), so the fixture must not opt in.

    :param http_client: HTTP client pointed at the live server.
    :returns: The agent name, ``"claude-native-ui"``.
    """
    spec: dict[str, Any] = {
        "name": _CLAUDE_NATIVE_UI_AGENT_NAME,
        "prompt": (
            "Claude Code is running in the session terminal. Web UI messages are "
            "forwarded into that Claude Code process through the native bridge."
        ),
        "executor": {
            "harness": "claude-native",
            "context_window": 200_000,
        },
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / f"{_CLAUDE_NATIVE_UI_AGENT_NAME}.yaml"
        yaml_path.write_text(yaml.safe_dump(spec, sort_keys=False))
        return upload_agent(
            http_client,
            Path(tmpdir),
            rewrite_model_for_databricks=False,
        )


@contextlib.contextmanager
def _claude_code_session(
    session_id: str,
    *,
    model: str | None,
    launch_env: dict[str, str],
) -> Iterator[Path]:
    """
    Start Claude Code in a private tmux window with the Omnigent MCP bridge.

    Mirrors the ``_claude_code_session`` context manager in
    ``test_comment_tools_claude_native.py``. Sets up the bridge
    directory, launches Claude Code with MCP + hook injection, writes
    ``tmux.json``, and waits for ``server.json`` before yielding.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param model: Claude model id to pin, e.g.
        ``"databricks-claude-sonnet-4-6"``. ``None`` lets the CLI choose.
    :param launch_env: Extra environment for the launched ``claude``
        process — mock LLM base URL and API key.
    :yields: The bridge directory path.
    :raises pytest.fail: If the bridge server does not start within
        :data:`_BRIDGE_READY_TIMEOUT_S`.
    """
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=Path.cwd())

    tmp = tempfile.mkdtemp(prefix="omnigent-e2e-session-tools-")
    tmux_socket = Path(tmp) / "tmux.sock"
    tmux_session = f"stool-{session_id[:8]}"
    tmux_target = f"{tmux_session}:0.0"

    base_args: tuple[str, ...] = (
        "--dangerously-skip-permissions",
        "--allowedTools",
        (
            "mcp__omnigent__list_comments,"
            "mcp__omnigent__update_comment,"
            "mcp__omnigent__sys_session_list,"
            "mcp__omnigent__sys_session_get_history"
        ),
    )
    if model:
        base_args = (*base_args, "--model", model)
    claude_args = augment_claude_args(
        base_args,
        bridge_dir=bridge_dir,
        python_executable=sys.executable,
    )

    launcher = Path(tmp) / "launch_claude.sh"
    launcher.write_text(
        "#!/bin/sh\nexec " + " ".join(f"'{a}'" for a in ["claude", *claude_args]) + "\n"
    )
    launcher.chmod(0o700)

    tmux_env = {k: v for k, v in os.environ.items() if k != _NESTED_SESSION_ENV}
    tmux_env.update(launch_env)

    claude_cwd = str(Path.cwd())

    try:
        subprocess.check_call(
            [
                "tmux",
                "-S",
                str(tmux_socket),
                "new-session",
                "-d",
                "-s",
                tmux_session,
                "-x",
                "220",
                "-y",
                "50",
                "-c",
                claude_cwd,
                str(launcher),
            ],
            env=tmux_env,
        )

        write_tmux_target(
            bridge_dir,
            socket_path=tmux_socket,
            tmux_target=tmux_target,
        )

        server_json = bridge_dir / "server.json"
        deadline = time.monotonic() + _BRIDGE_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if server_json.exists():
                break
            time.sleep(1.0)
        else:
            pytest.fail(
                f"MCP bridge server.json not found in {bridge_dir} within "
                f"{_BRIDGE_READY_TIMEOUT_S}s. "
                "Claude Code may have failed to start or the serve-mcp "
                "subprocess did not launch."
            )

        yield bridge_dir

    finally:
        with contextlib.suppress(Exception):
            subprocess.run(
                [
                    "tmux",
                    "-S",
                    str(tmux_socket),
                    "kill-session",
                    "-t",
                    tmux_session,
                ],
                check=False,
            )
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_assistant_text(session_snapshot: dict[str, Any]) -> str:
    """
    Extract all assistant message text from a session snapshot.

    :param session_snapshot: JSON response from ``GET /v1/sessions/{id}``.
    :returns: Concatenated text from all assistant messages.
    """
    parts: list[str] = []
    for item in session_snapshot.get("items", []):
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if item.get("type") != "message":
            continue
        role = data.get("role") if isinstance(data, dict) else None
        if role != "assistant":
            continue
        content = data.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def test_claude_native_session_tools_visible(
    http_client: httpx.Client,
    claude_native_ui_agent: str,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Session-discovery tools are visible in a claude-native MCP session.

    Verifies that ``sys_session_get_history`` and ``sys_session_list`` appear
    in Claude Code's MCP tool list when running in an Omnigent
    claude-native session. These tools are advertised via the runner's
    ``tool_relay.json`` and relayed through the MCP bridge.

    Flow:

    1. Skip unless opted in and ``claude`` / ``tmux`` are available.
    2. Create a runner-bound session with ``claude-native-ui``.
    3. Launch Claude Code in a private tmux window with mock LLM routing.
    4. Verify ``tool_relay.json`` contains ``sys_session_get_history``.
    5. Send a prompt asking Claude to list its omnigent MCP tools.
    6. Poll session items until Claude's response mentions
       ``sys_session_get_history``.
    7. Assert the response text contains ``sys_session_get_history``.

    **What breaks if this test fails:**

    - ``_ensure_comment_relay_started`` in ``runner/app.py`` does not
      include ``SysSessionGetHistoryTool`` / ``SysSessionListTool`` in the
      relay schemas, so ``tool_relay.json`` omits them.
    - The MCP bridge's ``_combined_mcp_tool_schemas`` does not read
      ``tool_relay.json``, so relay tools are invisible to Claude.
    - Claude Code's MCP client does not re-fetch tools after the
      ``notifications/tools/list_changed`` notification, so the relay
      tools added after startup are never discovered.

    :param http_client: HTTP client pointed at the live server.
    :param claude_native_ui_agent: Registered agent name.
    :param live_runner_id: Runner id the session is bound to.
    :param mock_llm_server_url: Mock LLM server base URL (no ``/v1`` suffix —
        the Anthropic SDK appends ``/v1/messages`` automatically).
    """
    if not os.environ.get(_RUN_GATE_ENV):
        pytest.skip(
            f"Set {_RUN_GATE_ENV}=1 to run. The interactive claude TUI's "
            "first-run onboarding/trust gates are version- and "
            "environment-dependent and block headless startup in CI."
        )
    if shutil.which("claude") is None:
        pytest.skip("'claude' CLI is not on PATH. Install and authenticate Claude Code.")
    if shutil.which("tmux") is None:
        pytest.skip("'tmux' is not on PATH. This test requires tmux.")

    session_id = create_runner_bound_session(
        http_client,
        agent_name=claude_native_ui_agent,
        runner_id=live_runner_id,
    )

    # Route the Claude CLI's LLM calls to the mock server.  ANTHROPIC_API_KEY
    # bypasses the CLI's OAuth login gate so no real account is needed.
    # CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS suppresses beta headers the mock
    # server does not expect.
    launch_env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": mock_llm_server_url,
        "ANTHROPIC_API_KEY": "mock-key",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }

    with _claude_code_session(session_id, model=None, launch_env=launch_env) as bridge_dir:
        # ── Step 4: Verify tool_relay.json has peek before asking Claude ──
        relay_file = bridge_dir / "tool_relay.json"
        # The relay is started by _ensure_comment_relay_started in the
        # runner's create_session_terminal handler, which fires BEFORE
        # Claude Code launches. By the time _claude_code_session yields,
        # server.json exists (the bridge is up), so tool_relay.json
        # should also be present. Poll briefly in case the runner is
        # still writing it.
        relay_deadline = time.monotonic() + 15.0
        relay_tools: list[str] = []
        while time.monotonic() < relay_deadline:
            if relay_file.exists():
                try:
                    relay_data = json.loads(relay_file.read_text())
                    relay_tools = [
                        t["name"]
                        for t in relay_data.get("tools", [])
                        if isinstance(t, dict) and isinstance(t.get("name"), str)
                    ]
                    if "sys_session_get_history" in relay_tools:
                        break
                except (json.JSONDecodeError, KeyError):
                    pass
            time.sleep(1.0)

        # If tool_relay.json is missing or doesn't have peek, the relay
        # wiring is broken — fail fast with a clear message rather than
        # waiting for the LLM prompt to time out.
        assert "sys_session_get_history" in relay_tools, (
            f"tool_relay.json does not advertise sys_session_get_history. "
            f"Found tools: {relay_tools}. Either the runner's "
            f"_ensure_comment_relay_started did not include "
            f"SysSessionGetHistoryTool in the relay schemas, or "
            f"tool_relay.json was not written before Claude launched."
        )
        assert "sys_session_list" in relay_tools, (
            f"tool_relay.json does not advertise sys_session_list. Found tools: {relay_tools}."
        )

        # ── Step 5: Ask Claude to list its omnigent MCP tools ──────────
        send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=(
                "List the names of ALL tools available to you from the "
                "'omnigent' MCP server. Just list the tool names, one per "
                "line, nothing else. Be complete — include every tool."
            ),
        )

        # ── Step 6: Poll session items for Claude's response ─────────────
        # Claude processes the injected message asynchronously in tmux.
        # The forwarder captures output and persists it as conversation
        # items. Poll until an assistant message appears that mentions
        # sys_session_get_history, or until timeout.
        deadline = time.monotonic() + _RESPONSE_POLL_TIMEOUT_S
        assistant_text = ""
        while time.monotonic() < deadline:
            resp = http_client.get(f"/v1/sessions/{session_id}")
            resp.raise_for_status()
            assistant_text = _extract_assistant_text(resp.json())
            # Claude may take a few turns to respond. Check for the
            # tool name in the accumulated text.
            if "sys_session_get_history" in assistant_text:
                break
            time.sleep(_RESPONSE_POLL_INTERVAL_S)

        # ── Step 7: Assert tool visibility ───────────────────────────────
        assert "sys_session_get_history" in assistant_text, (
            f"Claude's response does not mention sys_session_get_history. "
            f"The tool may not be visible via the MCP relay. "
            f"Assistant text: {assistant_text[:500]!r}"
        )
        # sys_session_list should also be mentioned since we asked for
        # ALL omnigent tools.
        assert "sys_session_list" in assistant_text, (
            f"Claude's response does not mention sys_session_list. "
            f"Assistant text: {assistant_text[:500]!r}"
        )


def test_claude_native_relay_advertises_broadened_orchestrator_surface(
    http_client: httpx.Client,
    claude_native_ui_agent: str,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The native relay advertises the full gated orchestrator surface.

    Regression-pins the cross-harness tool-delivery change: the runner's
    ``_ensure_comment_relay_started`` derives ``tool_relay.json`` from the
    session's ``ToolManager`` (filtered to ``_NATIVE_RELAY_BUILTIN_TOOLS``)
    rather than a hardcoded list, so claude-native — which ignores the
    harness ``tools`` list and sees only the relay — receives the
    always-on agent-discovery + session-read tools, gated exactly as
    non-native harnesses are.

    This asserts on ``tool_relay.json`` (the file the runner writes), not
    on Claude's response, so it is deterministic and independent of the
    LLM. The relay is written when the first turn dispatches
    (``_run_turn_bg`` → ``_ensure_comment_relay_started``), so the test
    sends a trigger message and then polls the file.

    **What breaks if this fails:** the relay assembly reverted to the
    hardcoded comment + ``sys_session_list`` / ``sys_session_get_history``
    set, so a claude-native orchestrator can no longer discover agents
    (``sys_agent_list``) or read a session's metadata
    (``sys_session_get_info``).

    This module's ``claude-native-ui`` fixture declares neither opt-in
    arm (no ``tools.agents``, no top-level ``spawn: true``), so the
    gated spawn-writes (``sys_session_create`` / ``send`` / ``close``)
    must be ABSENT — proving native honours the same gate as
    ``request.tools`` on non-native harnesses. (The real ``omnigent
    claude`` wrapper spec sets ``spawn: true`` and DOES get them.)

    :param http_client: HTTP client pointed at the live server.
    :param claude_native_ui_agent: Registered agent name.
    :param live_runner_id: Runner id the session is bound to.
    :param mock_llm_server_url: Mock LLM server base URL (no ``/v1`` suffix).
    """
    if not os.environ.get(_RUN_GATE_ENV):
        pytest.skip(f"Set {_RUN_GATE_ENV}=1 to run.")
    if shutil.which("claude") is None:
        pytest.skip("'claude' CLI is not on PATH.")
    if shutil.which("tmux") is None:
        pytest.skip("'tmux' is not on PATH.")

    session_id = create_runner_bound_session(
        http_client,
        agent_name=claude_native_ui_agent,
        runner_id=live_runner_id,
    )

    # Route the Claude CLI's LLM calls to the mock server.
    launch_env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": mock_llm_server_url,
        "ANTHROPIC_API_KEY": "mock-key",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }

    with _claude_code_session(session_id, model=None, launch_env=launch_env) as bridge_dir:
        # Trigger the first turn so the runner writes tool_relay.json via
        # _run_turn_bg → _ensure_comment_relay_started. (The relay is also
        # written on the create_session_terminal route; this synthetic
        # flow launches the terminal manually, so the turn is what fires
        # the relay write.) Post the event directly rather than via
        # send_user_message_to_session — that helper reads ``item_id`` from
        # the response, which runner-native sessions don't return.
        _evt = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
        _evt.raise_for_status()

        relay_file = bridge_dir / "tool_relay.json"
        deadline = time.monotonic() + _RESPONSE_POLL_TIMEOUT_S
        relay_tools: set[str] = set()
        while time.monotonic() < deadline:
            if relay_file.exists():
                try:
                    data = json.loads(relay_file.read_text())
                    relay_tools = {
                        t["name"]
                        for t in data.get("tools", [])
                        if isinstance(t, dict) and isinstance(t.get("name"), str)
                    }
                except (json.JSONDecodeError, KeyError):
                    relay_tools = set()
                # Wait until the relay has been populated past the static
                # bridge tools (sys_session_get_history is always-on).
                if "sys_session_get_history" in relay_tools:
                    break
            time.sleep(_RESPONSE_POLL_INTERVAL_S)

        # The broadened always-on surface my change adds must be present.
        broadened = {
            "sys_agent_list",
            "sys_agent_get",
            "sys_agent_download",
            "sys_session_get_info",
        }
        missing = broadened - relay_tools
        assert not missing, (
            f"tool_relay.json is missing broadened orchestrator tools {sorted(missing)}. "
            f"Relay advertised: {sorted(relay_tools)}. The runner's relay assembly "
            f"likely reverted to the hardcoded comment + session-read list instead of "
            f"deriving from ToolManager ∩ _NATIVE_RELAY_BUILTIN_TOOLS."
        )

        # This fixture's agent declares neither opt-in arm (no
        # tools.agents, no spawn: true), so the gated spawn-writes must
        # NOT be relayed — native gating parity with the ToolManager
        # gate non-native harnesses get via request.tools.
        gated_writes = {"sys_session_create", "sys_session_send", "sys_session_close"}
        leaked = gated_writes & relay_tools
        assert not leaked, (
            f"tool_relay.json leaked spawn-write tools {sorted(leaked)} for an agent "
            f"without any spawn opt-in — native relay gating diverged from the "
            f"ToolManager gate."
        )
