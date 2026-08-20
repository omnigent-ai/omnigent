"""UI e2e: sidebar keyboard chords in a real browser (#7).

Covers the two hook changes end to end:

- ``usePinnedSessionHotkeys`` — in the browser, ``Ctrl/Cmd+Alt+<digit>`` jumps
  to the Nth *pinned* session (plain ``Cmd+digit`` is the native tab switch,
  so the browser path owns the Alt chord and matches ``e.code``).
- ``useSidebarToggleHotkeys`` — ``Ctrl/Cmd+Alt+[`` toggles the left sidebar
  (exercising the handler, AltGraph guard included, on a real keydown). The
  sidebar collapses by animating its width to zero rather than unmounting, so
  the assertion is on the sidebar ``aside``'s rendered width, not its
  visibility.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

# Mirrors PINNED_CONVERSATION_IDS_STORAGE_KEY in web/src/shell/sidebarNav.ts —
# pins are client-side state, so the test seeds them where the app reads them.
_PINNED_KEY = "omnigent:pinned-conversation-ids"

# Width of the sidebar itself — it's what the collapse chord animates (→0),
# and it's robust to the inner search control's markup. The old search input
# shrank to zero with the rail; the Search button (a flex item) floors at its
# content width, so probe the collapsing container instead.
_SIDEBAR_WIDTH_JS = """
() => {
  const el = document.querySelector('aside[aria-label="Conversations"]');
  return el ? el.getBoundingClientRect().width : -1;
}
"""


def test_numeric_chord_jumps_to_pinned_session(
    page: Page, live_server: str, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    # Pin the seeded session before the app boots (pins live in localStorage).
    page.add_init_script(
        f"window.localStorage.setItem({_PINNED_KEY!r}, {json.dumps(json.dumps([session_id]))})"
    )
    page.goto(base_url)
    # The hook reads the RENDERED Pinned section — wait for it (it only
    # appears once the session list has loaded and the pin resolved).
    expect(page.get_by_text("Pinned", exact=True)).to_be_visible(timeout=30_000)

    page.keyboard.press("Control+Alt+Digit1")
    page.wait_for_url(f"**/c/{session_id}", timeout=15_000)


def test_bracket_chord_toggles_left_sidebar(page: Page, live_server: str) -> None:
    page.goto(live_server)
    expect(page.get_by_test_id("sidebar-search-button")).to_be_visible(timeout=30_000)
    expanded_width = page.evaluate(_SIDEBAR_WIDTH_JS)
    assert expanded_width > 100, f"sidebar unexpectedly narrow at start ({expanded_width}px)"

    # Collapse: the sidebar animates its width to zero.
    page.keyboard.press("Control+Alt+BracketLeft")
    page.wait_for_function(f"() => ({_SIDEBAR_WIDTH_JS})() < 80", timeout=10_000)
    # Expand again.
    page.keyboard.press("Control+Alt+BracketLeft")
    page.wait_for_function(f"() => ({_SIDEBAR_WIDTH_JS})() > 100", timeout=10_000)
