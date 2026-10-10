"""The embedded REPL terminal of a runner-hosted SDK session outlives its tmux server.

A runner-hosted SDK-harness session auto-creates the Omnigent REPL terminal
``tui:main`` that the web SPA shows behind the Chat/Terminal toggle. Its tmux
server can die underneath a live session (the REPL crashes or quits, the server
is killed). The runner must rebuild that terminal instead of dropping it: the
resource stays in the session inventory, the Terminal view reconnects, and the
session never claims the harness stopped while its SDK harness is still alive.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH"),
    pytest.mark.timeout(600),
]

_REPL_TERMINAL = "tui:main"
_FALLBACK = "The harness is not running."
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_REPLY = "Hello from the mock model."
# The idle watcher needs three 1s probes to declare tmux gone; the rebuild and
# the browser's re-attach follow. Give the runner ample room past that.
_RUNNER_VERDICT_TIMEOUT_S = 60.0


def _repl_terminal(base_url: str, session_id: str) -> dict[str, Any] | None:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/terminals", timeout=10.0)
    response.raise_for_status()
    return next((item for item in response.json()["data"] if item["name"] == _REPL_TERMINAL), None)


def _session_status(base_url: str, session_id: str) -> str:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return str(response.json()["status"])


def _tmux(socket_path: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", socket_path, *args], capture_output=True, text=True, timeout=10
    )


def _wait_for_pane_output(socket_path: str, timeout_s: float = 30.0) -> str:
    deadline = time.monotonic() + timeout_s
    while True:
        captured = _tmux(socket_path, "capture-pane", "-p")
        if captured.returncode == 0 and captured.stdout.strip():
            return captured.stdout
        assert time.monotonic() < deadline, "REPL never rendered into its tmux pane"
        time.sleep(0.5)


def _terminal_states(terminal: Any) -> list[str | None]:
    return terminal.evaluate_all("(elements) => elements.map((el) => el.dataset.state)")


def test_repl_terminal_survives_tmux_server_death(
    page: Page, seeded_session: tuple[str, str], mock_llm_server_url: str
) -> None:
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url, [{"text": _REPLY}], key="repl-tmux-death", match="Say hello"
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    composer.fill("Say hello")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).filter(has_text=_REPLY)).to_have_count(1, timeout=60_000)

    terminal_button = page.get_by_test_id("view-mode-terminal")
    expect(terminal_button).to_be_enabled(timeout=60_000)
    terminal_button.click()
    main_terminal = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
    terminal = main_terminal.get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)

    before = _repl_terminal(base_url, session_id)
    assert before is not None, "runner did not register the REPL terminal"
    socket_path = str(before["metadata"]["tmux_socket"])
    _wait_for_pane_output(socket_path)
    assert _session_status(base_url, session_id) == "idle"

    killed = _tmux(socket_path, "kill-server")
    assert killed.returncode == 0, killed.stderr
    assert _tmux(socket_path, "has-session").returncode != 0

    # Wait for the runner's verdict: either the misleading resume fallback
    # appears, or the Terminal view is connected to a live tmux pane again.
    # The inventory may lack tui:main for a moment while it is rebuilt, so a
    # transient miss is not a verdict.
    fallback = main_terminal.get_by_text(_FALLBACK, exact=True)
    deadline = time.monotonic() + _RUNNER_VERDICT_TIMEOUT_S
    while time.monotonic() < deadline:
        if fallback.count() > 0:
            break
        current = _repl_terminal(base_url, session_id)
        if (
            current is not None
            and "connected" in _terminal_states(terminal)
            and _tmux(str(current["metadata"]["tmux_socket"]), "has-session").returncode == 0
        ):
            break
        time.sleep(0.5)

    after = _repl_terminal(base_url, session_id)
    assert after is not None, "runner dropped tui:main from the session's terminal inventory"
    expect(fallback).to_have_count(0)
    expect(terminal).to_have_attribute("data-state", "connected", timeout=60_000)
    assert _tmux(str(after["metadata"]["tmux_socket"]), "has-session").returncode == 0, (
        "tui:main is listed but its tmux server is not running"
    )
    assert _session_status(base_url, session_id) == "idle"
