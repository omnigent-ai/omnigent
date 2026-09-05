"""End-to-end regression test: ``omnigent cursor`` must merge ``.cursor/hooks.json``.

Guards the regression where launching the cursor-native harness in a workspace
that already carries a project-scoped ``.cursor/hooks.json`` (e.g. Universe's
``preToolUse`` policy hook) fully replaced that file with Omnigent's
session-specific ``stop`` usage hook, destroying the user's existing hooks.
The destructive write happens during the runner-side terminal launch
(``write_hooks_config`` in :mod:`omnigent.cursor_native_bridge`, called from
``omnigent/runner/native/orchestration.py``) — *before* the cursor executable
is resolved and before any model turn.

Because the failure fires at launch, this test — unlike the sibling
``test_cursor_native_cli_e2e`` — needs NO authenticated ``cursor-agent`` and
no real model turn: an unauthenticated TUI sitting at its login prompt is
enough, since the hooks.json rewrite strictly precedes the tmux launch. It
therefore gates only on the ``cursor-agent`` and ``tmux`` binaries, not on
``OMNIGENT_E2E_CURSOR_NATIVE``.

The journey it drives is exactly the user's:

1. a workspace already has ``.cursor/hooks.json`` with a ``preToolUse`` hook;
2. the user launches ``omnigent cursor`` from that workspace;
3. after the cursor terminal comes up, ``.cursor/hooks.json`` must still
   contain the pre-existing ``preToolUse`` hook alongside Omnigent's ``stop``
   usage hook. A launch path that clobbers the file wholesale instead of
   merging fails the final assertion.

Run::

    .venv/bin/python -m pytest tests/e2e/test_cursor_native_hooks_merge_e2e.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e._native_resume_helpers import (
    PtyHandle,
    cli_env,
    omnigent_console_script,
    spawn_cli_background,
    wait_for_terminal_ready,
)
from tests.e2e.helpers import POLL_INTERVAL_S

# ``resume_test_server`` is provided by tests/e2e/conftest.py (the allow-list-
# free server the CLI wrapper's self-spawned host daemon registers against).

pytestmark = pytest.mark.skipif(
    shutil.which("cursor-agent") is None or shutil.which("tmux") is None,
    reason=(
        "cursor-native launch needs the `cursor-agent` and `tmux` binaries on "
        "PATH (no login required — the hooks.json rewrite happens at launch, "
        "before any turn)"
    ),
)

# Launch covers CLI parse -> daemon runner spawn -> cursor terminal launch;
# mirror the headroom the other native CLI e2e tests allow on a contended host.
_SESSION_ID_TIMEOUT = 120.0
_TERMINAL_READY_TIMEOUT = 120.0

# The CLI prints ``Omnigent: <server>/c/<session-id>`` shortly after creating
# the session. Unlike ``wait_for_conversation_id``'s ``conv_<hex>`` pattern,
# this matches both id shapes servers mint (``conv_<hex>`` and bare hex/uuid).
_SESSION_URL_ID_RE = re.compile(r"/c/([A-Za-z0-9_-]{8,})")

# The pre-existing project hook a workspace like Universe ships. Shape matches
# Cursor's hooks.json schema: {"version": 1, "hooks": {"<event>": [{"command": …}]}}.
_PRE_EXISTING_PRE_TOOL_USE = [{"command": "./scripts/universe-pre-tool-guard.sh"}]


def _wait_for_session_id(handle: PtyHandle, *, timeout: float) -> str:
    """Poll the backgrounded CLI's output until it prints its session URL.

    :param handle: The backgrounded session from ``spawn_cli_background``.
    :param timeout: Max seconds to wait for the ``…/c/<id>`` line.
    :returns: The session id, e.g. ``"conv_25cf39e3…"`` or ``"57563f27…"``.
    :raises AssertionError: If no id appears within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        match = _SESSION_URL_ID_RE.search(handle.output())
        if match:
            return match.group(1)
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"no session URL printed within {timeout}s; output tail:\n{handle.output()[-2000:]}"
    )


def test_cursor_native_launch_preserves_existing_hooks_json(
    resume_test_server: str,
    tmp_path: Path,
) -> None:
    """Launching ``omnigent cursor`` keeps the workspace's pre-existing hooks.

    Seeds ``.cursor/hooks.json`` with a ``preToolUse`` hook, launches the
    real ``omnigent cursor --server …`` CLI from that workspace, waits for the
    cursor terminal to register (which strictly follows the launch path's
    hooks.json write), and asserts the rewritten ``hooks.json`` still contains
    the original ``preToolUse`` hook alongside Omnigent's usage ``stop`` hook.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; hosts the seeded workspace.
    """
    workspace = tmp_path / "workspace"
    cursor_dir = workspace / ".cursor"
    cursor_dir.mkdir(parents=True)
    hooks_path = cursor_dir / "hooks.json"
    pre_existing = {"version": 1, "hooks": {"preToolUse": _PRE_EXISTING_PRE_TOOL_USE}}
    hooks_path.write_text(json.dumps(pre_existing, indent=2) + "\n", encoding="utf-8")

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "cursor", "--server", resume_test_server],
        env=cli_env(),
        cwd=str(workspace),
    )
    try:
        session_id = _wait_for_session_id(handle, timeout=_SESSION_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            # The terminal resource registers strictly AFTER the launch path's
            # write_hooks_config(), so once it exists the rewrite has happened.
            wait_for_terminal_ready(
                client,
                conversation_id=session_id,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )

        raw = hooks_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        hooks = data.get("hooks")
        assert isinstance(hooks, dict), f"hooks.json lost its hooks mapping:\n{raw}"

        # Sanity: the launch did register Omnigent's per-turn usage stop hook
        # (this is the write that clobbers the file today).
        stop_commands = [
            entry.get("command", "") for entry in hooks.get("stop", []) if isinstance(entry, dict)
        ]
        assert any("cursor_native_usage" in command for command in stop_commands), (
            "launch never registered Omnigent's cursor_native_usage stop hook in "
            f".cursor/hooks.json — cannot observe the rewrite; file was:\n{raw}"
        )

        # THE regression assertion: the workspace's pre-existing preToolUse
        # hook must survive the launch. A write_hooks_config() that replaces
        # the whole file fails this with preToolUse == None.
        assert hooks.get("preToolUse") == _PRE_EXISTING_PRE_TOOL_USE, (
            "cursor-native launch destroyed the workspace's pre-existing "
            "preToolUse hook instead of merging .cursor/hooks.json; "
            f"file after launch was:\n{raw}"
        )
    finally:
        handle.terminate()
