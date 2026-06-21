"""End-to-end regression test for the claude-native first-message readiness gate.

Covers the host-spawned Web UI flow for ``claude-native-ui``: list hosts
→ create a session with ``host_id`` + ``workspace`` → the server launches
a runner on the host → the runner auto-creates a Claude Code terminal →
the user's first message is injected into that terminal.

The bug this guards against (the "AWAIT TERMINAL UP" race): the runner
advertises ``tmux.json`` as soon as the tmux session exists, but Claude
Code's input box mounts several seconds later, and Claude flushes any
pending terminal input when its TUI initializes. So a first message
typed into that gap is silently dropped — the UI shows "Working…"
forever and nothing is persisted. ``inject_user_message`` now waits for
Claude's input prompt to render before typing (see
``omnigent.claude_native_bridge._wait_for_claude_prompt_ready``).

Making the race deterministic
-----------------------------
On a warm machine Claude can boot fast enough that the first message
lands even without the gate, which would make this a flaky / vacuous
test. To pin the regression, the runner launches ``claude`` through a
wrapper (first on ``PATH``) that:

1. ``sleep``\\ s for :data:`_BOOT_DELAY_S` so the input box reliably
   renders *after* the runner would otherwise inject, and
2. flushes pending terminal input (``termios.tcflush``) right before
   exec'ing the real ``claude`` — faithfully modeling Claude Code's
   own boot-time input flush, which is what discards early keystrokes.

With the wrapper, a pre-prompt inject is reliably lost: **gate absent →
this test fails (marker never answered); gate present → it passes.**
Verified red→green by toggling the gate.

Environment requirements (why this is opt-in, not pure-CI):

* **Opt-in only**: set ``OMNIGENT_E2E_CLAUDE_NATIVE=1`` to run. claude-native
  needs an *interactive* Claude login (OAuth/Enterprise) anchored to the
  real ``$HOME`` — it cannot be relocated into CI (verified: a copied
  ``~/.claude.json`` reports "Not logged in"). The ``claude`` binary IS
  present in CI (claude-sdk installs it), so gating on binary presence
  alone would let this run unauthenticated and hang the TUI until the
  shard times out. The env-var gate keeps it out of CI entirely; a
  developer with a logged-in Claude opts in explicitly.
  The tmux-level readiness test at the bottom needs no Claude login and
  DOES run in CI, gated only on ``tmux`` (installed on the e2e runners).
* It runs the daemon under the real HOME with the real Claude login —
  like ``claude_coder`` relies on the CLI's own session.
* The workspace folder must be trusted in ``~/.claude.json`` or Claude
  shows its folder-trust dialog, which blocks (and confounds) the gate.
  The test trusts a temp workspace and restores the original config on
  teardown. It does NOT touch ``~/.omnigent/config.yaml``: the test
  server is a fresh random-port instance, so the daemon's real host
  identity registers there with no collision.

claude-native authenticates through the Claude CLI's own session, so
``--llm-api-key`` only satisfies the server fixture. Derive it from the
oss profile::

    OMNIGENT_E2E_CLAUDE_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_host_claude_native_e2e.py \
        --profile oss \
        --llm-api-key "$(databricks auth token -p oss \
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
        -v
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from omnigent import claude_native_bridge
from tests.e2e.helpers import POLL_INTERVAL_S

# The full-stack claude-native tests need a real *interactive* Claude
# login (OAuth/Enterprise): the `claude` binary IS present in CI but is
# not logged in, so the TUI never reaches its input box and would hang
# until the shard times out. Gate each of those tests with this marker so
# they skip in CI. NOTE: this is a per-test decorator, deliberately NOT a
# module-level ``pytestmark`` — the tmux readiness test at the bottom
# needs no Claude login and MUST run in CI, so it is left undecorated.
_needs_logged_in_claude = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1" or shutil.which("claude") is None,
    reason=(
        "claude-native full-stack e2e needs an interactive Claude login; set "
        "OMNIGENT_E2E_CLAUDE_NATIVE=1 (and have `claude` installed + logged in) to run"
    ),
)

# The built-in agent the server auto-registers for the Web UI's
# "Claude Code" option (see server.app._ensure_default_agents).
_CLAUDE_NATIVE_AGENT_NAME = "claude-native-ui"

# Seconds the claude wrapper sleeps before exec'ing the real binary.
# Must comfortably exceed the runner's launch→inject latency so that,
# without the gate, the inject lands during the wrapper's sleep (before
# the input box renders) and is flushed.
_BOOT_DELAY_S = 8


@contextmanager
def _workspace_trusted_in_claude_config(workspace: Path) -> Iterator[None]:
    """
    Mark *workspace* trusted in ``~/.claude.json`` for the test's duration.

    Without this, fresh-config Claude shows its folder-trust dialog
    instead of the input box, which both blocks injection and (because
    the dialog's selector is also ``❯``) confounds the readiness gate.
    The original file bytes are restored on exit so the developer's
    config is left untouched.

    :param workspace: Absolute workspace path the session will start in,
        e.g. ``Path("/tmp/pytest-.../cn_ws")``.
    :returns: Iterator yielding once the trust entry is written.
    """
    config_path = Path.home() / ".claude.json"
    original = config_path.read_bytes() if config_path.exists() else None
    config = json.loads(original) if original is not None else {}
    config.setdefault("projects", {}).setdefault(str(workspace), {})["hasTrustDialogAccepted"] = (
        True
    )
    config_path.write_text(json.dumps(config))
    try:
        yield
    finally:
        if original is not None:
            config_path.write_bytes(original)
        else:
            config_path.unlink(missing_ok=True)


def _write_claude_boot_delay_wrapper(bin_dir: Path) -> None:
    """
    Write a ``claude`` shim that delays TUI boot, then execs real claude.

    Placed first on the daemon's ``PATH`` so the runner's auto-created
    terminal launches it instead of the real binary. The shim widens the
    boot window (:data:`_BOOT_DELAY_S`) and flushes pending terminal
    input before exec — modeling Claude's own boot-time flush so a
    pre-prompt inject is deterministically lost without the gate.

    :param bin_dir: Directory prepended to ``PATH``; the shim is written
        as ``bin_dir/claude``.
    :returns: None.
    :raises RuntimeError: If the real ``claude`` binary can't be found
        (the module-level skip should prevent this).
    """
    real_claude = shutil.which("claude")
    if real_claude is None:
        raise RuntimeError("real claude binary not found on PATH")
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"sleep {_BOOT_DELAY_S}\n"
        "# Model Claude Code's boot-time input flush: discard anything\n"
        "# typed into the pane before the TUI became interactive.\n"
        "python3 -c "
        "'import termios,sys; termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)' "
        "2>/dev/null || true\n"
        f'exec "{real_claude}" "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _spawn_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
    extra_path_dir: Path | None = None,
) -> subprocess.Popen[bytes]:
    """
    Spawn an ``omnigent host`` daemon under the real ``$HOME``.

    claude-native needs the real Claude login (auth can't be relocated),
    so this inherits the real environment. The daemon registers the
    machine's real host identity with *live_server* — safe because
    *live_server* is a fresh random-port instance with no other host of
    that id connected.

    :param tmp_path: Per-test temp dir for the daemon log.
    :param live_server: Test server URL the daemon registers with, e.g.
        ``"http://localhost:18501"``.
    :param extra_path_dir: Optional directory prepended to ``PATH`` so a
        shim (e.g. a boot-delay ``claude`` wrapper) takes precedence
        over the real binary. ``None`` leaves ``PATH`` untouched.
    :returns: The spawned daemon subprocess handle.
    """
    env = {**os.environ}
    if extra_path_dir is not None:
        env["PATH"] = f"{extra_path_dir}{os.pathsep}{os.environ['PATH']}"
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


def _spawn_host_daemon_with_claude_shim(
    *,
    tmp_path: Path,
    live_server: str,
) -> subprocess.Popen[bytes]:
    """
    Spawn a host daemon with the boot-delay ``claude`` shim on ``PATH``.

    Thin wrapper over :func:`_spawn_host_daemon` that writes the
    boot-delay wrapper into a temp ``bin`` dir and prepends it to
    ``PATH`` so the runner's auto-created terminal launches the shim
    instead of the real ``claude``.

    :param tmp_path: Per-test temp dir for the shim and the daemon log.
    :param live_server: Test server URL the daemon registers with, e.g.
        ``"http://localhost:18501"``.
    :returns: The spawned daemon subprocess handle.
    """
    bin_dir = tmp_path / "shim_bin"
    bin_dir.mkdir()
    _write_claude_boot_delay_wrapper(bin_dir)
    return _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server, extra_path_dir=bin_dir)


def _online_host_id(client: httpx.Client, timeout: float = 30.0) -> str:
    """
    Poll ``GET /v1/hosts`` until exactly one host is online; return its id.

    :param client: HTTP client pointed at the test server.
    :param timeout: Max seconds to wait for the daemon to register.
    :returns: The online host's ``host_id``.
    :raises AssertionError: If no host comes online within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _claude_native_agent_id(client: httpx.Client) -> str:
    """
    Return the durable id of the auto-registered ``claude-native-ui`` agent.

    :param client: HTTP client pointed at the test server.
    :returns: The ``"ag_..."`` id for ``claude-native-ui``.
    :raises AssertionError: If the server did not auto-register it.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == _CLAUDE_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(
        f"{_CLAUDE_NATIVE_AGENT_NAME!r} not registered on the server "
        "(expected from _ensure_default_agents at startup)"
    )


def _assistant_text(item: dict[str, object]) -> str:
    """
    Extract concatenated assistant text from a session item.

    :param item: One element of ``GET /v1/sessions/{id}/items`` data,
        e.g. ``{"role": "assistant", "content": [{"type":
        "output_text", "text": "..."}]}``.
    :returns: The joined text of all blocks, or ``""`` for non-assistant
        items or items without text blocks.
    """
    if item.get("role") != "assistant":
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _poll_for_assistant_marker(
    client: httpx.Client,
    *,
    session_id: str,
    marker: str,
    timeout: float,
) -> str:
    """
    Poll session items until an assistant message contains *marker*.

    The transcript forwarder mirrors Claude's terminal response back into
    the session as an assistant item, so the marker appearing proves the
    first message was delivered (the readiness gate worked) and answered.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param marker: The literal string Claude was asked to echo, e.g.
        ``"AWAITUP_3F9C1A"``.
    :param timeout: Max seconds to wait for the response.
    :returns: The matching assistant message text.
    :raises AssertionError: If no assistant message contains *marker*
        within *timeout* (the dropped-first-message regression).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 50, "order": "asc"})
        if resp.status_code == 200:
            for item in resp.json().get("data", []):
                text = _assistant_text(item)
                if marker in text:
                    return text
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"No assistant message containing {marker!r} within {timeout}s — "
        "the first message was dropped (readiness gate regression)."
    )


