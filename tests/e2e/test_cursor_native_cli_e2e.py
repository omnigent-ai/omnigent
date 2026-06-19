"""End-to-end tests: ``omnigent cursor`` drives the native Cursor TUI.

The cursor-native sibling of ``test_codex_native_cli_cwd_e2e`` /
``test_codex_native_cli_resume_e2e``. ``cursor-native`` is a *terminal-first*
harness: ``omnigent cursor`` launches the official ``cursor-agent`` TUI in a
runner-owned tmux pane, and each web-UI turn is injected into that pane
(bracketed paste + Enter) by
:class:`omnigent.inner.cursor_native_executor.CursorNativeExecutor`. The TUI's
own conversation store is tailed by
:mod:`omnigent.cursor_native_forwarder`, which mirrors ``cursor-agent``'s
replies back onto the Omnigent conversation as assistant items.

These tests drive the full stack the way a user does — spawn ``omnigent
cursor``, then talk to the session **through the server** (``POST
/v1/sessions/{id}/events``, the web-UI path) — and assert on the persisted
assistant items:

* **smoke** — inject a prompt that makes ``cursor-agent`` emit a unique marker
  word, and confirm the marker comes back as an assistant item. This exercises
  CLI parse -> daemon runner spawn -> cursor terminal launch -> tmux injection
  -> ``cursor-agent`` turn -> forwarder mirror -> conversation store.
* **cwd** — drop a marker file in the launch cwd and ask ``cursor-agent`` to
  read it. The file exists only in the launch directory (never in the runner's
  spec-bundle dir), so a correct answer proves both that the TUI launched in
  the launch cwd *and* that its built-in Read tool ran.

Unlike the SDK ``cursor`` harness (``test_per_harness_cursor``), cursor-native
authenticates from the **ambient ``cursor-agent login``** under ``$HOME/.cursor``
— there is no ``CURSOR_API_KEY``. The TUI is launched with ``-f`` (Cursor's
force/trust flag) so it neither blocks on the per-directory "Workspace Trust"
prompt nor on per-tool approval prompts — either of which would hang the pane.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* **Opt-in only**: set ``OMNIGENT_E2E_CURSOR_NATIVE=1`` to run. Like the other
  native-TUI e2e tests, cursor-native needs an interactive ``cursor-agent
  login`` anchored to the real ``$HOME`` and a ``tmux`` binary; the
  ``cursor-agent`` binary may be present on CI but unauthenticated, which would
  hang the TUI. The env-var gate keeps it out of CI; a developer with a
  logged-in Cursor opts in. ``tmux`` and ``cursor-agent`` on ``PATH`` are also
  required (checked below).
* Run it like the codex-native CLI tests::

    OMNIGENT_E2E_CURSOR_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_cursor_native_cli_e2e.py \
        --profile oss \
        --llm-api-key "$(databricks auth token -p oss \
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
        -v

  The ``--profile`` / ``--llm-api-key`` only satisfy the test server's startup
  (``resume_test_server``); the ``cursor-agent`` turn itself authenticates via
  the ambient Cursor login, not the Databricks gateway.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.cursor_native_bridge import bridge_dir_for_session_id, kill_session
from tests.e2e._native_resume_helpers import (
    PtyHandle,
    cli_env,
    inject_user_message,
    omnigent_console_script,
    poll_for_assistant_marker,
    spawn_cli_background,
    wait_for_conversation_id,
    wait_for_terminal_ready,
)

# ``resume_test_server`` is provided by tests/e2e/conftest.py (the allow-list-
# free server the CLI wrapper's self-spawned host daemon can register against).

# Opt-in only — see module docstring. Binary presence is not a sufficient gate
# (present-but-unauthenticated hangs the TUI), so require the explicit env var,
# plus the two binaries the terminal-first harness needs on PATH.
pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CURSOR_NATIVE") != "1"
    or shutil.which("cursor-agent") is None
    or shutil.which("tmux") is None,
    reason=(
        "cursor-native CLI e2e needs an interactive `cursor-agent login` and a "
        "`tmux` binary; set OMNIGENT_E2E_CURSOR_NATIVE=1 (and have `cursor-agent` "
        "installed + logged in and `tmux` on PATH) to run"
    ),
)

# Cursor's force/trust flag, passed as a raw cursor-agent arg. Clears the
# per-directory "Workspace Trust" gate and per-tool approval prompts so the TUI
# never blocks the tmux pane waiting on a y/n the test can't answer.
_FORCE_FLAG = "-f"

_CWD_MARKER_FILE = "CWD_MARKER.txt"

# cursor-agent cold-starts the TUI and round-trips to Cursor's backend; mirror
# the headroom the codex-native CLI tests allow on a contended host.
_CONV_ID_TIMEOUT = 120.0
_TERMINAL_READY_TIMEOUT = 90.0
_REPLY_TIMEOUT = 180.0
_COLD_RESUME_HINT = (
    "Terminal not running - starting a fresh Cursor session (prior chat not restored)."
)


def _write_json_line(stdin, payload: dict[str, object]) -> None:
    stdin.write(json.dumps(payload) + "\n")
    stdin.flush()


def _read_json_line(stdout, *, timeout_s: float) -> dict[str, object]:
    lines: queue.Queue[str] = queue.Queue(maxsize=1)

    def reader() -> None:
        lines.put(stdout.readline())

    threading.Thread(target=reader, daemon=True).start()
    try:
        line = lines.get(timeout=timeout_s)
    except queue.Empty as exc:
        raise AssertionError(f"timed out waiting for MCP response after {timeout_s}s") from exc
    assert line, "MCP process closed stdout"
    return json.loads(line)


def _read_jsonrpc_response(stdout, *, response_id: int, timeout_s: float) -> dict[str, object]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        payload = _read_json_line(stdout, timeout_s=max(0.1, deadline - time.time()))
        if payload.get("id") == response_id:
            return payload
    raise AssertionError(f"timed out waiting for MCP response id={response_id}")


def _wait_for_output_contains(handle: PtyHandle, needle: str, *, timeout: float) -> str:
    """Poll a PTY handle until *needle* appears in its decoded output."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = handle.output()
        if needle in output:
            return output
        time.sleep(0.2)
    output = handle.output()
    raise AssertionError(
        f"did not see {needle!r} within {timeout}s; output tail:\n{output[-2000:]}"
    )


