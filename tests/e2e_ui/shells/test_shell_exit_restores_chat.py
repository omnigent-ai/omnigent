"""E2E: closing a shell by typing ``exit`` restores the chat surface.

Companion to ``test_new_shell.py`` — that suite covers *opening* a shell and
typing into it; this file covers the *close* path the opener leaves open:
what happens when the user types ``exit`` in the shell instead of clicking
``MainTerminalView``'s close X.

While a user shell owns the main column, ``MainTerminalView`` renders a
"Close shell" X and ``ConnectionIndicator`` self-hides the Chat/Terminal pill
(a "Chat" option under someone else's shell misreads as the shell being the
agent). When the shell exits, the runner deletes its resource
(``session.resource.deleted``) so it leaves the terminal list — but AppShell's
panel key keeps pointing at the now-dead shell. Before the fix that left
``isShellView`` stuck ``true``: the pill stayed hidden while
``MainTerminalView`` fell back to the agent's own terminal, stranding the
session in terminal-only view with no way back to chat (#479). The fix drops
``isShellView`` false the moment the shell is gone, so the pill reappears and
chat is reachable again.

Like the opener, none of this needs an LLM turn — the user, not the agent,
launches and exits the shell — so the test never sends a chat message.

Uses the function-scoped ``terminal_session`` fixture (the same
zsh-declaring, runner-bound session ``test_new_shell`` uses) for an
independent session per run.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Tab key prefix for a user-created ``zsh`` shell (see test_new_shell.py):
# ``createTerminal`` mints a ``u-<rand>`` session key, yielding the resource id
# ``terminal_zsh_u-<rand>`` and the tab key ``terminal:terminal_zsh_u-…``.
_USER_ZSH_KEY_RE = re.compile(r"^terminal:terminal_zsh_u-")


def _open_new_shell(page: Page) -> None:
    """Open the Shells tab and click the "+ New shell" row (mirrors test_new_shell).

    Scopes every lookup to the desktop "Workspace" rail so it never matches the
    hidden mobile drawer that mirrors the same controls.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("Shells")).click()
    # Single declared name → the row creates directly on click (no dropdown).
    rail.get_by_role("button", name="New shell").click()


def test_shell_exit_restores_chat_view(page: Page, terminal_session: tuple[str, str]) -> None:
    """Typing ``exit`` in a shell restores the Chat/Terminal pill (#479).

    Opens a shell, confirms the chrome-light shell view (close X, no pill),
    then types ``exit``. The shell's resource is deleted, so the Chat/Terminal
    pill must reappear — letting the user navigate back to chat. The pre-fix
    bug left the pill hidden here, stranding the session in terminal-only view.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    # The shell takes over the main column: chrome-light shell view focused on
    # the clicked shell, with a "Close shell" X and no Chat/Terminal pill.
    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=60_000)
    expect(main_terminal).to_have_attribute("data-active-terminal", _USER_ZSH_KEY_RE)
    expect(page.get_by_role("button", name="Close shell")).to_be_visible()
    expect(page.get_by_role("button", name="Chat", exact=True)).to_have_count(0)

    # Wait for the shell's xterm to connect before sending keystrokes — input
    # typed before the WS attach opens is dropped.
    terminal_view = page.get_by_test_id("terminal-view").last
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    # Focus xterm's hidden input (a plain container click doesn't reliably
    # focus the WebGL canvas in headless Chromium), then exit the shell.
    terminal_view.locator("textarea.xterm-helper-textarea").focus()
    page.keyboard.type("exit")
    page.keyboard.press("Enter")

    # The shell exited and its resource was deleted, so the shell view's
    # "Close shell" X is gone and — the #479 fix — the Chat/Terminal pill
    # reappears so the user can navigate back to chat. (The view itself drops
    # back to the agent's own terminal; that fallback is covered by the
    # MainTerminalView unit tests, so this e2e asserts only the chat-reachable
    # contract the fix restores.)
    expect(page.get_by_role("button", name="Close shell")).to_have_count(0, timeout=30_000)
    expect(page.get_by_role("button", name="Chat", exact=True)).to_have_count(1, timeout=30_000)