@_needs_logged_in_claude
def test_claude_native_first_message_survives_terminal_boot(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A claude-native session's first message lands even when sent at once.

    Golden path for the Web UI "Claude Code" flow with a deliberately
    delayed Claude boot (see module docstring): connect host → create
    session with host_id + workspace → send the first message
    immediately (racing Claude's TUI boot) → assert Claude answers with
    the marker. Without the AWAIT-TERMINAL-UP gate the keystrokes are
    flushed during boot and this times out.
    """
    workspace = tmp_path / "cn_ws"
    workspace.mkdir()
    marker = f"AWAITUP_{uuid.uuid4().hex[:6].upper()}"

    with _workspace_trusted_in_claude_config(workspace):
        daemon = _spawn_host_daemon_with_claude_shim(tmp_path=tmp_path, live_server=live_server)
        try:
            host_id = _online_host_id(http_client, timeout=30.0)
            agent_id = _claude_native_agent_id(http_client)

            # Create the claude-native session bound to the host. The
            # server launches the runner + auto-creates the Claude
            # terminal asynchronously from here.
            create = http_client.post(
                "/v1/sessions",
                json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
                timeout=60.0,
            )
            create.raise_for_status()
            session_id = create.json()["id"]

            # Fire the FIRST message immediately — the racy path. The gate
            # must hold it until Claude's input box renders (past the
            # shim's boot delay + flush).
            event = http_client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Reply with exactly one word: {marker}",
                            }
                        ],
                    },
                },
                timeout=30.0,
            )
            event.raise_for_status()

            text = _poll_for_assistant_marker(
                http_client,
                session_id=session_id,
                marker=marker,
                # Generous: shim sleep + Claude boot + a real LLM turn.
                timeout=180.0,
            )
            assert marker in text, f"marker {marker!r} missing from response: {text!r}"
        finally:
            daemon.send_signal(signal.SIGTERM)
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()


def _plant_poisoned_omnigent_package(workspace: Path) -> None:
    """
    Write a booby-trapped ``omnigent/`` package inside *workspace*.

    Claude Code runs its hook subprocesses (and the relay MCP server)
    with the cwd set to the session's workspace. Absent ``python -I``,
    Python prepends that cwd to ``sys.path[0]``, so ``import omnigent``
    in the hook resolves to whatever ``omnigent/`` lives in the
    workspace -- not the installed package. This plants a copy whose
    ``__init__`` raises on import, faithfully modeling the real failure
    mode: a git worktree checked out in the workspace whose ``omnigent``
    is on a branch lacking the expected hook handlers.

    With the ``-I`` fix the cwd is excluded from ``sys.path``, the real
    installed package imports, ``record_hook_event`` runs, and the
    transcript forwarder mirrors Claude's reply back into the session.
    Without it, the hook crashes on import, no transcript path is ever
    recorded, and the assistant marker never appears.

    :param workspace: Absolute workspace path the session starts in; the
        package is written as ``workspace/omnigent/__init__.py``.
    :returns: None.
    """
    pkg_dir = workspace / "omnigent"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text(
        "raise RuntimeError(\n"
        '    "POISONED omnigent package imported from the session cwd -- "\n'
        '    "python -I should have excluded the workspace from sys.path"\n'
        ")\n"
    )


@_needs_logged_in_claude
def test_claude_native_hooks_ignore_workspace_omnigent_package(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    Hooks import the installed ``omnigent``, not a workspace shadow.

    Regression for the ``python -I`` fix (cwd import shadowing). The
    workspace contains a poisoned ``omnigent/`` package that raises on
    import. Claude Code runs hook subprocesses with cwd set to that
    workspace, so without ``-I`` the hook imports the poisoned copy and
    crashes -- ``record_hook_event`` never runs, the forwarder never
    learns the transcript path, and the first message's reply never
    mirrors back into the session.

    With ``-I`` (isolated mode) the cwd is excluded from ``sys.path``,
    the real package imports, and Claude's marker reply surfaces as an
    assistant item. Verified red->green by toggling the ``-I`` flag in
    ``build_hook_settings`` / ``build_mcp_config``.
    """
    workspace = tmp_path / "cn_shadow_ws"
    workspace.mkdir()
    _plant_poisoned_omnigent_package(workspace)
    marker = f"NOSHADOW_{uuid.uuid4().hex[:6].upper()}"

    with _workspace_trusted_in_claude_config(workspace):
        # No boot-delay shim here: this test pins the import-path fix,
        # not the readiness gate, so the real ``claude`` is fine.
        daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
        try:
            host_id = _online_host_id(http_client, timeout=30.0)
            agent_id = _claude_native_agent_id(http_client)

            create = http_client.post(
                "/v1/sessions",
                json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
                timeout=60.0,
            )
            create.raise_for_status()
            session_id = create.json()["id"]

            event = http_client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Reply with exactly one word: {marker}",
                            }
                        ],
                    },
                },
                timeout=30.0,
            )
            event.raise_for_status()

            # The marker only surfaces if the hook subprocess imported
            # the real package: it records the transcript path that the
            # forwarder tails to mirror Claude's reply. A poisoned-import
            # crash (no -I) breaks that chain and this times out.
            text = _poll_for_assistant_marker(
                http_client,
                session_id=session_id,
                marker=marker,
                # Claude boot + a real LLM turn (no shim delay here).
                timeout=180.0,
            )
            assert marker in text, f"marker {marker!r} missing from response: {text!r}"
        finally:
            daemon.send_signal(signal.SIGTERM)
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()


