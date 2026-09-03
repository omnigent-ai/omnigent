"""An outside tap must dismiss the mobile header overflow menu.

On an iOS-like touch viewport, opening the floating top-right three-dot
overflow menu (``HeaderConversationMenu``) and tapping outside it can leave
the menu open: it either never begins to close, or closes momentarily and
immediately reopens. The user cannot return to the session without leaving
or reloading the view. A single outside tap should dismiss the menu and
leave it closed until the trigger is tapped again.

The iOS app is a thin native shell over this same server-served SPA, so the
journey is driven on the web lane at a phone-sized touch context (390x664,
``has_touch``, ``is_mobile``). While the menu is open, Radix's modal mode
sets ``pointer-events: none`` on ``body`` (outside hits resolve to ``html``);
empirically the non-dismissable zone sits left of the opened menu popper
(which spans roughly x=157..381, y=52..511 at this viewport). The tap point
below was verified live to leave the menu open on the buggy build, while a
correct build dismisses the menu on that same tap.

The session is seeded with plan todos so the chat surface matches the
reproduction conditions (the plan tracker pinned at the top of the thread,
as in the reported session).
"""

from __future__ import annotations

import os

import httpx
from playwright.sync_api import Browser, expect

_MENU = '[data-slot="dropdown-menu-content"]'
_KEBAB = '[data-testid="header-conversation-actions"]'

# iPhone-13-like phone profile: below Tailwind's md breakpoint (768px) so the
# mobile floating header renders, with touch so ``tap()`` drives real touches.
_VIEWPORT = {"width": 390, "height": 664}

# Verified-live dead zone: outside the opened menu popper (left of its
# x=157 edge), over the conversation surface. On the buggy build a tap here
# leaves the menu open; on a correct build it dismisses the menu.
_OUTSIDE_TAP = (150, 200)

_PLAN = [
    {"content": "Read the code", "status": "completed", "activeForm": "Reading the code"},
    {"content": "Write the tracker", "status": "in_progress", "activeForm": "Writing"},
    {"content": "Ship it", "status": "pending", "activeForm": "Shipping it"},
]


def _publish_todos(base_url: str, session_id: str, todos: list[dict[str, str]]) -> None:
    """Publish session todos so the plan tracker renders in the chat thread."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_todos", "data": {"todos": todos}},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_outside_tap_dismisses_overflow_menu(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A single outside tap closes the header overflow menu and it stays closed.

    Failure mode this catches: after opening the top-right three-dot menu on
    a touch device, an outside tap leaves the menu open — it never dismisses,
    or it closes momentarily and immediately reopens.

    :param browser: Playwright browser to open a touch-enabled mobile context on.
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _publish_todos(base_url, session_id, _PLAN)

    ctx_kwargs: dict = {
        "viewport": _VIEWPORT,
        "has_touch": True,
        "is_mobile": True,
    }
    # Film the journey when the recording harness asks for it. The autouse
    # _record_video fixture only patches the async API, and this test drives
    # the sync API through its own context, so honor the env var directly.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir

    context = browser.new_context(**ctx_kwargs)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        # The seeded plan renders the tracker pinned at the top of the thread —
        # the same chat-surface state as the reported session.
        expect(page.locator('[data-testid="plan-tracker"]')).to_be_visible(timeout=30_000)
        kebab = page.locator(_KEBAB)
        expect(kebab).to_be_visible()
        page.wait_for_timeout(500)

        menu = page.locator(_MENU)
        kebab.tap()
        expect(menu).to_be_visible(timeout=5_000)
        # Let the open animation and focus handoff settle before dismissing.
        page.wait_for_timeout(400)

        x, y = _OUTSIDE_TAP
        page.touchscreen.tap(x, y)

        # A single outside tap must dismiss the menu...
        expect(menu).to_be_hidden(timeout=3_000)
        # ...and it must STAY dismissed — the bug reopens it immediately after
        # a momentary close, so re-assert after the reopen window has passed.
        page.wait_for_timeout(1_000)
        expect(menu).to_be_hidden()
    finally:
        context.close()
