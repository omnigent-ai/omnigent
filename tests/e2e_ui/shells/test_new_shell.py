"""E2E: the rail's "+ New shell" affordance and typing into the shell.

The right rail's Shells tab shows by default whenever the session agent
declares a non-empty ``terminals:`` block — its empty state carries a
virtual "+ New shell" row (``NewTerminalButton`` in
``ap-web/src/shell/NewTerminalButton.tsx``). With a single declared
terminal name the row creates the shell directly on click (no dropdown),
POSTing ``/resources/terminals`` and handing the new terminal's tab key
to ``onExpand``, which opens it in the main column via
``MainTerminalView``. None of this needs an LLM turn — the user, not the
agent, launches the shell — so these tests never send a chat message.

Two behaviors are covered:

1. **"+ New shell" launches and opens a shell.** Clicking the row creates
   a ``zsh`` shell and replaces the main session view with it: the
   chrome-light shell view (``MainTerminalView``'s ``isShellView``) shows
   a header naming the shell and a "Close shell" X, its xterm connects,
   and the X returns to the conversation surface.

2. **The user can type a command into the shell.** xterm renders to a
   WebGL canvas, so the shell's output is not in the DOM and can't be
   asserted on directly (the same reason ``files/test_right_panel.py``
   only checks ``data-state``). Instead we type a ``pwd`` that redirects
   into the session workspace and read the file back through the
   filesystem API — proving the keystrokes reached the PTY and the
   command executed.

Both use the function-scoped ``terminal_session`` fixture (registers the
``zsh``-declaring agent and a runner-bound session), so each test gets an
independent session.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# The ``zsh`` terminal (and the session's ``default`` environment
# filesystem) is rooted at the repo checkout — the runner inherits the
# pytest process cwd, so a shell ``pwd`` prints this path and a file
# written here reads back through the filesystem API by its bare name.
# This file sits one level deeper than ``conftest.py`` (``shells/`` vs
# ``e2e_ui/``), so the checkout root is ``parents[3]``, not ``parents[2]``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Tab key prefix for a user-created ``zsh`` shell: ``createTerminal``
# mints a ``u-<rand>`` session key, yielding the resource id
# ``terminal_zsh_u-<rand>`` and the tab key ``terminal:terminal_zsh_u-…``.
_USER_ZSH_KEY_RE = re.compile(r"^terminal:terminal_zsh_u-")


def _read_file(base_url: str, session_id: str, path: str) -> str:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["content"]


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


def test_new_shell_runs_typed_command(page: Page, terminal_session: tuple[str, str]) -> None:
    """Typing ``pwd`` into a freshly created shell executes in the PTY.

    xterm's WebGL canvas keeps the rendered output out of the DOM, so we
    can't assert on the on-screen text. Instead the typed ``pwd`` is
    redirected to a marker file at the shell's cwd — which is the repo
    checkout, the same root the session's ``default`` environment
    filesystem serves — and we read it back through the filesystem API: a
    non-empty absolute path proves the keystrokes reached the PTY and the
    command ran.
    """
    base_url, session_id = terminal_session
    marker = f"shell_pwd_{uuid.uuid4().hex[:8]}.txt"
    # Absolute redirect target at the filesystem-API root, so the read-back
    # by bare name resolves to the same file regardless of the shell's cwd.
    abs_target = _REPO_ROOT / marker

    try:
        page.goto(f"{base_url}/c/{session_id}")
        _open_new_shell(page)

        # Wait for the shell's xterm to connect before sending keystrokes —
        # input typed before the WS attach opens is dropped.
        terminal_view = page.get_by_test_id("terminal-view").last
        expect(terminal_view).to_be_visible(timeout=60_000)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

        # Focus xterm's hidden input (a plain container click doesn't
        # reliably focus the WebGL canvas in headless Chromium), then type a
        # pwd that persists its output to a file the API can read back. The
        # brief wait lets the attached shell finish drawing its first prompt
        # so the keystrokes aren't swallowed mid-redraw.
        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        page.wait_for_timeout(1000)
        page.keyboard.type(f'pwd > "{abs_target}"')
        page.keyboard.press("Enter")

        # Poll the file endpoint until the redirected pwd output lands.
        deadline = time.monotonic() + 20.0
        content = ""
        while time.monotonic() < deadline:
            try:
                content = _read_file(base_url, session_id, marker)
            except httpx.HTTPStatusError:
                content = ""  # not written yet
            if content.strip():
                break
            time.sleep(0.5)

        assert content.strip().startswith("/"), (
            f"shell never wrote pwd output to {marker}; last server content: {content!r}"
        )
    finally:
        abs_target.unlink(missing_ok=True)
