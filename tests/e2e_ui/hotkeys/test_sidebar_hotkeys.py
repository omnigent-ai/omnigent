"""UI e2e: sidebar keyboard chords in a real browser (#7).

Covers the two hook changes end to end:

- ``usePinnedSessionHotkeys`` — in the browser, ``Ctrl/Cmd+Alt+<digit>`` jumps
  to the Nth *pinned* session (plain ``Cmd+digit`` is the native tab switch,
  so the browser path owns the Alt chord and matches ``e.code``).
- ``useSidebarToggleHotkeys`` — ``Ctrl/Cmd+Alt+[`` toggles the left sidebar
  (exercising the handler, AltGraph guard included, on a real keydown). The
  sidebar collapses to an icon rail rather than unmounting, so the assertion
  is on the search input's rendered width, not its visibility.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

# Mirrors PINNED_CONVERSATION_IDS_STORAGE_KEY in web/src/shell/sidebarNav.ts —
# pins are client-side state, so the test seeds them where the app reads them.
_PINNED_KEY = "omnigent:pinned-conversation-ids"

_SEARCH_WIDTH_JS = """
() => {
  const el = document.querySelector('input[placeholder="Search sessions"]');
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
    expect(page.get_by_placeholder("Search sessions")).to_be_visible(timeout=30_000)
    expanded_width = page.evaluate(_SEARCH_WIDTH_JS)
    assert expanded_width > 100, f"sidebar unexpectedly narrow at start ({expanded_width}px)"

    # Collapse: the rail shrinks to icon width (input stays mounted).
    page.keyboard.press("Control+Alt+BracketLeft")
    page.wait_for_function(f"() => ({_SEARCH_WIDTH_JS})() < 80", timeout=10_000)
    # Expand again.
    page.keyboard.press("Control+Alt+BracketLeft")
    page.wait_for_function(f"() => ({_SEARCH_WIDTH_JS})() > 100", timeout=10_000)
