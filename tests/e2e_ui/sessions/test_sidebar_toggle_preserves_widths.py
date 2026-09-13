"""E2E: toggling the Conversations sidebar preserves chat + rail widths.

Covers OMNI-2366 / ``useResizableInlinePanel``: opening the left sidebar must
temporarily shrink the right Workspace rail (never squeezing the center chat
below its 480px minimum), and collapsing the sidebar must restore the rail to
its previous width rather than leaving it shrunk.

The viewport is pinned to 1200px so the rail's default width would, without the
fix, push the chat under 480px once the 320px sidebar opens — making the
sidebar-aware clamp observable end-to-end. Pure client-side layout, so no LLM
turn (and none of the nightly/real-agent markers the approval suites carry).
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_CONVERSATIONS = 'aside[aria-label="Conversations"]'
_LEFT_CHORD = "Control+Alt+BracketLeft"
_CHAT_MIN_PX = 480
_CHAT_HARD_MIN_PX = 240


def _rail_width(page: Page) -> float:
    box = page.get_by_role("complementary", name="Workspace").bounding_box()
    assert box is not None
    return box["width"]


def _chat_width(page: Page) -> float:
    """Width of the center chat surface, excluding the resize gutter."""
    chat = page.locator("main").bounding_box()
    assert chat is not None
    return chat["width"]


def test_sidebar_toggle_preserves_widths(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Opening the sidebar shrinks the rail (not the chat); collapsing restores it."""
    base_url, session_id = seeded_session
    # 1200px: default rail (~432px) + 320px sidebar would leave the chat under
    # 480px if the rail didn't yield, so the clamp is exercised.
    page.set_viewport_size({"width": 1200, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")

    conversations = page.locator(_CONVERSATIONS)
    workspace = page.get_by_role("complementary", name="Workspace")
    expect(workspace).to_be_visible(timeout=30_000)

    # Sidebar defaults open on the desktop viewport. Chat must already sit at
    # or above its minimum, with the rail shrunk to make room.
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    rail_open = _rail_width(page)
    assert _chat_width(page) >= _CHAT_MIN_PX - 1, _chat_width(page)

    # Collapse the sidebar (⌘⌥[ / Ctrl+Alt+[): the rail springs back wider now
    # that the reserved sidebar space is freed.
    page.keyboard.press(_LEFT_CHORD)
    expect(conversations).to_have_attribute("data-collapsed", "true")
    rail_collapsed = _rail_width(page)
    assert rail_collapsed > rail_open, (rail_collapsed, rail_open)

    # Reopen the sidebar: the rail shrinks back to exactly its earlier width —
    # the preferred width was preserved through the squeeze, not overwritten.
    page.keyboard.press(_LEFT_CHORD)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    assert abs(_rail_width(page) - rail_open) < 2, (_rail_width(page), rail_open)
    assert _chat_width(page) >= _CHAT_MIN_PX - 1, _chat_width(page)


def test_shrinking_viewport_keeps_chat_minimum_with_sidebar_open(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Shrinking the window keeps the chat above its tablet hard floor.

    The rail must recompute as the viewport shrinks. Once the row reaches tablet
    width, the chat may cede below its 480px comfort minimum so the rail retains
    drag travel, but it must never cross the hard minimum.
    """
    base_url, session_id = seeded_session
    # Start wide so the rail's default is well above the shrunk ceiling.
    page.set_viewport_size({"width": 1600, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")

    conversations = page.locator(_CONVERSATIONS)
    workspace = page.get_by_role("complementary", name="Workspace")
    expect(workspace).to_be_visible(timeout=30_000)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    assert _chat_width(page) >= _CHAT_MIN_PX - 1, _chat_width(page)

    # At this tablet width the rail clamp may cede below the chat's comfort
    # minimum, but it must preserve the hard floor.
    page.set_viewport_size({"width": 928, "height": 800})
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    chat_width = _chat_width(page)
    assert chat_width >= _CHAT_HARD_MIN_PX - 1, chat_width
    assert chat_width < _CHAT_MIN_PX, chat_width
