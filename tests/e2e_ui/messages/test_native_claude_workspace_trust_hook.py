"""claude-native must not run an unreviewed workspace's project hooks at startup.

The runner pre-seeds Claude Code's workspace trust before launch, so Claude
boots an unreviewed workspace with no trust gate and loads its project
``.claude/settings.json`` under the CLI's default setting sources. A
``SessionStart`` command hook there then executes as the runner user with no
confirmation. Drives the real product path (session create -> runner bind ->
real Claude in the Terminal view) and asserts the project hook did not run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page

from tests._helpers.native_session import create_native_session
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _ensure_runner_online,
    _server_state,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.helpers.ui_configuration import _CLAUDE_MOCK_MODEL, temp_omnigent_mock_config

from .test_message_render_parity import _ensure_chat_view, _send
from .test_native_claude_render_parity import (
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_HOOK_LINE = "unreviewed project hook ran at startup"
_HOOK_WRITE_TIMEOUT_S = 45.0
_MOCK_TURN_TIMEOUT_MS = 90_000


def _make_untrusted_workspace() -> Path:
    """Create a fresh, never-reviewed project with a SessionStart hook.

    The runner shares the worktree but not ``/tmp``, so the workspace lives
    under the repo for the runner-launched Claude to open it.
    """
    root = _REPO_ROOT / ".omnigent" / "e2e-untrusted-ws" / uuid.uuid4().hex
    (root / ".claude").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "README.md").write_text("untrusted project\n", encoding="utf-8")

    marker = root / "hook-ran.txt"
    command = f"echo {shlex.quote(_HOOK_LINE)} > {shlex.quote(str(marker))}"
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    (root / ".claude" / "settings.json").write_text(
        json.dumps(settings, indent=2), encoding="utf-8"
    )
    return root


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_unreviewed_project_hook_does_not_run_at_startup(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """An unreviewed project's SessionStart hook must not execute on launch."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    workspace = _make_untrusted_workspace()
    marker = workspace / "hook-ran.txt"
    reply = f"ack-{uuid.uuid4().hex[:8]}"
    session_id: str | None = None

    try:
        with temp_omnigent_mock_config(
            mock_llm_server_url,
            "claude",
            workflow_owned=bool(_server_state.get("workflow_owned")),
        ):
            set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, reply)

            created = create_native_session(
                httpx, live_server, harness="claude", metadata={"workspace": str(workspace)}
            )
            session_id = str(created["session_id"])
            bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=15.0)

            # Record the user surface from here: create the page only after the
            # non-browser setup (session create + runner bind) is complete.
            page: Page = request.getfixturevalue("page")
            page.goto(f"{live_server}/c/{session_id}")
            _open_terminal_view(page)
            _wait_terminal_connected(page)
            _log.info("claude-native terminal attached for %s", session_id)

            deadline = time.monotonic() + _HOOK_WRITE_TIMEOUT_S
            while time.monotonic() < deadline and not marker.exists():
                page.wait_for_timeout(1000)

            if marker.exists():
                # Surface the executed payload in the pane for the recording.
                try:
                    _type_into_tui(page, "!cat hook-ran.txt")
                    page.wait_for_timeout(2000)
                except Exception:
                    _log.warning("could not echo hook output into the TUI", exc_info=True)
            else:
                # Fix-agnostic proof Claude fully booted and ran a turn (so the
                # SessionStart phase certainly fired) before clearing the hook.
                _ensure_chat_view(page)
                _send(page, "hello")
                page.get_by_text(reply).first.wait_for(timeout=_MOCK_TURN_TIMEOUT_MS)

            assert not marker.exists(), (
                "unreviewed project .claude/settings.json SessionStart hook executed "
                f"at startup without confirmation: {marker} -> "
                f"{marker.read_text(encoding='utf-8')!r}"
            )
    finally:
        if session_id is not None:
            with contextlib.suppress(Exception):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        reset_mock_llm(mock_llm_server_url)
        shutil.rmtree(workspace, ignore_errors=True)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)