@_needs_logged_in_claude
def test_claude_native_message_not_duplicated_on_cold_start(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    Each item appears exactly once after the first turn on a cold session.

    Regression for the double-forwarder race: on cold start two concurrent
    code paths could both reach ``_auto_create_claude_terminal`` — the
    runner's ``_on_runner_connect`` (via ``create_session``, which holds
    ``_claude_terminal_ensure_locks``) and the first-message path's
    ``_ensure_native_terminal_ready`` (via ``create_session_terminal`` with
    ``ensure_native_terminal=True``, which was previously unguarded).

    Each call spawned an independent transcript forwarder task; each
    forwarder independently posted every transcript item via
    ``external_conversation_item`` — doubling every user message and
    assistant response in the DB. The fix adds
    ``_claude_terminal_ensure_locks`` to the ``create_session_terminal``
    claude path so the second concurrent caller finds the already-created
    terminal and returns without spawning a second forwarder.

    The race window is widest on remote hosts where
    ``_auto_create_claude_terminal`` makes several sequential HTTP calls
    back to the AP server (seconds of latency). Locally the window is
    sub-millisecond and the race rarely fires without artificial delay, so
    the unit test ``test_claude_terminal_ensure_concurrent_calls_create_once``
    (in ``tests/runner/test_session_resources.py``) covers the concurrent
    lock semantics deterministically. This test covers the end-to-end
    path: one turn → exactly one user item, exactly one assistant item.
    """
    workspace = tmp_path / "cn_dedup_ws"
    workspace.mkdir()
    marker = f"DEDUP_{uuid.uuid4().hex[:6].upper()}"

    with _workspace_trusted_in_claude_config(workspace):
        daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
        try:
            host_id = _online_host_id(http_client, timeout=30.0)
            agent_id = _claude_native_agent_id(http_client)

            create = http_client.post(
                "/v1/sessions",
                json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
                timeout=60.0,
            )
            create.raise_for_status()
            session_id = create.json()["id"]

            # Send the first message immediately so it races terminal creation.
            event = http_client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Reply with exactly one word: {marker}",
                            }
                        ],
                    },
                },
                timeout=30.0,
            )
            event.raise_for_status()

            # Wait until Claude has replied — proves the forwarder persisted
            # the assistant response and the turn completed.
            _poll_for_assistant_marker(
                http_client,
                session_id=session_id,
                marker=marker,
                timeout=180.0,
            )

            # Fetch all items and count by role. With the double-forwarder
            # race, both the user turn and the assistant turn get posted
            # twice — each count would be 2, not 1.
            resp = http_client.get(
                f"/v1/sessions/{session_id}/items",
                params={"limit": 50, "order": "asc"},
            )
            resp.raise_for_status()
            items = resp.json().get("data", [])

            user_items = [i for i in items if i.get("role") == "user"]
            assistant_items = [i for i in items if i.get("role") == "assistant"]

            # Exactly one user message — the one we sent.
            # A count of 2 means two forwarders both posted the transcript
            # user-turn entry (the double-forwarder race re-appeared).
            assert len(user_items) == 1, (
                f"Expected 1 user item, got {len(user_items)}. "
                "A count of 2 indicates the double-forwarder race re-appeared: "
                "two transcript forwarder tasks were created and each independently "
                "posted the same user turn via external_conversation_item."
            )

            # Exactly one assistant response.
            # A count of 2 carries the same diagnosis as above.
            assert len(assistant_items) == 1, (
                f"Expected 1 assistant item, got {len(assistant_items)}. "
                "A count of 2 indicates the double-forwarder race re-appeared: "
                "two transcript forwarder tasks were created and each independently "
                "posted the same assistant turn via external_conversation_item."
            )
        finally:
            daemon.send_signal(signal.SIGTERM)
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()


# Number of rows the test's custom statusLine emits below the input box.
# Comfortably exceeds the old five-line prompt-scan window so the live
# ``❯`` row lands well outside it — the issue #701 condition.
_TALL_FOOTER_LINES = 8
# A multi-line statusLine command (no quoting needed — hyphenated tokens).
# omnigent chains the user's global statusLine into its own capture
# wrapper, so these rows render in the launched Claude's footer exactly
# as a real user's multi-line cost/usage bar (e.g. claude-hud) would.
_TALL_STATUSLINE_COMMAND = "; ".join(
    f"echo omnigent-hud-row-{i}" for i in range(1, _TALL_FOOTER_LINES + 1)
)


@contextmanager
def _user_statusline_configured(command: str) -> Iterator[None]:
    """
    Set ``~/.claude/settings.json`` ``statusLine.command`` for the test.

    omnigent overrides the launched Claude's statusLine with its own
    capture wrapper but chains to whatever the user configured globally
    (``claude_native_bridge.read_user_status_line_command``, wired into
    the per-session ``--settings``). A multi-line command therefore
    renders extra footer rows directly below the input box — faithfully
    modeling the enterprise cost/usage status bar from issue #701 with no
    paid seat. The original file bytes (or absence) are restored on exit.

    :param command: Shell command string to install as
        ``statusLine.command``.
    :returns: Iterator yielding once the statusLine entry is written.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    original = settings_path.read_bytes() if settings_path.exists() else None
    settings = json.loads(original) if original is not None else {}
    settings["statusLine"] = {"type": "command", "command": command}
    settings_path.write_text(json.dumps(settings))
    try:
        yield
    finally:
        if original is not None:
            settings_path.write_bytes(original)
        else:
            settings_path.unlink(missing_ok=True)


