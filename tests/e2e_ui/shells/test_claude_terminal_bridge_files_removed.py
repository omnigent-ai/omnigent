"""UI journey: a claude-native terminal must survive its temp-dir bridge files vanishing.

macOS periodic temp cleanup removes files under ``$TMPDIR`` that nobody touched
for ~3 days. For an idle claude-native session those are ``tmux.json`` and
``bridge.json`` in the bridge dir plus the ``omnigent-terminal-*`` tmux socket
dir, while the runner process and the tmux server holding the Claude pane keep
running. This test stands in for the cleaner by deleting those three artifacts
and then checks what the user sees in the still-open Terminal tab.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import psutil
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.bridge import bridge_dir_for_bridge_id
from omnigent.process_logging import logs_root
from tests.e2e_ui.conftest import _server_state
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _TERMINAL_VIEW,
    _open_terminal_view,
    _wait_terminal_connected,
)

_PLACEHOLDER = "The harness is not running."
# The watcher declares tmux gone after 3 consecutive 1 s probes; leave slack.
_OBSERVE_S = 60.0


def _tmux_pids(socket_path: Path, target: str) -> tuple[int, int]:
    probe = subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "display-message",
            "-p",
            "-t",
            target,
            "#{pid} #{pane_pid}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr
    server_pid, pane_pid = probe.stdout.split()
    return int(server_pid), int(pane_pid)


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _terminal_listed(base_url: str, session_id: str) -> bool:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10)
    response.raise_for_status()
    data = response.json()
    return any(item.get("type") == "terminal" for item in data.get("data", []))


def _visible_main_terminal(page: Page):
    return page.locator('[data-testid="main-terminal-view"][data-visible="true"]')


def _watch_terminal(page: Page, base_url: str, session_id: str, duration_s: float) -> dict:
    """Poll the Terminal tab; stop early once the stopped placeholder is on screen."""
    placeholder = _visible_main_terminal(page).get_by_text(_PLACEHOLDER, exact=True)
    terminal = page.locator(_TERMINAL_VIEW).last
    started = time.monotonic()
    seen_after_s: float | None = None
    while time.monotonic() - started < duration_s:
        if placeholder.count() and placeholder.first.is_visible():
            seen_after_s = round(time.monotonic() - started, 1)
            break
        page.wait_for_timeout(500)
    return {
        "placeholder_seen_after_s": seen_after_s,
        "terminal_view_state": terminal.get_attribute("data-state") if terminal.count() else None,
        "terminal_resource_listed": _terminal_listed(base_url, session_id),
    }


def _save_runner_logs(evidence_dir: Path) -> None:
    """Copy the runner logs into the evidence dir for post-hoc debugging."""
    for log in sorted(logs_root().glob("runner/*.log")):
        shutil.copy(log, evidence_dir / log.name)


@pytest.mark.timeout(420)
def test_claude_terminal_survives_bridge_files_removed_from_tmp(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    base_url, session_id = native_claude_mock_session
    evidence_dir = Path(os.environ.get("OMNIGENT_REPRO_EVIDENCE_DIR") or tmp_path)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    page: Page = request.getfixturevalue("page")

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    page.wait_for_timeout(3_000)
    page.screenshot(path=str(evidence_dir / "1-terminal-connected.png"))

    tmp_root = Path(tempfile.gettempdir()).resolve()
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    tmux_file, config_file = bridge_dir / "tmux.json", bridge_dir / "bridge.json"
    tmux_info = json.loads(tmux_file.read_text())
    socket_path = Path(tmux_info["socket_path"])
    terminal_dir = socket_path.parent
    assert terminal_dir.name.startswith("omnigent-terminal-"), terminal_dir
    for path in (tmux_file, config_file, terminal_dir):
        assert path.exists() and path.resolve().is_relative_to(tmp_root), path
    tmux_pid, pane_pid = _tmux_pids(socket_path, tmux_info["tmux_target"])
    runner_pid = int(_server_state["runner_pid"])
    pane_capture = subprocess.run(
        ["tmux", "-S", str(socket_path), "capture-pane", "-p", "-t", tmux_info["tmux_target"]],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert pane_capture.returncode == 0, pane_capture.stderr
    (evidence_dir / "pane-before-deletion.txt").write_text(pane_capture.stdout)

    # Stand-in for the macOS temp cleaner: remove the same three artifacts, touch no process.
    tmux_file.unlink()
    config_file.unlink()
    shutil.rmtree(terminal_dir)
    deleted_at = time.time()
    alive_after_delete = {
        "runner": _alive(runner_pid),
        "tmux_server": _alive(tmux_pid),
        "pane": _alive(pane_pid),
    }
    assert all(alive_after_delete.values()), alive_after_delete

    live = _watch_terminal(page, base_url, session_id, _OBSERVE_S)
    page.wait_for_timeout(3_000)
    page.screenshot(path=str(evidence_dir / "2-after-deletion.png"))
    main_terminal = _visible_main_terminal(page)
    alive_at_end = {
        "runner": _alive(runner_pid),
        "tmux_server": _alive(tmux_pid),
        "pane": _alive(pane_pid),
    }
    _save_runner_logs(evidence_dir)
    (evidence_dir / "evidence.json").write_text(
        json.dumps(
            {
                "runner_pid": runner_pid,
                "tmux_server_pid": tmux_pid,
                "pane_pid": pane_pid,
                "tempdir": str(tmp_root),
                "terminal_dir": str(terminal_dir),
                "deleted_at_unix": deleted_at,
                "alive_after_delete": alive_after_delete,
                "open_page": live,
                "alive_at_end": alive_at_end,
                "main_terminal_text": main_terminal.all_inner_texts(),
            },
            indent=1,
        )
    )

    assert all(alive_at_end.values()), f"stand-in killed a process: {alive_at_end}"
    assert live["placeholder_seen_after_s"] is None, (
        f"Bug reproduced: {live['placeholder_seen_after_s']}s after the bridge files were "
        f"removed the Terminal tab showed {_PLACEHOLDER!r} although the runner, tmux server "
        f"and Claude pane are alive ({alive_at_end}); terminal resource listed: "
        f"{live['terminal_resource_listed']}"
    )
    expect(main_terminal.get_by_text(_PLACEHOLDER, exact=True)).to_have_count(0)
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute("data-state", "connected")
    assert live["terminal_resource_listed"], "terminal resource was removed"
    assert socket_path.exists(), "the reaped private socket was not recreated"
