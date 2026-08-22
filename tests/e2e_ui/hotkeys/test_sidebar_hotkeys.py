"""UI e2e: global keyboard chords in a real browser (#7).

Covers the hook behavior end to end:

- ``usePinnedSessionHotkeys`` — in the browser, ``Ctrl/Cmd+Alt+<digit>`` jumps
  to the Nth *pinned* session (plain ``Cmd+digit`` is the native tab switch,
  so the browser path owns the Alt chord and matches ``e.code``).
- ``useSidebarToggleHotkeys`` — ``Ctrl/Cmd+Alt+[`` toggles the left sidebar
  (exercising the handler, AltGraph guard included, on a real keydown). The
  sidebar collapses by animating its width to zero rather than unmounting, so
  the assertion is on the sidebar ``aside``'s rendered width, not its
  visibility.
- ``useSessionSwitchHotkey`` — ``Ctrl/Cmd+↑/↓`` switches sessions *while the
  composer holds focus*, which is where users actually sit.
- ``useCommandPaletteHotkey`` — ``Cmd+K`` opens the palette even with a
  terminal focused (xterm drops Cmd chords), while ``Ctrl+K`` stays with the
  PTY, which turns it into ^K.
- ``useSessionSwitchHotkey`` again — the switch chord yields to the open
  command palette, which binds the same chord to its own row navigation.

Focus is the whole point of the last two, so they assert against a real
focused surface rather than a synthetic event target.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail, seed_committed_turn

# The palette's search box — its user-visible handle, stable across the
# dialog's internals.
_PALETTE_INPUT = "Search sessions or run a command"

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


def test_session_switch_chord_fires_while_the_composer_has_focus(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """``Ctrl/Cmd+↓`` switches sessions with the composer focused and typed in.

    The hook used to bail whenever the event target was a ``textarea`` /
    ``input`` / contenteditable, so the chord was dead in the one place users
    spend their time. It carries a modifier, so it can never be mistaken for
    typing — and the draft it leaves behind must survive the switch.

    Both sessions are seeded with a committed turn so the transcript renders
    from the store instead of waiting on the mock LLM.
    """
    base_url, session_a, session_b = seeded_session_pair
    seed_committed_turn(session_a, prompt="ping a", reply="pong a")
    seed_committed_turn(session_b, prompt="ping b", reply="pong b")

    page.goto(f"{base_url}/c/{session_a}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.click()
    composer.type("half-written draft")
    expect(composer).to_be_focused()

    page.keyboard.press("Control+ArrowDown")

    # Lands on another conversation. Asserting "moved off session_a" rather
    # than "landed on session_b" keeps this independent of sidebar ordering,
    # which follows recency and can carry rows from other tests in the shard.
    page.wait_for_url(
        lambda url: bool(re.search(r"/c/[0-9a-f]+$", url)) and session_a not in url,
        timeout=15_000,
    )
    # The draft is per-session, so the composer we land on is a different one:
    # the typed text must not have followed us, nor been sent anywhere.
    expect(page.get_by_label("Message the agent")).to_have_value("")

    # Going back restores the draft — proof the chord navigated rather than
    # clobbering composer state.
    page.keyboard.press("Control+ArrowUp")
    page.wait_for_url(f"**/c/{session_a}", timeout=15_000)
    expect(page.get_by_label("Message the agent")).to_have_value("half-written draft")


def test_command_palette_chord_with_a_terminal_focused(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """``Cmd+K`` opens the palette from a focused terminal; ``Ctrl+K`` does not.

    The hook used to yield ⌘K to any focused ``.xterm``. But xterm only
    forwards the *control* variant — it writes ``\x0b`` (^K, kill-to-end-of-
    line) to the PTY — and drops Cmd chords on macOS entirely, so yielding ⌘K
    left it dead: no palette, and nothing delivered to the shell either.
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


def test_session_switch_chord_yields_to_the_open_command_palette(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """With the palette open, ``Ctrl+↑/↓`` moves its highlight and nothing else.

    ``useSessionSwitchHotkey`` yields on ``defaultPrevented`` rather than on
    "focus is in a text field", so the guard only holds while cmdk really does
    claim this chord (it binds Cmd/Ctrl+↑/↓ to jump to the first/last row).
    Asserting the highlight *moved* is what pins that: a cmdk upgrade that
    stopped binding these chords would fail here rather than silently start
    navigating the app behind the open palette.
    """
    base_url, session_a, _session_b = seeded_session_pair
    seed_committed_turn(session_a, prompt="ping a", reply="pong a")

    page.goto(f"{base_url}/c/{session_a}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)

    page.keyboard.press("Control+k")
    palette_input = page.get_by_placeholder(_PALETTE_INPUT)
    expect(palette_input).to_be_visible(timeout=10_000)
    # Wait for a highlighted row: cmdk selects one as soon as the list renders,
    # and the chord below has nothing to move until it does.
    selected = page.locator("[cmdk-item][data-selected=true]")
    expect(selected).to_have_count(1, timeout=10_000)
    before_row = selected.inner_text()

    page.keyboard.press("Control+ArrowDown")

    # cmdk consumed it: the highlight moved to the last row...
    expect(page.locator("[cmdk-item][data-selected=true]")).not_to_have_text(
        before_row, timeout=10_000
    )
    # ...and the app did NOT navigate behind the still-open palette.
    expect(palette_input).to_be_visible()
    assert page.url.endswith(f"/c/{session_a}"), f"route moved behind the palette: {page.url}"
