"""E2E: clicking the sidebar toggle must open the sidebar even when the
hover-preview (peek card) has appeared under the pointer.

With the Conversations sidebar collapsed, dwelling on the
chat header's "Open sidebar" toggle for 400ms arms dwell-to-peek
(``web/src/shell/ChatHeader.tsx``), which mounts the sidebar as a floating
peek card (``web/src/shell/Sidebar.tsx``). If that card covers the toggle
the pointer is resting on, the card surface at the toggle's coordinates is
the sidebar's brand/home link, and a click aimed at the toggle hits the
brand link instead: the app navigates to ``/`` (losing the open session)
and the sidebar is never pinned open — once the pointer wanders off, the
peek card self-dismisses back to collapsed. The card must therefore float
clear of the chat header, leaving the toggle exposed and clickable.

The user journey this encodes (from the report: clicking the toggle "before
the hover-preview appears" fails intermittently — the preview has actually
just mounted and is still fading in, so the click lands on it):

1. open a session with the sidebar collapsed
2. move the pointer onto the top-left sidebar toggle
3. the hover-preview (peek card) appears under the pointer
4. click — aiming at the toggle
5. expected: the sidebar pins open and the session stays put;
   actual: the app navigates to ``/`` and the sidebar ends collapsed

The click is a raw ``page.mouse`` press at the toggle's coordinates — the
user's physical gesture — not ``locator.click()``, whose actionability checks
would refuse to press a covered button and mask the bug. Waiting for the
``is-peek`` class before pressing makes the race deterministic: any click
landing after the card mounts reproduces the failure every time.

Pure client-side layout/pointer behaviour, so no LLM turn is needed (and none
of the nightly/real-agent markers the approval suites carry). Desktop-only:
peek is a desktop hover affordance (mobile taps never arm it).
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

_CONVERSATIONS = 'aside[aria-label="Conversations"]'
_LEFT_CHORD = "Control+Alt+BracketLeft"
_PEEK_CLASS = re.compile(r"(^|\s)is-peek(\s|$)")
# The collapsed-state toggle in the chat header — outside the macOS Electron
# shell this is the ONLY way to reopen a collapsed sidebar with the pointer.
_TOGGLE = "header button.chat-header-sidebar-toggle"


def test_sidebar_toggle_opens_after_peek_appears(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A click on the sidebar toggle pins the sidebar open even mid-peek."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # Session page settled: the composer is the stable ready signal (never
    # wait for networkidle here — the page keeps an SSE stream open).
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)

    conversations = page.locator(_CONVERSATIONS)
    # The sidebar defaults open on the desktop viewport; collapse it with the
    # standard hotkey so the header toggle appears.
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    page.keyboard.press(_LEFT_CHORD)
    expect(conversations).to_have_attribute("data-collapsed", "true")

    toggle = page.locator(_TOGGLE)
    expect(toggle).to_be_visible()
    box = toggle.bounding_box()
    assert box is not None, "sidebar toggle has no layout box"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2

    # Step 2-3 of the journey: rest the pointer on the toggle until the
    # hover-preview mounts under it. Waiting on the is-peek class (rather
    # than sleeping 400ms) pins the race deterministically at its earliest
    # failing point — the card has mounted and is still fading in.
    page.mouse.move(cx, cy)
    expect(conversations).to_have_class(_PEEK_CLASS, timeout=5_000)

    # Step 4: the user clicks the toggle they aimed at.
    page.mouse.down()
    page.mouse.up()

    # Step 5 (expected behaviour): the click must act as the sidebar toggle —
    # staying on the session, never hijacked into a navigation by whatever
    # the peek card slid under the pointer.
    expect(page).to_have_url(f"{base_url}/c/{session_id}")

    # ... and the sidebar must end up genuinely pinned open (docked, not a
    # transient peek that self-dismisses when the pointer wanders off).
    expect(conversations).not_to_have_class(_PEEK_CLASS)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    page.mouse.move(640, 420)
    page.wait_for_timeout(500)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