def _lines_below_live_prompt(session_id: str, *, timeout_s: float = 10.0) -> int:
    """
    Capture the session's Claude pane; count non-empty rows below ``❯``.

    Host-spawned sessions key the bridge dir by conversation id, so the
    runner's ``tmux.json`` is locatable from *session_id*. Captures the
    live pane with the same helpers the production gate uses and returns
    how many non-empty rows sit below the bottom-most prompt glyph — i.e.
    how far the footer has pushed the live ``❯`` off the bottom. Polls
    until the footer is tall (or *timeout_s* elapses) to ride out a
    status-bar redraw landing mid-capture.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param timeout_s: Seconds to keep re-capturing for a tall footer.
    :returns: Non-empty pane rows below the live prompt (best observed).
    :raises AssertionError: When no capture ever carried a prompt glyph
        (capture failed, or the input box never mounted).
    """
    bridge_dir = claude_native_bridge.bridge_dir_for_conversation_id(session_id)
    info = claude_native_bridge._wait_for_tmux_info(bridge_dir, timeout_s=30.0)
    glyph = claude_native_bridge._CLAUDE_PROMPT_GLYPH
    deadline = time.monotonic() + timeout_s
    best = -1
    last_pane = ""
    while time.monotonic() < deadline:
        pane = claude_native_bridge._capture_pane(info["socket_path"], info["tmux_target"])
        non_empty = [line for line in pane.splitlines() if line.strip()]
        glyph_rows = [i for i, line in enumerate(non_empty) if glyph in line]
        if glyph_rows:
            best = len(non_empty) - 1 - glyph_rows[-1]
            last_pane = pane
            if best >= 5:
                return best
        time.sleep(POLL_INTERVAL_S)
    assert best >= 0, f"no Claude prompt glyph in captured pane:\n{last_pane}"
    return best


