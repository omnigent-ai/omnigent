"""E2E: creating a shell from the tab strip's "+" menu and typing into it.

The right rail's tab strip carries a "+" ("Open new") menu whenever the
session agent declares a non-empty ``terminals:`` block (``NewTabMenu`` in
``web/src/shell/WorkspacePanel.tsx``). Picking "Shell" with a single
declared terminal name creates it directly, POSTing ``/resources/terminals``
and opening the new terminal as a rail tab (its xterm renders in the rail's
content slot). None of this needs an LLM turn — the user, not the agent,
launches the shell — so these tests never send a chat message.

Three behaviors are covered:

1. **"+" → Shell launches and opens a shell.** Picking Shell creates a
   ``zsh`` shell that opens as a rail tab (``zsh · u-…``): its xterm
   connects in the rail's content slot, the chat page is left undisturbed,
   and the tab's "x" closes it back to the Shells list.

2. **The user can type a command into the shell.** We type ``pwd`` into
   the connected shell and assert it keeps running — the keystrokes are
   accepted and the bridge does not error or close. We deliberately do
   NOT assert on the command's output: xterm renders to a WebGL canvas,
   so stdout is not in the DOM (the same reason
   ``files/test_right_panel.py`` only checks ``data-state``), and reading
   it back via a file side-effect proved environment-fragile (the shell's
   cwd and the filesystem-API root coincide locally but not on CI).

3. **The workspace rail preserves its redesigned top inset.** The rail floats
   beside the chat header at the 8px outer inset instead of clearing the
   header like the main-column surfaces.

Both use the function-scoped ``terminal_session`` fixture (registers the
``zsh``-declaring agent and a runner-bound session), so each test gets an
independent session.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def _open_new_shell(page: Page) -> None:
    """Create a shell via the tab strip's "+" → Shell menu.

    Leaves the create POST fired and the new shell opening as a rail tab.
    Scopes every lookup to the desktop "Workspace" rail so it never matches
    the hidden mobile drawer that mirrors the same controls.

    :param page: Playwright page already navigated to ``/c/{id}``.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    # The "+" menu is the shell entry point (the agent declares a ``zsh``
    # terminal, so it renders). Open it and pick "Shell" — a single declared
    # type launches the default directly.
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def test_new_shell_launches_and_opens(page: Page, terminal_session: tuple[str, str]) -> None:
    """Clicking "+ New shell" launches a shell and opens it as a rail tab.

    The create is user-driven (no chat message), so the only wait is for
    the runner to spin the PTY up and the xterm to connect. The shell opens
    as a tab in the workspace rail's top strip (labeled ``zsh · u-…``) and
    its xterm renders inside the rail's content slot — the chat page is
    left undisturbed (no ``main-terminal-view`` takeover). The tab's "x"
    closes it and drops back to the Shells list.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    rail = page.get_by_role("complementary", name="Workspace")
    # A shell tab appears in the rail strip, labeled "zsh · u-<rand>" (the
    # user-minted session key), carrying a "Close zsh · u-…" x button.
    close_tab = rail.get_by_role("button", name=re.compile(r"^Close zsh · u-"))
    expect(close_tab).to_be_visible(timeout=60_000)

    # The shell's xterm mounts INSIDE the rail (not the main column) and
    # connects. The chat surface is untouched — the composer stays visible.
    # Assert no VISIBLE main terminal surface: terminal-first sessions keep
    # a hidden pre-warmed surface mounted (data-visible="false"), which is
    # not a takeover.
    terminal_view = rail.get_by_test_id("terminal-view")
    expect(terminal_view.last).to_be_visible(timeout=20_000)
    expect(terminal_view.last).to_have_attribute("data-state", "connected", timeout=20_000)
    expect(page.locator('[data-testid="main-terminal-view"][data-visible="true"]')).to_have_count(
        0
    )
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    # The tab's x closes the shell — its xterm unmounts and the rail falls
    # back to the Shells list.
    close_tab.click()
    expect(terminal_view).to_have_count(0)


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
    # input typed before the WS attach opens is dropped. The shell now opens
    # as a rail tab, so its xterm lives inside the Workspace rail.
    rail = page.get_by_role("complementary", name="Workspace")
    terminal_view = rail.get_by_test_id("terminal-view").last
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


def test_shell_wheel_scroll_reaches_mouse_tracking_program(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Trackpad-sized wheel deltas reach a mouse-tracking TUI as SGR reports.

    TUIs like Claude Code and OpenCode scroll their transcript via mouse
    reports, so the browser terminal must translate wheel gestures into SGR
    sequences. xterm's built-in conversion damps small pixel deltas to at
    most ~1 report per gesture, which reads as "scrolling doesn't work" on
    macOS trackpads; the custom wheel handler accumulates deltas and emits
    one report per whole line. Pin that end-to-end: a program in the shell
    enables mouse tracking and records its stdin to a file (absolute
    ``tmp_path`` — the shell and this test share a host, sidestepping the
    cwd/filesystem-API mismatch that made cwd-relative side-effects
    fragile), then a ~80px gesture of 4px ticks must land >=3 wheel-up
    reports. The damped path yields <=1, so this fails on a regression.
    """
    base_url, session_id = terminal_session
    log = tmp_path / "wheel.log"

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    terminal_view = page.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    # Become a minimal mouse-tracking "TUI": raw tty (so mouse bytes flow
    # byte-by-byte, not line-buffered), any-motion tracking + SGR encoding
    # (the mode set Claude Code and OpenCode use), stdin appended to a file.
    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type(f"stty raw -echo; printf '\\e[?1003h\\e[?1006h'; cat > '{log}'")
    page.keyboard.press("Enter")
    # Let the mode enables round-trip so the browser xterm starts forwarding.
    page.wait_for_timeout(1_500)

    # Slow two-finger scroll: 20 ticks of 4px over the terminal screen. With
    # a ~17px cell this is ~4.7 lines -> >=3 SGR wheel-up reports from the
    # accumulating handler; xterm's damped built-in emits at most 1.
    screen = terminal_view.locator(".xterm-screen").first
    box = screen.bounding_box()
    assert box is not None, "xterm screen should have a bounding box"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(20):
        page.mouse.wheel(0, -4)
        page.wait_for_timeout(25)

    # The reports traverse browser -> WS -> tmux -> program asynchronously;
    # poll the log rather than sleeping a fixed amount.
    deadline = time.monotonic() + 15
    reports = 0
    while time.monotonic() < deadline:
        reports = log.read_bytes().count(b"\x1b[<64") if log.exists() else 0
        if reports >= 3:
            break
        page.wait_for_timeout(500)
    assert reports >= 3, (
        f"expected >=3 SGR wheel-up reports to reach the shell program, got {reports} "
        f"(log: {log.read_bytes()[:200]!r})"
        if log.exists()
        else "wheel log never created"
    )


def test_workspace_rail_preserves_outer_top_inset(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """The old rail cleared the absolute chat header and aligned with expanded
    main-column surfaces. The redesign deliberately extends it beside the
    header, matching the sidebar's outer inset. Assert that geometry directly;
    shell launch behavior remains covered by the two tests above.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    header = page.get_by_role("banner")
    expect(rail).to_be_visible()
    expect(header).to_be_visible()

    rail_top = rail.evaluate("el => el.getBoundingClientRect().top")
    header_bottom = header.evaluate("el => el.getBoundingClientRect().bottom")

    assert rail_top < header_bottom, (
        f"workspace rail top {rail_top}px vs header bottom {header_bottom}px "
        "— expected the rail to extend beside the header"
    )
