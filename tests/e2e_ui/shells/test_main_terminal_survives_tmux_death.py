"""E2E regression: the embedded main terminal must survive its tmux backing dying.

A runner-hosted SDK-harness session (any non-native harness, here
``openai-agents``) auto-creates an Omnigent REPL terminal ``tui:main`` — the
embedded terminal the web SPA shows behind the Chat/Terminal toggle
(``omnigent/runner/native/orchestration.py:_auto_create_repl_terminal``). That
terminal runs ``omnigent attach`` inside a private tmux server and is observed
as an *auxiliary* terminal.

A threaded idle watcher polls the pane with ``tmux capture-pane``
(``omnigent/inner/terminal.py:_idle_watch_loop_threaded``). When the pane's
tmux server goes away — the REPL process crashes/exits, the tmux server dies —
the watcher confirms with ``has-session``, flips ``running=False`` and fires
the exit callback. The runner must then rebuild the embedded terminal instead
of silently dropping it: before this behavior existed, ``tui:main`` vanished
from the resource inventory and a user watching the Terminal view saw the live
pane replaced by the misleading "The harness is not running." fallback even
though the session's SDK harness was perfectly alive.

The test drives the real user path:

1. create a runner-hosted ``openai-agents`` session (auto-creates ``tui:main``),
2. drive one chat turn so the runner initialises the session + REPL terminal,
3. open the Terminal view and confirm the embedded terminal is ``connected``,
4. kill the tmux server backing ``tui:main`` (``tmux kill-server`` on its
   private socket — the faithful stand-in for the REPL process/tmux dying),
5. assert the runner recreates ``tui:main`` (a fresh resource with a new tmux
   socket) and the Terminal view reconnects instead of ending on the
   "harness is not running" fallback.
"""

from __future__ import annotations

import subprocess

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_REPL_TERMINAL_NAME = "tui:main"


def _list_terminals(base_url: str, session_id: str) -> list[dict]:
    """Return the session's live terminal resources from the runner inventory."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def _find_repl_terminal(base_url: str, session_id: str) -> dict | None:
    for term in _list_terminals(base_url, session_id):
        if term.get("name") == _REPL_TERMINAL_NAME:
            return term
    return None


def test_main_terminal_recreated_after_tmux_server_dies(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """``tui:main`` is rebuilt and reconnects when its tmux server dies.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` — a runner-bound
        ``openai-agents`` (non-native SDK harness) session.
    :param mock_llm_server_url: Mock LLM the seeded agent's turn is routed to.
    :returns: None.
    """
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "hello from the agent"}],
        key="repl-terminal-recovery-turn",
        match="Say hello",
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=60_000)

    # One real chat turn drives the runner's session-init handshake, which
    # auto-creates the tui:main REPL terminal and stamps the omnigent.ui:terminal
    # label that gates the web Chat/Terminal toggle.
    composer.fill("Say hello")
    page.get_by_role("button", name="Send", exact=True).click()

    # The REPL terminal auto-creates on the runner. Poll the inventory until it
    # appears and grab its private tmux socket (published in the resource
    # metadata) so we can make that tmux server disappear like production.
    repl_terminal: dict | None = None
    for _ in range(90):
        repl_terminal = _find_repl_terminal(base_url, session_id)
        if repl_terminal is not None:
            break
        page.wait_for_timeout(1000)
    assert repl_terminal is not None, (
        "tui:main REPL terminal was never auto-created for the runner-hosted SDK session"
    )
    tmux_socket = repl_terminal["metadata"]["tmux_socket"]
    assert tmux_socket, "REPL terminal resource did not publish its tmux socket"

    # Open the Terminal view and confirm the embedded REPL pane connects — this
    # is the live terminal the user is watching when its backing dies.
    terminal_toggle = page.get_by_test_id("view-mode-terminal")
    expect(terminal_toggle).to_be_visible(timeout=30_000)
    terminal_toggle.click()
    terminal_surface = page.get_by_test_id("terminal-view").first
    expect(terminal_surface).to_have_attribute("data-state", "connected", timeout=60_000)

    # Faithful fault injection: the tmux server backing tui:main goes away
    # (the REPL process crashing / tmux dying is what production hits). The
    # runner is a sibling process on this machine, so its private tmux socket
    # is reachable here.
    killed = subprocess.run(
        ["tmux", "-S", tmux_socket, "kill-server"],
        capture_output=True,
        text=True,
    )
    assert killed.returncode == 0, (
        f"failed to kill the REPL tmux server: rc={killed.returncode} stderr={killed.stderr!r}"
    )

    # The idle watcher (1s poll, 3-failure threshold) detects tmux is gone and
    # fires the auxiliary exit. The runner must rebuild the embedded terminal:
    # poll until tui:main is listed again with a fresh tmux socket. Without the
    # rebuild, the resource stays gone and this times out — the main terminal
    # has silently disappeared while the SDK session is still alive.
    recreated: dict | None = None
    for _ in range(60):
        candidate = _find_repl_terminal(base_url, session_id)
        candidate_socket = (
            (candidate.get("metadata") or {}).get("tmux_socket") if candidate is not None else None
        )
        if candidate_socket and candidate_socket != tmux_socket:
            recreated = candidate
            break
        page.wait_for_timeout(1000)
    assert recreated is not None, (
        "tui:main was not recreated after its tmux server died — the embedded "
        "main terminal silently disappeared while the SDK session was still "
        "alive"
    )

    # The user-visible recovery: the Terminal view reconnects to the rebuilt
    # pane instead of ending on the misleading "harness is not running"
    # fallback while the session is still alive.
    terminal_surface = page.get_by_test_id("terminal-view").first
    expect(terminal_surface).to_have_attribute("data-state", "connected", timeout=60_000)
    expect(page.get_by_text("The harness is not running.")).to_have_count(0)
    expect(page.get_by_text("Resume the session to reconnect the terminal.")).to_have_count(0)
