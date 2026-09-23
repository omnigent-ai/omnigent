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
``omnigent.harnesses.claude_native.bridge._wait_for_claude_prompt_ready``).

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
import shutil
import signal
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import (
    _claude_prompt_rendered,
    bridge_dir_for_bridge_id,
)
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

# Opt-in only. claude-native needs a real *interactive* Claude login
# (OAuth/Enterprise), which can't be relocated into CI: the `claude`
# binary IS present in CI (the claude-sdk e2e tests install it), but it
# is not logged in, so this test would launch a Claude TUI that can
# never reach its input box and hang until the shard times out. Binary
# presence is therefore NOT a sufficient gate — require an explicit
# opt-in env var that only a developer with a logged-in Claude sets.
pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1" or shutil.which("claude") is None,
    reason=(
        "claude-native e2e needs an interactive Claude login; set "
        "OMNIGENT_E2E_CLAUDE_NATIVE=1 (and have `claude` installed + logged in) to run"
    ),
)

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
            # Compat-aware: pinned OLD host venv in runner compat mode (Config 2).
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
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
        if agent["name"] == CLAUDE_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(
        f"{CLAUDE_NATIVE_AGENT_NAME!r} not registered on the server "
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
        within *timeout* (the dropped-first-message regression), or as soon
        as the session reports the turn ``failed`` — carrying its
        ``last_task_error`` so the terminal's last output is in the report.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 50, "order": "asc"})
        if resp.status_code == 200:
            for item in resp.json().get("data", []):
                text = _assistant_text(item)
                if marker in text:
                    return text
        session = client.get(f"/v1/sessions/{session_id}")
        if session.status_code == 200 and session.json().get("status") == "failed":
            raise AssertionError(
                f"The turn failed before any assistant message contained {marker!r}: "
                f"{session.json().get('last_task_error')!r}"
            )
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"No assistant message containing {marker!r} within {timeout}s — "
        "the first message was dropped (readiness gate regression)."
    )


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


