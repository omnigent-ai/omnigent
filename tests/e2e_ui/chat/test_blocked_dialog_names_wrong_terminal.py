"""E2E: chat blocked on a Claude Code dialog must route the user to the Terminal view.

A claude-native session has two kinds of terminal: the agent's terminal behind
the header's Chat view / Terminal view switcher, and user shells that open as
terminal tabs in the Workspace rail. A slash command sent from the chat composer
(``/theme``) opens a dialog inside Claude Code that is visible only in Terminal
view, so an indicator saying "open the terminal tab" sends a user with a rail
shell open to a terminal that shows no dialog.

Drives the real Claude Code CLI (mock model) so the ``dialog open`` status comes
from Claude's own status file; the injected-status variant lives in
``test_blocked_dialog_terminal_routing.py``.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_WORKING = '[data-testid="working-indicator"]'
_BLOCKED_ON_DIALOG = re.compile(r"dialog", re.IGNORECASE)
# The visible name of the header segment that shows the agent's terminal.
_NAMES_TERMINAL_VIEW = re.compile(r"terminal view", re.IGNORECASE)
# The Workspace rail's user shells — never where the dialog is.
_NAMES_TERMINAL_TAB = re.compile(r"terminal tab", re.IGNORECASE)
_TERMINAL_READY_TIMEOUT_MS = 180_000
_DIALOG_TIMEOUT_MS = 120_000


def _open_rail_shell(page: Page) -> Locator:
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()
    shell = rail.get_by_test_id("terminal-view").last
    expect(shell).to_have_attribute("data-state", "connected", timeout=90_000)
    return shell


def _send_from_composer(page: Page, text: str) -> None:
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.nightly
@pytest.mark.timeout(600)
@pytest.mark.browser_context_args(
    viewport={"width": 1440, "height": 900},
    record_video_size={"width": 1440, "height": 900},
)
def test_chat_blocked_on_dialog_names_the_terminal_that_holds_it(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
) -> None:
    base_url, session_id = native_claude_mock_session
    # Requested after the session fixture so a recording starts at the journey.
    page: Page = request.getfixturevalue("page")

    page.goto(f"{base_url}/c/{session_id}")
    terminal_segment = page.get_by_test_id("view-mode-terminal")
    expect(terminal_segment).to_have_attribute(
        "aria-label", "Terminal view", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    page.get_by_test_id("view-mode-chat").click()
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    rail_shell = _open_rail_shell(page)
    expect(composer).to_be_visible()

    _send_from_composer(page, "/theme")

    working = page.locator(_WORKING)
    expect(working).to_contain_text(_BLOCKED_ON_DIALOG, timeout=_DIALOG_TIMEOUT_MS)
    expect(rail_shell).to_be_visible()
    # With a user shell open as a terminal tab beside the chat, the indicator must
    # name the header's Terminal view, where the dialog is, not a "terminal tab".
    expect(working).to_contain_text(_NAMES_TERMINAL_VIEW)
    expect(working).not_to_contain_text(_NAMES_TERMINAL_TAB)

    # The hidden main view already targets the agent terminal; the rail shell is
    # the competing surface the button must not open.
    rail_terminal_id = rail_shell.get_attribute("data-terminal-id")
    assert rail_terminal_id, "rail shell is connected but has no data-terminal-id"
    rail_key = f"terminal:{rail_terminal_id}"
    agent_key = page.locator('[data-testid="main-terminal-view"]').first.get_attribute(
        "data-active-terminal"
    )
    assert agent_key and agent_key != rail_key, (agent_key, rail_key)

    # The indicator's own control takes the user to the dialog.
    open_terminal_view = working.get_by_role(
        "button", name=re.compile("terminal view", re.IGNORECASE)
    )
    expect(open_terminal_view).to_be_visible()
    open_terminal_view.click()
    main_terminal = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
    expect(main_terminal).to_be_visible(timeout=30_000)
    expect(main_terminal).to_have_attribute("data-active-terminal", agent_key)
    agent_terminal = main_terminal.locator('[data-testid="terminal-view"]').last
    expect(agent_terminal).to_have_attribute("data-state", "connected", timeout=60_000)
    expect(agent_terminal).to_have_attribute(
        "data-terminal-id", agent_key.removeprefix("terminal:")
    )
    expect(terminal_segment).to_have_attribute("aria-pressed", "true")
    expect(rail_shell).to_be_visible()
