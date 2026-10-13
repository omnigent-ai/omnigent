"""The embedded REPL terminal of a runner-hosted SDK session outlives its tmux server.

A runner-hosted SDK-harness session auto-creates the Omnigent REPL terminal
``tui:main`` that the web SPA shows behind the Chat/Terminal toggle. Its tmux
server can die underneath a live session (the REPL crashes or quits, the server
is killed). The runner must rebuild that terminal instead of dropping it: the
resource stays in the session inventory, the Terminal view reconnects, and the
session never claims the harness stopped while its SDK harness is still alive.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.process_logging import process_log_dir
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm

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
# What the watcher logs when the REPL's exit took its whole tmux server with it.
_TMUX_UNAVAILABLE_SIGNATURE = re.compile(
    r"tmux unavailable after \d+ consecutive probes for terminal tui:main"
)
# The REPL status line reports ``ready`` as a standalone word; match on a word
# boundary so prose such as "already" never satisfies the prompt check.
_READY_PROMPT = re.compile(r"\bready\b")


def _repl_terminal(base_url: str, session_id: str) -> dict[str, Any] | None:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/terminals", timeout=10.0)
    response.raise_for_status()
    return next((item for item in response.json()["data"] if item["name"] == _REPL_TERMINAL), None)


def _session_status(base_url: str, session_id: str) -> str:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return str(response.json()["status"])


def _tmux(socket_path: str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["tmux", "-S", socket_path, *args], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired as exc:
        # A hung probe must surface as a failed command, not an opaque crash.
        return subprocess.CompletedProcess(
            exc.cmd, returncode=124, stdout=exc.stdout or "", stderr=exc.stderr or ""
        )


def _wait_for_pane(
    socket_path: str, accept: Callable[[str], bool], failure: str, timeout_s: float
) -> str:
    """Poll the pane until ``accept`` passes its text; a failed capture reads as empty."""
    deadline = time.monotonic() + timeout_s
    while True:
        captured = _tmux(socket_path, "capture-pane", "-p")
        text = captured.stdout if captured.returncode == 0 else ""
        if accept(text):
            return text
        assert time.monotonic() < deadline, f"{failure}; last frame:\n{text}"
        time.sleep(0.5)


def _wait_for_pane_output(socket_path: str, timeout_s: float = 30.0) -> str:
    return _wait_for_pane(
        socket_path,
        lambda text: bool(text.strip()),
        "REPL never rendered into its tmux pane",
        timeout_s,
    )


def _wait_for_pane_contains(socket_path: str, needle: str, timeout_s: float = 60.0) -> str:
    return _wait_for_pane(
        socket_path, lambda text: needle in text, f"pane never showed {needle!r}", timeout_s
    )


def _wait_for_pane_matches(
    socket_path: str, pattern: re.Pattern[str], timeout_s: float = 60.0
) -> str:
    return _wait_for_pane(
        socket_path,
        lambda text: pattern.search(text) is not None,
        f"pane never matched {pattern.pattern!r}",
        timeout_s,
    )


def _reach_repl_prompt(page: Page, helper: Any, socket_path: str) -> None:
    """Advance past the first-launch theme picker to the live REPL prompt.

    A fresh config has no persisted theme, so the REPL opens on an arrow-key
    theme picker that eats ordinary text. Accept its detected default so the
    ``❯`` prompt (status line ``ready``) takes over before we type.
    """
    helper.focus()
    deadline = time.monotonic() + 60.0
    while True:
        pane = _tmux(socket_path, "capture-pane", "-p").stdout
        if _READY_PROMPT.search(pane):
            return
        if "Choose your theme" in pane:
            helper.focus()
            page.keyboard.press("Enter")
            _wait_for_pane_matches(socket_path, _READY_PROMPT)
            return
        assert time.monotonic() < deadline, f"REPL never reached a prompt:\n{pane}"
        time.sleep(0.5)


def _wait_for_repl_exit(socket_path: str, timeout_s: float = 30.0) -> None:
    """Wait until the REPL process is gone: its pane is dead or its server is."""
    deadline = time.monotonic() + timeout_s
    while True:
        probe = _tmux(socket_path, "list-panes", "-F", "#{pane_dead}")
        if probe.returncode != 0 or "1" in probe.stdout.split():
            return
        assert time.monotonic() < deadline, (
            "the REPL did not exit after /quit; pane shows:\n"
            + _tmux(socket_path, "capture-pane", "-p").stdout
        )
        time.sleep(0.5)


def _runner_log_offsets() -> dict[Path, int]:
    """Snapshot the runner process logs so a later read covers only new lines."""
    return {path: path.stat().st_size for path in process_log_dir("runner").glob("runner*.log")}


def _runner_logs_since(offsets: dict[Path, int], session_id: str) -> str:
    """Read what the spawned runner logged for ``session_id`` after ``offsets``.

    Workers share the runner log directory under one HOME. Only chunks whose
    new content mentions this session are returned, kept whole because the
    tmux-unavailable line is not itself session-tagged. A log file that first
    appears after the snapshot is read from its start.
    """
    chunks = []
    for path in sorted(process_log_dir("runner").glob("runner*.log")):
        with path.open(encoding="utf-8", errors="replace") as handle:
            handle.seek(offsets.get(path, 0))
            chunk = handle.read()
        if session_id in chunk:
            chunks.append(chunk)
    return "\n".join(chunks)


def _terminal_states(terminal: Any) -> list[str | None]:
    return terminal.evaluate_all("(elements) => elements.map((el) => el.dataset.state)")


def _open_connected_terminal(
    page: Page, base_url: str, session_id: str
) -> tuple[Locator, Locator, dict[str, Any]]:
    """Run one mock turn, open the Terminal view, and wait for a live REPL pane.

    Returns the visible main-terminal container, its xterm view, and the REPL
    terminal's inventory entry, with the session idle and the pane rendering.
    """
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
    _wait_for_pane_output(str(before["metadata"]["tmux_socket"]))
    assert _session_status(base_url, session_id) == "idle"
    return main_terminal, terminal, before


def test_repl_terminal_survives_tmux_server_death(
    page: Page, seeded_session: tuple[str, str], mock_llm_server_url: str
) -> None:
    base_url, session_id = seeded_session
    # The mock LLM server is session-scoped; drop any "Say hello" queue a
    # sibling test left behind so this session's turn routes to ours.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url, [{"text": _REPLY}], key="repl-tmux-death", match="Say hello"
    )

    main_terminal, terminal, before = _open_connected_terminal(page, base_url, session_id)
    socket_path = str(before["metadata"]["tmux_socket"])

    killed = _tmux(socket_path, "kill-server")
    assert killed.returncode == 0, killed.stderr
    assert _tmux(socket_path, "has-session").returncode != 0

    # Poll for the runner's verdict: the misleading fallback appears, or the view
    # reconnects to a live tmux pane; a transient inventory miss during the
    # rebuild is not a verdict.
    fallback = main_terminal.get_by_text(_FALLBACK, exact=True)
    deadline = time.monotonic() + _RUNNER_VERDICT_TIMEOUT_S
    while time.monotonic() < deadline:
        if fallback.count() > 0:
            break
        current = _repl_terminal(base_url, session_id)
        if (
            current is not None
            and str(current["metadata"]["tmux_socket"]) != socket_path
            and "connected" in _terminal_states(terminal)
            and _tmux(str(current["metadata"]["tmux_socket"]), "has-session").returncode == 0
        ):
            break
        time.sleep(0.5)

    after = _repl_terminal(base_url, session_id)
    assert after is not None, "runner dropped tui:main from the session's terminal inventory"
    assert fallback.count() == 0, f"Terminal view fell back to {_FALLBACK!r}"
    expect(terminal).to_have_attribute("data-state", "connected", timeout=60_000)
    assert _tmux(str(after["metadata"]["tmux_socket"]), "has-session").returncode == 0, (
        "tui:main is listed but its tmux server is not running"
    )
    assert _session_status(base_url, session_id) == "idle"


def test_repl_terminal_survives_repl_quit(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str], mock_llm_server_url: str
) -> None:
    """Quitting the embedded REPL leaves a diagnosable exit and a rebuilt terminal.

    ``/quit`` ends the REPL, the only process in ``tui:main``'s private tmux
    server. The pane must outlive it so the watcher reports the exit itself
    instead of logging the "tmux unavailable" cascade, and the runner must
    rebuild the terminal so the Terminal view reconnects without ever claiming
    the still-running harness stopped.
    """
    base_url, session_id = seeded_session
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": _REPLY}], key="repl-quit", match="Say hello")
    # Session setup is done; open the (possibly recorded) page only now.
    page: Page = request.getfixturevalue("page")

    main_terminal, terminal, before = _open_connected_terminal(page, base_url, session_id)
    before_socket = str(before["metadata"]["tmux_socket"])

    # xterm forwards keystrokes to the pty through its hidden input element.
    helper = terminal.locator("textarea.xterm-helper-textarea")
    _reach_repl_prompt(page, helper, before_socket)

    log_offsets = _runner_log_offsets()
    helper.focus()
    page.keyboard.type("/quit")
    _wait_for_pane_contains(before_socket, "/quit")
    page.keyboard.press("Enter")
    _wait_for_repl_exit(before_socket)

    # The runner's verdict: the view must never show the stopped-harness
    # fallback, and tui:main must come back on a fresh tmux server.
    fallback = main_terminal.get_by_text(_FALLBACK, exact=True)
    rebuilt: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    deadline = time.monotonic() + _RUNNER_VERDICT_TIMEOUT_S
    while time.monotonic() < deadline:
        assert fallback.count() == 0, "Terminal view fell back to 'The harness is not running.'"
        current = _repl_terminal(base_url, session_id)
        if (
            current is not None
            and str(current["metadata"]["tmux_socket"]) != before_socket
            and "connected" in _terminal_states(terminal)
        ):
            rebuilt = current
            break
        time.sleep(0.5)
    assert rebuilt is not None, (
        "runner did not rebuild tui:main after the REPL quit; "
        f"inventory entry={current!r} view states={_terminal_states(terminal)!r}"
    )
    after_socket = str(rebuilt["metadata"]["tmux_socket"])
    assert _tmux(after_socket, "has-session").returncode == 0, "rebuilt tui:main lost its server"
    assert _tmux(before_socket, "has-session").returncode != 0, "exited REPL's server was kept"
    _wait_for_pane_output(after_socket)
    expect(terminal).to_have_attribute("data-state", "connected")
    expect(fallback).to_have_count(0)
    assert _session_status(base_url, session_id) == "idle"

    logs = _runner_logs_since(log_offsets, session_id)
    assert f"Terminal exit observed: session={session_id} terminal=tui:main" in logs, (
        f"the runner's process log did not record the REPL exit:\n{logs[-2000:]}"
    )
    signature = _TMUX_UNAVAILABLE_SIGNATURE.search(logs)
    assert signature is None, (
        "the REPL's exit reaped its tmux server instead of leaving a dead pane: "
        f"{signature.group(0)}"
    )
    # End the page's context here so a recording stops on the rebuilt terminal
    # rather than on fixture teardown.
    page.context.close()