def test_claude_native_first_message_survives_project_mcp_approval_gate(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A workspace with an unapproved ``.mcp.json`` still answers the first message.

    Claude Code shows a blocking "New MCP server found in this project" dialog
    the first time it runs in a directory whose ``.mcp.json`` it has not
    approved, and every per-session worktree is such a directory. The dialog
    fires no hook and replaces the input box, so without the pre-approval in
    ``build_hook_settings`` the readiness gate waits out its slow-boot cap,
    the turn fails, and the pane is reaped: the marker never arrives and the
    session reports ``failed``.

    The boot-delay shim pins that stall. On a warm machine Claude renders the
    dialog before the runner's first inject arrives, and the composer reclaim
    then Escapes it: the message lands but the project's servers are silently
    rejected, which this marker cannot see. With the shim the inject lands
    during boot, Claude comes up on the dialog afterwards, and the gate has
    nothing to reclaim — unfixed, the turn fails; fixed, no dialog appears.
    """
    workspace = tmp_path / "cn_mcp_ws"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"e2e-noop": {"command": "cat"}}})
    )
    marker = f"MCPGATE_{uuid.uuid4().hex[:6].upper()}"

    with _workspace_trusted_in_claude_config(workspace):
        daemon = _spawn_host_daemon_with_claude_shim(tmp_path=tmp_path, live_server=live_server)
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

            text = _poll_for_assistant_marker(
                http_client,
                session_id=session_id,
                marker=marker,
                # Past the readiness gate's 180 s slow-boot cap, so an unfixed
                # build fails on the authentic ``failed`` turn, not on this poll.
                timeout=240.0,
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


def _write_claude_dialog_shim(bin_dir: Path) -> None:
    """
    Write a ``claude`` shim that boots slowly and re-arms the MCP approval dialog.

    Reuses the boot-delay wrapper's sleep and input flush so the first inject
    lands before Claude renders anything, then turns off
    ``enableAllProjectMcpServers`` in the runner's per-session sidecar,
    preserving its transcript hooks. This forces the real MCP approval
    dialog even with pre-approval enabled; it does not simulate managed policies.

    :param bin_dir: Directory prepended to ``PATH``; the shim is written as
        ``bin_dir/claude``.
    :returns: None.
    :raises RuntimeError: If the real ``claude`` binary can't be found.
    """
    real_claude = shutil.which("claude")
    if real_claude is None:
        raise RuntimeError("real claude binary not found on PATH")
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, termios, time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if '--settings' not in args:\n"
        f"    os.execv({real_claude!r}, [{real_claude!r}, *args])\n"
        f"time.sleep({_BOOT_DELAY_S})\n"
        "termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)\n"
        "settings_path = Path(args[args.index('--settings') + 1])\n"
        "settings = json.loads(settings_path.read_text())\n"
        "settings['enableAllProjectMcpServers'] = False\n"
        "settings_path.write_text(json.dumps(settings))\n"
        f"os.execv({real_claude!r}, [{real_claude!r}, *args])\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _wait_for_failed_turn(
    client: httpx.Client,
    *,
    session_id: str,
    timeout: float,
) -> dict[str, object]:
    """
    Poll the session until it reports ``failed``; return ``last_task_error``.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param timeout: Max seconds to wait, e.g. ``90.0`` — well under the
        readiness gate's 180 s slow-boot cap, so a build that only fails by
        timing out cannot pass this wait.
    :returns: The session's ``last_task_error`` payload.
    :raises AssertionError: If the session does not report ``failed`` in time.
    """
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        body = client.get(f"/v1/sessions/{session_id}").json()
        status = body.get("status")
        if status == "failed":
            return dict(body.get("last_task_error") or {})
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Session {session_id} did not report a failed turn within {timeout}s "
        f"(last status={status!r}); the readiness gate is still stalled on the dialog."
    )


def _wait_for_marker_or_failure(
    client: httpx.Client,
    *,
    session_id: str,
    marker: str,
    pane_of: Callable[[], str],
    timeout: float,
) -> str:
    """
    Poll until an assistant message carries *marker*, failing fast on a failed turn.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param marker: The literal string Claude is asked to echo.
    :param pane_of: Captures the live Claude pane, attached to the report so
        a stall is diagnosable from the failure alone.
    :param timeout: Max seconds to wait.
    :returns: The assistant text containing *marker*.
    :raises AssertionError: On a failed turn (with ``last_task_error``) or
        when the marker never arrives (with the session status and the pane).
    """
    deadline = time.monotonic() + timeout
    status = None
    retry_started = False
    while time.monotonic() < deadline:
        items = client.get(
            f"/v1/sessions/{session_id}/items", params={"limit": 50, "order": "asc"}
        )
        if items.status_code == 200:
            for item in items.json().get("data", []):
                text = _assistant_text(item)
                if marker in text:
                    return text
        body = client.get(f"/v1/sessions/{session_id}").json()
        status = body.get("status")
        retry_started = retry_started or status != "failed"
        if status == "failed" and retry_started:
            raise AssertionError(
                f"The resent turn failed before answering {marker!r}: "
                f"{body.get('last_task_error')!r}\nPane:\n{pane_of()}"
            )
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"No assistant message containing {marker!r} within {timeout}s "
        f"(session status={status!r}).\nPane:\n{pane_of()}"
    )


def _send_marker_message(client: httpx.Client, *, session_id: str, marker: str) -> None:
    """
    Post the one-word marker prompt as a user message.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param marker: The literal string Claude is asked to echo.
    :returns: None.
    """
    event = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"Reply with exactly one word: {marker}"}
                ],
            },
        },
        timeout=30.0,
    )
    event.raise_for_status()


def test_claude_native_first_message_reports_a_terminal_dialog_without_reaping(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A dialog holding Claude's terminal fails the turn fast and leaves it answerable.

    The shim brings Claude up on the "New MCP server found in this project"
    dialog after the first inject has already started waiting. Unfixed, the
    readiness gate sits on the dialog for its 180 s cap, the executor reaps
    the pane, and the web gets a generic timeout with nothing left to answer.
    Fixed, the turn fails within seconds with the dialog named in the error,
    the pane is still parked on the dialog, and answering it in the terminal
    then resending delivers the message and gets the marker back.
    """
    workspace = tmp_path / "cn_dialog_ws"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"e2e-noop": {"command": "cat"}}})
    )
    marker = f"DIALOG_{uuid.uuid4().hex[:6].upper()}"
    bin_dir = tmp_path / "shim_bin"
    bin_dir.mkdir()
    _write_claude_dialog_shim(bin_dir)

    with _workspace_trusted_in_claude_config(workspace):
        daemon = _spawn_host_daemon(
            tmp_path=tmp_path, live_server=live_server, extra_path_dir=bin_dir
        )
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

            _send_marker_message(http_client, session_id=session_id, marker=marker)

            error = _wait_for_failed_turn(http_client, session_id=session_id, timeout=90.0)
            report = json.dumps(error)
            assert "waiting for an answer" in report, report
            assert "New MCP server found in this project" in report, report

            # The pane survived the failure and is still parked on the dialog.
            tmux = json.loads((bridge_dir_for_bridge_id(session_id) / "tmux.json").read_text())
            socket_path, target = tmux["socket_path"], tmux["tmux_target"]

            def capture_pane() -> str:
                return subprocess.run(
                    ["tmux", "-S", socket_path, "capture-pane", "-p", "-t", target],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout

            pane = capture_pane()
            assert "New MCP server found in this project" in pane, pane

            # Answer it the way a person would (Esc: continue without the
            # server) and resend: the same message now lands and is answered.
            subprocess.run(
                ["tmux", "-S", socket_path, "send-keys", "-t", target, "Escape"], check=True
            )
            deadline = time.monotonic() + 30.0
            while not _claude_prompt_rendered(pane := capture_pane()):
                assert time.monotonic() < deadline, f"Claude did not leave the dialog:\n{pane}"
                time.sleep(POLL_INTERVAL_S)
            _send_marker_message(http_client, session_id=session_id, marker=marker)
            text = _wait_for_marker_or_failure(
                http_client,
                session_id=session_id,
                marker=marker,
                pane_of=capture_pane,
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
