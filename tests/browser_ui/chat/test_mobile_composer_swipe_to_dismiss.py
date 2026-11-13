"""Swiping down drops composer focus, which closes the phone keyboard.

Chromium has no soft keyboard, so composer focus stands in for it."""

from __future__ import annotations

import pytest
from playwright.sync_api import CDPSession, Locator, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract

_CARD = "[data-composer-card]"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "has_touch": True,
        "is_mobile": True,
    }


def _swipe(cdp: CDPSession, page: Page, *, x: float, y: float, dx: float = 0, dy: float) -> None:
    """Drag one finger by (dx, dy) over 12 moves, then lift."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for step in range(1, 13):
        point = {"x": x + dx * step / 12, "y": y + dy * step / 12}
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
        page.wait_for_timeout(16)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _focus(composer: Locator) -> None:
    composer.tap()
    expect(composer).to_be_focused()
    expect(composer.locator("xpath=ancestor::*[@data-composer-card]")).to_have_attribute(
        "data-mobile-composer", "expanded"
    )


def test_swipe_down_dismisses_new_session_composer(page: Page, chat_session_contract) -> None:
    """CUJ-1: on the new-session screen a downward swipe blurs the composer."""
    chat: ChatSessionContract = chat_session_contract
    # The landing's workspace picker lists the host's folders.
    chat.contract.json(f"/v1/hosts/{chat.host_id}/filesystem", {"path": "/", "entries": []})
    page.goto(f"{chat.base_url}/")
    composer = page.get_by_test_id("new-chat-landing-input")
    expect(composer).to_be_visible()
    cdp = page.context.new_cdp_session(page)
    card = page.locator(_CARD).filter(has=composer)

    _focus(composer)
    # Upward and leftward drags keep the keyboard up (a rightward one opens the sidebar).
    _swipe(cdp, page, x=200, y=420, dy=-160)
    expect(composer).to_be_focused()
    _swipe(cdp, page, x=300, y=300, dx=-160, dy=30)
    expect(composer).to_be_focused()

    _swipe(cdp, page, x=200, y=260, dy=160)
    expect(composer).not_to_be_focused()
    expect(card).to_have_attribute("data-mobile-composer", "collapsed")

    # A typed draft survives the dismissal.
    _focus(composer)
    composer.press_sequentially("Fix the flaky login test")
    _swipe(cdp, page, x=200, y=260, dy=160)
    expect(composer).not_to_be_focused()
    expect(composer).to_have_value("Fix the flaky login test")


def test_swipe_down_dismisses_session_composer(page: Page, chat_session_contract) -> None:
    """CUJ-2: in a session, a downward swipe over the transcript blurs the composer."""
    chat: ChatSessionContract = chat_session_contract
    chat.seed_transcript(6)
    page.goto(chat.url)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    cdp = page.context.new_cdp_session(page)

    _focus(composer)
    _swipe(cdp, page, x=200, y=300, dy=200)
    expect(composer).not_to_be_focused()
    expect(page.locator(_CARD).filter(has=composer)).to_have_attribute(
        "data-mobile-composer", "collapsed"
    )
    expect(page.locator('aside[aria-label="Conversations"]')).to_have_attribute(
        "data-collapsed", "true"
    )
