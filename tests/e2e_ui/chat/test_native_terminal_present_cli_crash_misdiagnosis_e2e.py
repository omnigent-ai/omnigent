"""E2E: a present Claude Code CLI that runs and then exits 127 must get the generic
terminal-exit card, not "Agent command not found" with an install hint.

The crashing CLI is a scripted ``claude`` stub selected through the workspace's
``harness.claude-native.command`` override, so the journey runs against the shared
``live_server`` runner, including a workflow-prepared one.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.native_session import create_native_session
from tests.e2e_ui.conftest import _bind_session_runner, _ensure_runner_online, _server_state
from tests.helpers.ui_configuration import temp_omnigent_mock_config

_REPO_ROOT = Path(__file__).resolve().parents[3]

# bind → stub runs and dies 127 → required_terminal_exited → card in the SPA.
_CARD_TIMEOUT_MS = 120_000
_FAILURE_TIMEOUT_S = 120.0

_ERROR_PILL = '[data-testid="error-pill"]'

_CRASH_EXIT_CODE = 127
_RESUME_MARKER = "Resume this session with:"
_STRAY_ARG_LINE = "zsh:2: command not found: --model"
# Written by the stub after its last output line, right before it exits 127.
_RAN_MARKER = "ran-to-completion"

_MISDIAGNOSIS_HEADLINE = "Agent command not found"
_MISDIAGNOSIS_REMEDIATION = "Install the harness"
_HONEST_HEADLINE = "The agent's terminal exited unexpectedly"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="the claude-native required terminal needs tmux",
)


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    if not os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        return browser_context_args
    return {**browser_context_args, "record_video_size": {"width": 1280, "height": 720}}


def _write_crashing_claude_stub(bin_dir: Path) -> Path:
    """Write a ``claude`` executable that runs, prints the reported pane, then exits 127."""
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "Claude Code (native session)"\n'
        "sleep 0.5\n"
        'echo "● Unknown command: /restart"\n'
        'echo "  Press Ctrl-C again to exit    ✔ Update installed · Restart to update"\n'
        "sleep 0.5\n"
        f'echo "{_RESUME_MARKER}"\n'
        'echo "claude --resume be28caff-2e85-47de-8f17-9346d116106b"\n'
        # Changing output keeps the pane observed as running before it dies.
        "for i in $(seq 1 15); do printf '\\rworking %s ' \"$i\"; sleep 0.2; done\n"
        "echo\n"
        f'echo "^C{_STRAY_ARG_LINE}"\n'
        f'touch "$(dirname "$0")/{_RAN_MARKER}"\n'
        f"exit {_CRASH_EXIT_CODE}\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _write_workspace_with_claude_override(workspace: Path, stub: Path) -> None:
    """Point the session workspace's claude-native launch at *stub*."""
    (workspace / ".omnigent").mkdir(parents=True, exist_ok=True)
    (workspace / ".omnigent" / "config.yaml").write_text(
        f"harness:\n  claude-native:\n    command: {stub}\n", encoding="utf-8"
    )


@pytest.mark.timeout(30)
def test_crashing_claude_stub_exits_127_after_running(tmp_path: Path) -> None:
    stub = _write_crashing_claude_stub(tmp_path)
    result = subprocess.run([str(stub)], capture_output=True, text=True, check=False)
    assert result.returncode == _CRASH_EXIT_CODE
    assert _RESUME_MARKER in result.stdout, result.stdout
    assert _STRAY_ARG_LINE in result.stdout, result.stdout
    assert (tmp_path / _RAN_MARKER).exists()


