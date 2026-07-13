"""E2E: the rail's "+ New shell" affordance and typing into the shell.

The right rail's Shells tab shows by default whenever the session agent
declares a non-empty ``terminals:`` block — its empty state carries a
virtual "+ New shell" row (``NewTerminalButton`` in
``web/src/shell/NewTerminalButton.tsx``). With a single declared
terminal name the row creates the shell directly on click (no dropdown),
POSTing ``/resources/terminals`` and handing the new terminal's tab key
to ``onExpand``, which opens it in the main column via
``MainTerminalView``. None of this needs an LLM turn — the user, not the
agent, launches the shell — so these tests never send a chat message.

Three behaviors are covered:

1. **"+ New shell" launches and opens a shell.** Clicking the row creates
   a ``zsh`` shell and replaces the main session view with it: the
   chrome-light shell view (``MainTerminalView``'s ``isShellView``) shows
   a header naming the shell and a "Close shell" X, its xterm connects,
   and the X returns to the conversation surface.

2. **The user can type a command into the shell.** We type ``pwd`` into
   the connected shell and assert it keeps running — the keystrokes are
   accepted and the bridge does not error or close. We deliberately do
   NOT assert on the command's output: xterm renders to a WebGL canvas,
   so stdout is not in the DOM (the same reason
   ``files/test_right_panel.py`` only checks ``data-state``), and reading
   it back via a file side-effect proved environment-fragile (the shell's
   cwd and the filesystem-API root coincide locally but not on CI).

3. **The opened shell card lines up with the workspace rail.** The
   expanded card and the rail both clear the 56px chat header, so their
   top edges must agree; a clearance mismatch is asserted geometrically.

Both use the function-scoped ``terminal_session`` fixture (registers the
``zsh``-declaring agent and a runner-bound session), so each test gets an
independent session.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Tab key prefix for a user-created ``zsh`` shell: ``createTerminal``
# mints a ``u-<rand>`` session key, yielding the resource id
# ``terminal_zsh_u-<rand>`` and the tab key ``terminal:terminal_zsh_u-…``.
_USER_ZSH_KEY_RE = re.compile(r"^terminal:terminal_zsh_u-")


def _open_new_shell(page: Page) -> None:
    """Open the Shells tab and click the "+ New shell" row.

    Leaves the rail's Shells tab active with the create POST fired. Scopes
    every lookup to the desktop "Workspace" rail so it never matches the
    hidden mobile drawer that mirrors the same controls.

    :param page: Playwright page already navigated to ``/c/{id}``.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    # Shells is present by default — the agent declares a ``zsh`` terminal,
    # so the tab shows before any shell exists with the "+ New shell"
    # affordance as its whole content.
    rail.get_by_role("tab", name=re.compile("Shells")).click()
    # Single declared name → the row creates directly on click (no dropdown).
    rail.get_by_role("button", name="New shell").click()


def test_new_shell_launches_and_opens(page: Page, terminal_session: tuple[str, str]) -> None:
    """Clicking "+ New shell" launches a shell and opens it in the main view.

    The create is user-driven (no chat message), so the only wait is for
    the runner to spin the PTY up and the xterm to connect. The opened
    view must focus the freshly-created shell — a ``terminal_tui_main``
    active key here would mean the new key was dropped and the view fell
    back to the agent's REPL — and render as the chrome-light shell view
    (header + close X, no Chat/Terminal pill). The X returns to chat.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    # The new shell takes over the main column (terminal-first session).
    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=60_000)
    # The view focuses the CLICKED shell, not the agent's REPL terminal.
    expect(main_terminal).to_have_attribute("data-active-terminal", _USER_ZSH_KEY_RE)
    # Chrome-light shell view: the shell header names it and a "Close
    # shell" X is present; the Chat/Terminal pill is hidden (a "Chat"
    # option under a shell misreads as the shell being the agent).
    expect(main_terminal).to_contain_text("zsh")
    expect(page.get_by_role("button", name="Chat", exact=True)).to_have_count(0)

    # The shell's xterm mounts in the main pane and connects.
    terminal_view = page.get_by_test_id("terminal-view")
    expect(terminal_view.last).to_be_visible(timeout=20_000)
    expect(terminal_view.last).to_have_attribute("data-state", "connected", timeout=20_000)

    # The header's close X is the way back to the conversation surface.
    page.get_by_role("button", name="Close shell").click()
    expect(main_terminal).to_have_count(0)


def test_new_shell_accepts_typed_command(page: Page, terminal_session: tuple[str, str]) -> None:
    """The user can type a command into a freshly created shell.

    Types ``pwd`` into the connected shell and asserts the bridge keeps
    running: the keystrokes are accepted and the PTY neither errors nor
    closes. We do NOT assert on the command's output — xterm renders to a
    WebGL canvas, so stdout never reaches the DOM, and capturing it via a
    file side-effect proved environment-fragile (the shell cwd and the
    filesystem-API root line up locally but not on CI). Verifying the shell
    stays healthy after input is the portable signal.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    # Wait for the shell's xterm to connect before sending keystrokes —
    # input typed before the WS attach opens is dropped.
    terminal_view = page.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    # Focus xterm's hidden input (a plain container click doesn't reliably
    # focus the WebGL canvas in headless Chromium), then type a command.
    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type("pwd")
    page.keyboard.press("Enter")

    # The shell accepted the command and stays live — no bridge error, no
    # "terminal session ended". A regression that drops user input or kills
    # the PTY on first keystroke would flip this out of ``connected``.
    expect(terminal_view).to_have_attribute("data-state", "connected")


def test_expanded_shell_card_top_aligns_with_workspace_rail(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """The expanded shell card's top edge lines up with the workspace rail.

    Both panels clear the same 56px absolute chat header: the terminal card
    via ``pt-14`` on ``MainTerminalView``'s wrapper, the rail via ``mt-14``
    on its ``<aside>``. When the two clearances disagree (the old ``pt-16``
    left the card 8px lower than the rail) the panel tops read as visibly
    ragged. Assert the two top edges agree within a hairline tolerance so a
    future clearance drift is caught geometrically, not just by eye.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=60_000)

    # The bordered card is the wrapper's only child; its top edge is what
    # the eye reads as the "top of the middle box". The rail's ``<aside>``
    # is itself the floating card, so its own top edge is the comparison.
    card = main_terminal.locator("> div").first
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail).to_be_visible()

    card_top = card.evaluate("el => el.getBoundingClientRect().top")
    rail_top = rail.evaluate("el => el.getBoundingClientRect().top")
    assert abs(card_top - rail_top) <= 2, (
        f"terminal card top {card_top}px vs workspace rail top {rail_top}px "
        "— the expanded shell card is not aligned with the rail"
    )
