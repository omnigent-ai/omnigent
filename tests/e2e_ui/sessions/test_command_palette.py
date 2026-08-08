"""E2E: the two overlay chords — ⌘/Ctrl+K commands, ⌘/Ctrl+Shift+F sessions.

Covers ``ap-web/src/shell/CommandPalette.tsx`` and its global hotkeys
(``useCommandPaletteHotkey`` / ``useSessionSearchHotkey``, both bound in
``AppShell``). Following VS Code, the two are separate surfaces: ⌘K runs app
commands and lists no sessions at all, while ⌘⇧F searches sessions from the
same server-search source as the sidebar and navigates to the picked one.

Both open from a focused composer, proving the window-level hotkeys fire
regardless of focus (same contract as the session-switch hotkey).

No LLM turn is needed — this is pure client-side keyboard + routing — so it
skips the nightly/real-agent markers the approval suites carry. Two runner-bound
sessions come from the ``seeded_session_pair`` fixture; both are recent and
non-archived, so both appear in session search's default (empty-query) list.

Server-side search-query *filtering* is left to the Vitest unit tests
(``CommandPalette.test.tsx``) and ``test_sidebar_search.py``: the server's
search reindex is asynchronous (see ``useConversations.ts``), which would make a
"type then expect filtered" assertion timing-dependent here. Selecting from the
listed sessions exercises the same open → select → navigate path
deterministically.
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


def _seed_titled_pair(base_url: str, session_a: str, session_b: str) -> None:
    """Give both fixture sessions stable, assertable titles."""
    _set_title(base_url, session_a, "e2e-palette-a")
    _set_title(base_url, session_b, "e2e-palette-b")


def _focus_composer(page: Page) -> None:
    """Put focus in a text field, so the window-level hotkey has to beat it."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.click()


def test_session_search_hotkey_switches_session(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """⌘/Ctrl+Shift+F opens session search; picking session B navigates to it."""
    base_url, session_a, session_b = seeded_session_pair
    _seed_titled_pair(base_url, session_a, session_b)

    page.goto(f"{base_url}/c/{session_a}")

    # Both sessions must be loaded so the search list holds them.
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    _focus_composer(page)

    # CI runs Linux chromium → Control; the hook also accepts Cmd via metaKey
    # on macOS.
    page.keyboard.press("Control+Shift+f")

    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("command-palette-input")).to_be_focused()

    # Pick the other session from inside the overlay and assert we navigate.
    dialog.get_by_text("e2e-palette-b").click()

    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=10_000)
    # The overlay closes on select.
    expect(page.get_by_test_id("command-palette-input")).to_have_count(0)


def test_command_palette_lists_commands_not_sessions(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """⌘/Ctrl+K runs commands only — the session list belongs to ⌘⇧F."""
    base_url, session_a, session_b = seeded_session_pair
    _seed_titled_pair(base_url, session_a, session_b)

    page.goto(f"{base_url}/c/{session_a}")
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible(timeout=30_000)

    _focus_composer(page)
    page.keyboard.press("Control+k")

    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(dialog.get_by_text("Go to Settings")).to_be_visible()
    # The separation this suite exists for: a session that IS listed under ⌘⇧F
    # must not appear here. Scoped to the dialog — the title also renders in the
    # sidebar row behind it.
    expect(dialog.get_by_text("e2e-palette-b")).to_have_count(0)