def test_cursor_native_cli_smoke(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """A cursor-native turn driven through the server returns the model's reply.

    Spawns a backgrounded ``omnigent cursor`` session, waits for its terminal
    to register, injects (via ``/events`` — the web-UI path) a prompt asking
    ``cursor-agent`` to emit a unique marker word, and asserts the marker comes
    back as an assistant item. The marker is a fresh per-run nonce so a match
    cannot be coincidental and a parallel run cannot leak it.

    This is the end-to-end smoke gate for the cursor-native harness: it covers
    the whole path from CLI parse through tmux injection to the forwarder
    mirroring ``cursor-agent``'s reply onto the conversation store.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; its ``pwd`` subdir is the launch cwd.
    :param request: Pytest request — reads ``--profile`` for the test server.
    """
    profile = request.config.getoption("--profile")
    assert profile, "this test requires --profile (e.g. --profile oss) for the test server"

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()
    marker = f"CURSOR_{uuid.uuid4().hex[:8].upper()}"

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "cursor", "--server", resume_test_server, _FORCE_FLAG],
        env=cli_env(profile=profile),
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )
            inject_user_message(
                client,
                conversation_id=conversation_id,
                text=f"Reply with ONLY this exact word and nothing else: {marker}",
            )
            try:
                poll_for_assistant_marker(
                    client,
                    conversation_id=conversation_id,
                    marker=marker,
                    timeout=_REPLY_TIMEOUT,
                )
            except AssertionError as exc:
                raise AssertionError(
                    f"`omnigent cursor` did not return marker {marker!r}. The "
                    "cursor-native path regressed somewhere between tmux injection, "
                    "the cursor-agent turn, and the forwarder mirroring the reply "
                    f"onto the conversation.\n\nCLI output tail:\n{handle.output()[-2000:]}"
                ) from exc
    finally:
        handle.terminate()


