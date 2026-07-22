"""E2E: leaving a user shell returns to the view it was opened from.

In a terminal-first session a shell opened from the rail's **Shells** tab
takes over the main column, and while it does the ``Chat | Terminal``
pill is hidden (``ConnectionIndicator`` gates on ``isShellView``). That
makes the way *out* of the shell the only thing standing between the user
and their previous view — so it must land where they came from:

* opened from **Chat** → back to chat (no terminal surface),
* opened from the agent **Terminal** view → back to the agent terminal,
  with the pill restored.

Two exit paths reach that restore and both are covered here:

1. **The header ✕** (``MainTerminalView``'s "Close shell"), which calls
   ``exitShellView()``.
2. **Typing ``exit``**, which kills the PTY; the runner deletes the
   resource and the shell drops out of ``terminals``. ``AppShell``
   notices the open panel key has gone stale and exits the shell view
   itself. Without that, the panel key keeps pointing at the dead shell:
   ``MainTerminalView`` falls back to rendering the agent terminal (so no
   shell ✕) while the pill stays hidden — no way back to chat at all.

The ``exit`` path is the harder assertion, because the *rendered pane*
looks right either way (``MainTerminalView`` re-targets the agent
terminal on its own). The pill's visibility is what separates fixed from
broken, so that is what these tests assert.

Uses the function-scoped ``terminal_session`` fixture: a runner-bound SDK
session, which the runner makes terminal-first by auto-creating the
Omnigent REPL terminal (``terminal_tui_main``) and stamping
``omnigent.ui: terminal``. No chat message is ever sent — the user, not
the agent, opens these shells.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# The agent's own terminal for an SDK session — the pane the pill's
# "Terminal" option shows, and the restore target when a shell was opened
# from it.
_AGENT_TERMINAL_KEY = "terminal:terminal_tui_main"
# A user-created ``zsh`` shell: ``createTerminal`` mints a ``u-<rand>``
# session key, so the resource id is ``terminal_zsh_u-<rand>``.
_USER_ZSH_KEY_RE = re.compile(r"^terminal:terminal_zsh_u-")


def _open_new_shell(page: Page) -> None:
    """Open the Shells tab and click the "+ New shell" row.

    Scopes every lookup to the desktop "Workspace" rail so it never
    matches the hidden mobile drawer mirroring the same controls.

    :param page: Playwright page already navigated to ``/c/{id}``.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("Shells")).click()
    # A single declared terminal name → the row creates directly (no dropdown).
    rail.get_by_role("button", name="New shell").click()


def _expect_shell_took_over(page: Page):
    """Wait for a freshly created shell to own the main column.

    :param page: Playwright page with a "+ New shell" click already fired.
    :returns: The ``main-terminal-view`` locator, focused on the shell.
    """
    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=60_000)
    expect(main_terminal).to_have_attribute("data-active-terminal", _USER_ZSH_KEY_RE)
    # Shell view: the pill is hidden, so the header ✕ is the only way out.
    expect(page.get_by_role("button", name="Close shell")).to_be_visible()
    expect(page.get_by_role("button", name="Terminal", exact=True)).to_have_count(0)
    return main_terminal


def _open_agent_terminal_view(page: Page):
    """Switch the pill to the agent's Terminal view and wait for it.

    The "Terminal" option is disabled until the runner's REPL terminal
    exists, so this tolerates the spin-up window.

    :param page: Playwright page already navigated to ``/c/{id}``.
    :returns: The ``main-terminal-view`` locator, focused on the agent terminal.
    """
    terminal_option = page.get_by_role("button", name="Terminal", exact=True)
    expect(terminal_option).to_be_visible(timeout=60_000)
    expect(terminal_option).to_be_enabled(timeout=60_000)
    terminal_option.click()

    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=30_000)
    expect(main_terminal).to_have_attribute("data-active-terminal", _AGENT_TERMINAL_KEY)
    return main_terminal


def test_close_shell_opened_from_chat_returns_to_chat(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A shell opened from chat closes back to chat, not the terminal.

    The baseline half of the restore contract: with no prior terminal
    view, the ✕ must leave the terminal surface entirely rather than
    stranding the user on the agent's terminal.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    main_terminal = _expect_shell_took_over(page)

    page.get_by_role("button", name="Close shell").click()

    # Back on the conversation surface: no terminal pane at all, and the
    # pill is showing Chat.
    expect(main_terminal).to_have_count(0)
    expect(page.get_by_role("button", name="Chat", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )


def test_close_shell_opened_from_terminal_returns_to_terminal(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A shell opened from the agent Terminal view closes back to it.

    Previously the ✕ hard-coded ``setView("chat")``, so opening a shell
    from the Terminal view and closing it silently discarded the
    Chat-vs-Terminal choice and dumped the user into chat.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    main_terminal = _open_agent_terminal_view(page)

    _open_new_shell(page)
    _expect_shell_took_over(page)

    page.get_by_role("button", name="Close shell").click()

    # Back on the AGENT's terminal — still the terminal surface, with the
    # pill restored and "Terminal" selected. A regression drops to chat,
    # which removes `main-terminal-view` entirely.
    expect(main_terminal).to_be_visible()
    expect(main_terminal).to_have_attribute("data-active-terminal", _AGENT_TERMINAL_KEY)
    expect(page.get_by_role("button", name="Terminal", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )


def test_typing_exit_in_shell_restores_the_previous_view(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Typing ``exit`` leaves shell view instead of stranding the user.

    The shell's PTY dies, the runner deletes the resource, and the shell
    disappears from the terminal list. The pane then falls back to the
    agent terminal on its own — but the open panel key still named the
    dead shell, so the app stayed in "shell view": pill hidden, and no
    ✕ rendered (the pane is the agent's, not a shell). Assert the pill
    is back, which is what actually distinguishes restored from stranded.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_agent_terminal_view(page)

    _open_new_shell(page)
    main_terminal = _expect_shell_took_over(page)

    # Keystrokes sent before the WS attach opens are dropped, so wait for
    # the shell's xterm to connect first.
    terminal_view = page.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    # A plain container click doesn't reliably focus the WebGL canvas in
    # headless Chromium; focus xterm's hidden input instead.
    terminal_view.locator("textarea.xterm-helper-textarea").focus()
    page.keyboard.type("exit")
    page.keyboard.press("Enter")

    # The dead shell drops out of the list and the view restores itself —
    # no click needed. Back on the agent terminal with the pill usable.
    expect(main_terminal).to_have_attribute(
        "data-active-terminal", _AGENT_TERMINAL_KEY, timeout=30_000
    )
    expect(page.get_by_role("button", name="Close shell")).to_have_count(0)
    expect(page.get_by_role("button", name="Terminal", exact=True)).to_have_attribute(
        "aria-pressed", "true", timeout=30_000
    )