def _post_user_message(client: httpx.Client, *, session_id: str, text: str) -> None:
    """
    POST a user text message onto a session's event stream.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param text: User message text to deliver into the Claude terminal.
    :returns: None.
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    )
    resp.raise_for_status()


@_needs_logged_in_claude
def test_claude_native_second_message_survives_tall_status_footer(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A 2nd message lands when a status bar grew taller than the scan window.

    Regression for issue #701. The readiness gate scanned only the last
    five non-empty pane lines for Claude's ``❯`` prompt glyph. An
    enterprise cost/usage status bar — or any multi-line custom
    ``statusLine`` — renders enough footer rows below the input box to
    push ``❯`` out of that window, so ``_wait_for_claude_prompt_ready``
    timed out and the message raised "did not become ready" before a
    keystroke was sent (a restart recovered only the next single message,
    then it broke again).

    Faithful to the reported trigger: the **first** message lands during
    boot, before the multi-line ``statusLine`` has rendered, so the
    prompt is still near the bottom and even the old gate passes. That
    turn drives Claude to paint its full (tall) footer. The **second**
    message is the regression — ``❯`` now sits well above the bottom, so
    the old fixed-window scan can't see it and injection times out, while
    the structural detector (``_claude_prompt_rendered`` keying on the
    box border rule directly under ``❯``) still finds it and the message
    is delivered. Verified red→green by toggling the structural detector
    (red: second marker never arrives; green: it does).

    The tall ``statusLine`` is chained into the omnigent status wrapper
    exactly as a real user's bar is (see
    :func:`_user_statusline_configured`). No boot-delay shim: the footer
    height — not boot timing — is what defeats the old gate.
    """
    workspace = tmp_path / "cn_tall_footer_ws"
    workspace.mkdir()
    marker_one = f"TALLBAR1_{uuid.uuid4().hex[:6].upper()}"
    marker_two = f"TALLBAR2_{uuid.uuid4().hex[:6].upper()}"

    with (
        _workspace_trusted_in_claude_config(workspace),
        _user_statusline_configured(_TALL_STATUSLINE_COMMAND),
    ):
        daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
        try:
            host_id = _online_host_id(http_client, timeout=30.0)
            agent_id = _claude_native_agent_id(http_client)

            create = http_client.post(
                "/v1/sessions",
                json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
                timeout=60.0,
            )
            create.raise_for_status()
            session_id = create.json()["id"]

            # Message 1: lands before the multi-line statusLine renders, so
            # even the old gate passes. Its turn makes Claude paint the
            # full, tall footer below the input box.
            _post_user_message(
                http_client,
                session_id=session_id,
                text=f"Reply with exactly one word: {marker_one}",
            )
            _poll_for_assistant_marker(
                http_client, session_id=session_id, marker=marker_one, timeout=180.0
            )

            # Self-validation against vacuity: prove the footer actually
            # pushed the live ``❯`` past the old five-line scan window. If a
            # future Claude rendered a short footer the regression wouldn't
            # reproduce, and without this guard the test would pass green
            # while testing nothing. Fail loudly instead.
            lines_below = _lines_below_live_prompt(session_id)
            assert lines_below >= 5, (
                f"tall-footer precondition not met: only {lines_below} non-empty "
                "row(s) below the live prompt, so the old tail-5 scan would still "
                "have found ❯ — this run would not exercise issue #701."
            )

            # Message 2: the tall footer is now rendered, so ``❯`` sits far
            # above the bottom. The old tail-window scan never finds it and
            # this injection times out; the structural detector delivers it.
            _post_user_message(
                http_client,
                session_id=session_id,
                text=f"Reply with exactly one word: {marker_two}",
            )
            text = _poll_for_assistant_marker(
                http_client, session_id=session_id, marker=marker_two, timeout=180.0
            )
            assert marker_two in text, (
                f"second message dropped: marker {marker_two!r} missing from {text!r} — "
                "the tall-footer readiness regression (issue #701) re-appeared."
            )
        finally:
            daemon.send_signal(signal.SIGTERM)
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()


