"""E2E: ⌘/Ctrl+K opens the command palette and jumps to a session.

Covers the command palette added in ``ap-web/src/shell/CommandPalette.tsx`` and
its global hotkey (``useCommandPaletteHotkey``, ⌘/Ctrl+K, bound in
``AppShell``). The palette lists sessions from the same server-search source as
the sidebar and navigates to the picked one.

The flow: open the palette from a focused composer (proving the window-level
hotkey fires regardless of focus, like the session-switch hotkey), then select
the *other* seeded session from the palette's list and assert the route changes
to it.

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

import httpx
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"


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
