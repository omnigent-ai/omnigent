"""E2E: ⌘/Ctrl+K opens the command palette and jumps to a session.

Covers the command palette added in ``ap-web/src/shell/CommandPalette.tsx`` and
its global hotkey (``useCommandPaletteHotkey``, ⌘/Ctrl+K, bound in
``AppShell``). The palette lists sessions from the same server-search source as
the sidebar and navigates to the picked one.

The flow: open the palette from a focused composer (proving the window-level
hotkey fires regardless of focus, like the session-switch hotkey), then select
the *other* seeded session from the palette's list and assert the route changes
to it.

Two more chord-ownership cases live here because they are about who gets ⌘K
and ⌘↑/↓ when another surface is focused:

- ``test_command_palette_chord_with_a_terminal_focused``: ⌘K opens the palette
  over a focused terminal, while Ctrl+K stays with the PTY.
- ``test_session_switch_chord_yields_to_the_open_palette``: with the palette
  open, the switch chord moves *its* highlight and does not navigate behind it.

No LLM turn is needed — this is pure client-side keyboard + routing — so it
skips the nightly/real-agent markers the approval suites carry. Two runner-bound
sessions come from the ``seeded_session_pair`` fixture; both are recent and
non-archived, so both appear in the palette's default (empty-query) list.

Server-side search-query *filtering* is left to the Vitest unit tests
(``CommandPalette.test.tsx``): the server's search reindex is asynchronous (see
``useConversations.ts``), which would make a "type then expect filtered" e2e
assertion timing-dependent. Selecting from the listed sessions exercises the
same open → select → navigate path deterministically.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_COMPOSER = "Ask the agent anything…"
# The palette's search box — its user-visible handle, stable across the
# dialog's internals.
_PALETTE_INPUT = "Search sessions or run a command"


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Title a session via ``PATCH /v1/sessions/{id}`` so its row is legible."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_command_palette_opens_and_switches_session(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """⌘/Ctrl+K opens the palette; picking session B navigates to it."""
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, "e2e-palette-a")
    _set_title(base_url, session_b, "e2e-palette-b")

    page.goto(f"{base_url}/c/{session_a}")

    # Both sessions must be loaded so the palette's session list holds them.
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    # Focus the composer first — the hotkey is window-level and must fire even
    # from a focused text field (same contract as the session-switch hotkey).
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.click()

    # Open the palette. CI runs Linux chromium → Control; the hook also accepts
    # Cmd via metaKey on macOS.
    page.keyboard.press("Control+k")

    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("command-palette-input")).to_be_focused()

    # Pick the other session from inside the palette and assert we navigate to it.
    dialog.get_by_text("e2e-palette-b").click()

    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=10_000)
    # The palette closes on select.
    expect(page.get_by_test_id("command-palette-input")).to_have_count(0)


def test_command_palette_chord_with_a_terminal_focused(
    page: Page,
    terminal_session: tuple[str, str],
) -> None:
    """⌘K opens the palette from a focused terminal; Ctrl+K does not.

    The hotkey used to yield ⌘K to any focused ``.xterm``. But xterm forwards
    only the *control* variant — it writes ``\x0b`` (^K, kill-to-end-of-line)
    to the PTY — and drops Cmd chords on macOS entirely, so yielding ⌘K left it
    dead: no palette, and nothing delivered to the shell either.
    """
    base_url, session_id = terminal_session
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()

    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    # xterm renders to a canvas; its hidden helper textarea is what holds focus
    # (a container click doesn't reliably focus the canvas in headless).
    terminal_view.locator("textarea.xterm-helper-textarea").focus()

    # Ctrl+K belongs to the PTY — the palette must leave it alone.
    page.keyboard.press("Control+k")
    expect(page.get_by_placeholder(_PALETTE_INPUT)).to_have_count(0)

    # Cmd+K reaches nothing else, so the palette claims it.
    page.keyboard.press("Meta+k")
    expect(page.get_by_placeholder(_PALETTE_INPUT)).to_be_visible(timeout=10_000)


def test_session_switch_chord_yields_to_the_open_palette(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """With the palette open, the switch chord moves its highlight, nothing more.

    ``useSessionSwitchHotkey`` yields on ``defaultPrevented`` rather than on
    "focus is in a text field", so the guard only holds while cmdk really does
    claim this chord (it binds Cmd/Ctrl+↑/↓ to jump to the first/last row).
    Asserting the highlight *moved* is what pins that: a cmdk upgrade that
    stopped binding these chords would fail here rather than silently start
    navigating the app behind the open palette.
    """
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, "e2e-yield-a")
    _set_title(base_url, session_b, "e2e-yield-b")

    page.goto(f"{base_url}/c/{session_a}")
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_be_visible(timeout=30_000)

    page.keyboard.press("ControlOrMeta+k")
    palette_input = page.get_by_placeholder(_PALETTE_INPUT)
    expect(palette_input).to_be_visible(timeout=10_000)
    # cmdk highlights a row as soon as its list renders; the chord below has
    # nothing to move until it does.
    selected = page.locator("[cmdk-item][data-selected=true]")
    expect(selected).to_have_count(1, timeout=10_000)
    before_row = selected.inner_text()

    page.keyboard.press("ControlOrMeta+ArrowDown")

    # cmdk consumed it: the highlight moved to the last row...
    expect(page.locator("[cmdk-item][data-selected=true]")).not_to_have_text(
        before_row, timeout=10_000
    )
    # ...and the app did NOT navigate behind the still-open palette.
    expect(palette_input).to_be_visible()
    assert page.url.endswith(f"/c/{session_a}"), f"route moved behind the palette: {page.url}"