def test_cursor_native_cli_runs_in_launch_cwd(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """``omnigent cursor`` launches ``cursor-agent`` in the directory it was run from.

    Spawns a backgrounded ``omnigent cursor`` whose process cwd is a temp
    directory containing a marker file, then injects (via the server, the
    web-UI path) a request to read that file. The marker exists only in the
    launch cwd (never in the runner's spec-bundle dir), so it can come back
    only if the wrapper launched the TUI in the launch directory *and*
    ``cursor-agent``'s built-in Read tool ran (the ``-f`` flag auto-approves
    it). The cursor-native sibling of ``test_codex_native_cli_runs_in_launch_cwd``.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; its ``pwd`` subdir is the launch cwd.
    :param request: Pytest request — reads ``--profile`` for the test server.
    """
    profile = request.config.getoption("--profile")
    assert profile, "this test requires --profile (e.g. --profile oss) for the test server"

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()
    marker = f"PWD_{uuid.uuid4().hex[:6].upper()}"
    (pwd_dir / _CWD_MARKER_FILE).write_text(marker + "\n")

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "cursor", "--server", resume_test_server, _FORCE_FLAG],
        env=cli_env(profile=profile),
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )
            inject_user_message(
                client,
                conversation_id=conversation_id,
                text=(
                    f"Read the file {_CWD_MARKER_FILE} in your current directory "
                    "and reply with its exact contents and nothing else."
                ),
            )
            try:
                poll_for_assistant_marker(
                    client,
                    conversation_id=conversation_id,
                    marker=marker,
                    timeout=_REPLY_TIMEOUT,
                )
            except AssertionError as exc:
                raise AssertionError(
                    f"`omnigent cursor` did not return marker {marker!r} from "
                    f"{_CWD_MARKER_FILE} — it did not run cursor-agent in its launch "
                    "cwd (the wrapper-path cwd resolution regressed, likely the "
                    f"spec-bundle dir).\n\nCLI output tail:\n{handle.output()[-2000:]}"
                ) from exc
    finally:
        handle.terminate()