@pytest.fixture
def crashing_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A claude-native session whose present CLI runs, then exits 127.

    The stub and the workspace live inside the worktree: a workflow-prepared
    runner runs in another sandbox and cannot see this process's temp dir.

    :returns: ``(base_url, session_id, scratch_dir)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    scratch = _REPO_ROOT / ".omnigent" / "e2e-scratch" / f"claude-crash-{uuid.uuid4().hex[:8]}"
    workspace = scratch / "workspace"
    workspace.mkdir(parents=True)
    stub = _write_crashing_claude_stub(scratch)
    _write_workspace_with_claude_override(workspace, stub)
    session_id: str | None = None
    try:
        with temp_omnigent_mock_config(
            mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
        ):
            created = create_native_session(
                httpx, live_server, harness="claude", metadata={"workspace": str(workspace)}
            )
            session_id = str(created["session_id"])
            _bind_session_runner(live_server, session_id, runner_id)
            yield (live_server, session_id, scratch)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(scratch, ignore_errors=True)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def _await_required_terminal_exit(
    base_url: str, session_id: str, *, timeout_s: float = _FAILURE_TIMEOUT_S
) -> dict[str, Any]:
    """Poll the session until the runner's required-terminal exit failure is persisted.

    The exit is recorded on the snapshot's ``last_task_error`` (the SPA renders the
    card from it); an ``error`` transcript item is accepted too.
    """
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        info = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if info.status_code == 200:
            last = info.json().get("last_task_error")
            if isinstance(last, dict) and last.get("code"):
                return last
        items = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 50, "order": "desc"},
            timeout=10.0,
        )
        if items.status_code == 200:
            for item in items.json().get("data", []):
                error = item.get("error") if isinstance(item.get("error"), dict) else item
                if item.get("type") == "error" and error.get("code"):
                    return error
        time.sleep(1.0)
    raise AssertionError(f"no task failure persisted in time; last_task_error={last!r}")


def _reveal_failure_card(page: Page):
    """Switch to the Chat view, then expand the error pill and its diagnostics."""
    toggle = page.get_by_test_id("view-mode-toggle")
    pill = page.locator(_ERROR_PILL).first
    expect(toggle.or_(pill).first).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    if toggle.count() > 0:
        with contextlib.suppress(Exception):
            page.get_by_test_id("view-mode-chat").click(timeout=30_000)
    expect(pill).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    if pill.get_by_test_id("error-message-content").count() == 0:
        pill.get_by_test_id("error-headline").click()
    expect(pill.get_by_test_id("error-message-content")).to_be_visible(timeout=15_000)
    diag_btn = pill.get_by_role("button", name="View diagnostics")
    if diag_btn.count() > 0:
        with contextlib.suppress(Exception):
            diag_btn.first.click()
            output_tab = pill.get_by_role("tab", name="Last captured output")
            if output_tab.count() > 0:
                output_tab.first.click()
            pill.get_by_test_id("error-diagnostics-content").first.scroll_into_view_if_needed()
    return pill


@pytest.mark.timeout(300)
def test_present_cli_crash_is_not_misdiagnosed_as_missing(
    request: pytest.FixtureRequest,
    crashing_claude_session: tuple[str, str, Path],
) -> None:
    """A present CLI that ran and crashed 127 must not get the "install the harness" card."""
    base_url, session_id, scratch = crashing_claude_session
    # Create the recorded page only now, so a clip starts at the first navigation.
    page: Page = request.getfixturevalue("page")

    page.goto(f"{base_url}/c/{session_id}")
    pill = _reveal_failure_card(page)
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        with contextlib.suppress(Exception):
            page.screenshot(path=str(Path(record_dir) / "failure-card.png"))

    # Precondition the misdiagnosis ignores: the present CLI ran through its
    # resume banner and the stray `--model` line, then the registered terminal
    # exited (a launch-time death would be a different failure code).
    error = _await_required_terminal_exit(base_url, session_id)
    assert (scratch / _RAN_MARKER).exists(), "the stub CLI never ran to its exit"
    assert error.get("code") == "required_terminal_exited", error

    # Read the card only after that failure is persisted, so this is its final text.
    page.wait_for_timeout(3_000)
    card_text = pill.inner_text()
    assert _RESUME_MARKER in card_text and _STRAY_ARG_LINE in card_text, (
        f"The card's captured output lacks the pane lines proving the CLI ran.\n"
        f"Card text:\n{card_text}"
    )

    assert _MISDIAGNOSIS_HEADLINE not in card_text, (
        f"Misdiagnosis: the card reads {_MISDIAGNOSIS_HEADLINE!r} for a present Claude Code CLI "
        f"that ran and then exited {_CRASH_EXIT_CODE}.\nCard text:\n{card_text}"
    )
    assert _MISDIAGNOSIS_REMEDIATION.lower() not in card_text.lower(), (
        f"Misdiagnosis: the card tells the user to install a harness that was present.\n"
        f"Card text:\n{card_text}"
    )
    expect(pill).to_contain_text(_HONEST_HEADLINE, timeout=15_000)