def test_prompt_ready_detects_glyph_above_tall_footer_in_real_tmux(tmp_path: Path) -> None:
    """
    The readiness gate finds ``❯`` above a tall footer in a *real* tmux pane.

    The CI-runnable slice of issue #701: this needs no Claude login — only
    ``tmux`` (installed on the e2e runners) — so, unlike the full-stack
    tests above, it is deliberately NOT gated on a logged-in Claude and
    runs in CI. It paints a Claude-like idle frame whose footer is far
    taller than the old five-line prompt-scan window into a real tmux
    pane, then drives the production readiness gate against it through the
    same helpers injection uses (``_capture_pane`` →
    ``_claude_prompt_rendered`` → ``_wait_for_claude_prompt_ready``).

    Before the structural-detection fix the live ``❯`` sat outside the
    scanned tail and the gate raised "did not become ready"; after it the
    box is recognized by the border rule directly beneath ``❯``,
    independent of footer height. Verified red→green by toggling the fix.
    """
    if shutil.which("tmux") is None:
        pytest.skip("needs tmux")

    # A Claude-like idle frame: input-box top rule, the live prompt, the
    # box's closing rule, then a footer with far more than four rows below
    # ``❯`` — so the old tail-5 scan cannot reach the prompt row.
    pane_lines = [
        "────────────────────────────────────────",
        "❯ ",
        "────────────────────────────────────────",
        *[f"  status row {n}" for n in range(1, 9)],
    ]
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text("\n".join(pane_lines) + "\n", encoding="utf-8")

    # Unix-domain socket paths are length-limited (~104 chars on macOS) and
    # pytest's tmp_path is far longer, so put the tmux socket under /tmp with
    # a short unique name. tmux removes it on kill-server (no manual cleanup).
    socket = f"/tmp/cnrp-{uuid.uuid4().hex[:12]}.sock"
    session = "cn_ready_probe"
    # Paint the frame (cat) and hold the pane open (sleep) so the gate can
    # poll a stable capture. tmux runs the command through the shell.
    subprocess.run(
        [
            "tmux",
            "-S",
            socket,
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            "120",
            "-y",
            "40",
            f"cat {shlex.quote(str(pane_file))}; sleep 30",
        ],
        check=True,
    )
    try:
        # Returns once the frame renders (post-fix); raises RuntimeError
        # "did not become ready" while the prompt stays outside the scan
        # window (pre-fix). Reaching past this call means it was detected.
        claude_native_bridge._wait_for_claude_prompt_ready(socket, session, timeout_s=10.0)
    finally:
        subprocess.run(["tmux", "-S", socket, "kill-server"], check=False)