def test_cursor_native_cli_resume_warns_when_terminal_was_killed(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Live reattach stays quiet, but cold resume tells the truth.

    This is the e2e guard for the UX bug in ``CURSOR_NATIVE_AUDIT_FIXES.md``
    item #2. A second ``omnigent cursor --resume <conv>`` while the original
    tmux pane is still alive should attach to the live terminal and must not
    print the cold-resume warning. After killing that tmux session, the same
    resume command necessarily starts a fresh ``cursor-agent`` TUI with no
    prior Cursor chat, so the CLI must print the honest stderr hint before it
    attaches.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; its ``pwd`` subdir is the launch cwd.
    :param request: Pytest request - reads ``--profile`` for the test server.
    """
    profile = request.config.getoption("--profile")
    assert profile, "this test requires --profile (e.g. --profile oss) for the test server"

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()

    omni = str(omnigent_console_script())
    base = [omni, "cursor", "--server", resume_test_server]
    primary = spawn_cli_background(
        [*base, _FORCE_FLAG],
        env=cli_env(profile=profile),
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(primary, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )

        live_resume = spawn_cli_background(
            [*base, "--resume", conversation_id, _FORCE_FLAG],
            env=cli_env(profile=profile),
            cwd=str(pwd_dir),
        )
        try:
            _wait_for_output_contains(
                live_resume,
                f"/c/{conversation_id}",
                timeout=_CONV_ID_TIMEOUT,
            )
            # The cold-resume hint is printed immediately after the Web UI line
            # and before the blocking tmux attach. Wait briefly past that point
            # before asserting absence, so this catches a misplaced hint on live
            # reattach rather than racing ahead of it.
            time.sleep(2.0)
            live_output = live_resume.output()
            assert _COLD_RESUME_HINT not in live_output, (
                "live cursor-native resume incorrectly printed the cold-resume hint; "
                "it should be a true reattach while the terminal is running."
            )
        finally:
            live_resume.terminate()

        kill_session(bridge_dir_for_session_id(conversation_id), timeout_s=30.0)
        primary.terminate()

        cold_resume = spawn_cli_background(
            [*base, "--resume", conversation_id, _FORCE_FLAG],
            env=cli_env(profile=profile),
            cwd=str(pwd_dir),
        )
        try:
            _wait_for_output_contains(
                cold_resume,
                _COLD_RESUME_HINT,
                timeout=_CONV_ID_TIMEOUT,
            )
            with httpx.Client(base_url=resume_test_server, timeout=30) as client:
                wait_for_terminal_ready(
                    client,
                    conversation_id=conversation_id,
                    harness="cursor",
                    timeout=_TERMINAL_READY_TIMEOUT,
                )
        finally:
            cold_resume.terminate()
    finally:
        primary.terminate()


def test_cursor_native_cli_exposes_omnigent_mcp_tools(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """``omnigent cursor`` wires Omnigent tools into Cursor's native MCP client.

    Spawns a real cursor-native session, waits for the runner-owned Cursor TUI
    to start, then asks ``cursor-agent``'s own MCP subcommand to discover the
    workspace-scoped ``omnigent`` server. This catches the regressions that made
    Cursor-native unable to call ``sys_*`` tools: missing ``bridge.json``, a
    disabled workspace MCP server, lost ``TMPDIR`` under ``python -I``, missing
    auto-approval config, and stdio framing incompatibility.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; its ``pwd`` subdir is the launch cwd.
    :param request: Pytest request — reads ``--profile`` for the test server.
    """
    profile = request.config.getoption("--profile")
    if not profile:
        pytest.skip("requires --profile (e.g. --profile oss) for the test server")

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()

    omni = str(omnigent_console_script())
    env = cli_env(profile=profile)
    handle = spawn_cli_background(
        [omni, "cursor", "--server", resume_test_server, _FORCE_FLAG],
        env=env,
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )

        bridge_dir = bridge_dir_for_session_id(conversation_id)
        mcp_config_path = pwd_dir / ".cursor" / "mcp.json"
        assert mcp_config_path.is_file(), "cursor-native did not write .cursor/mcp.json"
        assert (bridge_dir / "bridge.json").is_file(), "serve-mcp token bridge was not written"
        assert (bridge_dir / "tool_relay.json").is_file(), "Omnigent tool relay was not started"

        payload = json.loads(mcp_config_path.read_text(encoding="utf-8"))
        server = payload["mcpServers"]["omnigent"]
        assert server["env"]["TMPDIR"]
        assert "--bridge-dir" in server["args"]
        assert str(bridge_dir) in server["args"]
        assert "sys_session_list" in server["autoApprove"]
        assert "sys_os_read" in server["autoApprove"]

        cli_config_path = Path.home() / ".cursor" / "cli-config.json"
        if cli_config_path.is_file():
            cli_config = json.loads(cli_config_path.read_text(encoding="utf-8"))
            allow = cli_config.get("permissions", {}).get("allow", [])
            assert "Mcp(omnigent:sys_session_list)" in allow
            assert "Mcp(omnigent:sys_os_read)" in allow

        listed = subprocess.run(
            ["cursor-agent", "mcp", "list"],
            cwd=pwd_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=45,
            check=False,
        )
        assert listed.returncode == 0, listed.stdout
        assert "omnigent" in listed.stdout
        assert "ready" in listed.stdout.lower()

        tools = subprocess.run(
            ["cursor-agent", "mcp", "list-tools", "omnigent"],
            cwd=pwd_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=45,
            check=False,
        )
        assert tools.returncode == 0, tools.stdout
        assert "sys_session_list" in tools.stdout
        assert "sys_session_send" in tools.stdout
        assert "sys_os_read" in tools.stdout
    finally:
        handle.terminate()


def test_cursor_native_cli_mcp_can_call_sys_tool(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Cursor-native's generated Omnigent MCP server can call ``sys_*`` tools.

    Launches a real Cursor TUI, then calls the same generated ``.cursor/mcp.json``
    Omnigent relay that Cursor uses. The tool call is direct JSON-RPC over the
    generated stdio server rather than model-steered prose, so it deterministically
    proves the Cursor-native MCP wiring can execute relayed ``sys_*`` tools.
    """
    profile = request.config.getoption("--profile")
    if not profile:
        pytest.skip("requires --profile (e.g. --profile oss) for the test server")

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()

    omni = str(omnigent_console_script())
    env = cli_env(profile=profile)
    handle = spawn_cli_background(
        [omni, "cursor", "--server", resume_test_server, _FORCE_FLAG],
        env=env,
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )

        bridge_dir = bridge_dir_for_session_id(conversation_id)
        mcp_config_path = pwd_dir / ".cursor" / "mcp.json"
        payload = json.loads(mcp_config_path.read_text(encoding="utf-8"))
        server = payload["mcpServers"]["omnigent"]
        proc_env = {**os.environ, **env, **server.get("env", {})}
        proc = subprocess.Popen(
            [server.get("command") or sys.executable, *server["args"]],
            cwd=pwd_dir,
            env=proc_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                assert (bridge_dir / "tool_relay.json").is_file()
                _write_json_line(
                    proc.stdin,
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
                initialize = _read_jsonrpc_response(proc.stdout, response_id=1, timeout_s=10.0)
                assert initialize["id"] == 1

                _write_json_line(
                    proc.stdin,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "sys_session_list",
                            "arguments": {},
                        },
                    },
                )
                tool_response = _read_jsonrpc_response(
                    proc.stdout,
                    response_id=2,
                    timeout_s=30.0,
                )
                assert "error" not in tool_response, tool_response
                text = tool_response["result"]["content"][0]["text"]
                decoded = json.loads(text)
                assert "error" not in decoded
                assert "sessions" in decoded or "child_sessions" in decoded or "items" in decoded
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
    finally:
        handle.terminate()
