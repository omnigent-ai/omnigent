"""E2E: touch swipe actions on sidebar session rows (phone viewport).

``Sidebar.rowActions.test.tsx`` covers the ``useRowSwipe`` gesture logic
in jsdom, but jsdom has no real touch surface and no Tailwind ``md:``
media queries — so it can't prove the gesture actually arms on a phone,
where it is gated to ``useIsMobileViewport`` and a ``pointerType ===
"touch"`` primary pointer. These tests run a real browser with a
touchscreen at a 390px viewport and drive a horizontal swipe with CDP
touch events, asserting the two configured directions:

  - swipe-left → archive (the default) flips the server ``archived`` flag;
  - swipe-right → delete opens the same confirm dialog the kebab uses
    (no immediate delete), which then cancels cleanly.

A committed swipe crosses the 72px threshold with ``touch-action: pan-y``
reserving the horizontal axis, so this also exercises that the browser
doesn't hand the gesture off to native scroll mid-swipe.

Both tests park the page on session A and swipe a *different* session's
row (B), then assert the URL never moved to B — so a regressed
trailing-click suppression (the row is a ``<Link>``) surfaces as a real,
visible navigation rather than a same-route no-op.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

# iPhone-12-class portrait viewport — below the Tailwind ``md`` breakpoint
# (768px) so ``useIsMobileViewport`` reports mobile and the swipe arms.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Give the context a real touchscreen and a phone viewport.

    The swipe is touch-only (``pointerType === "touch"``) and mobile-gated,
    so the default desktop context (no touch) would never arm it.
    """
    return {
        **browser_context_args,
        "has_touch": True,
        "viewport": _MOBILE_VIEWPORT,
    }


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title via ``PATCH /v1/sessions/{id}``."""
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()


def _reveal_row(page: Page, title: str) -> Locator:
    """Open the phone sidebar overlay and return the row once it's fully on-screen.

    On a phone the sidebar starts as a closed overlay; the chat header's
    "Open sidebar" button slides it in. The drawer animates, so wait until the
    row's left edge clears x=0 — a swipe measured mid-slide would land off the
    row (and the browser would ``pointercancel`` the gesture).
    """
    page.get_by_role("button", name="Open sidebar").click()
    row = page.get_by_role("link", name=title, exact=True)
    expect(row).to_be_visible()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        box = row.bounding_box()
        if box is not None and box["x"] >= 0:
            return row
        page.wait_for_timeout(50)
    raise AssertionError("sidebar drawer did not settle on-screen")


def _swipe_row(page: Page, row: Locator, dx: float) -> None:
    """Drive a horizontal touch swipe of ``dx`` px across ``row``'s centre.

    Uses CDP ``Input.dispatchTouchEvent`` so Chromium synthesizes a *trusted*
    primary touch pointer. The gesture calls ``setPointerCapture`` mid-swipe,
    which the browser only honours for a real active pointer — a Playwright
    ``dispatch_event`` (untrusted, no active pointer) would throw instead.
    """
    box = row.bounding_box()
    assert box is not None, "row must be visible to swipe"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    sign = 1 if dx > 0 else -1
    cdp = page.context.new_cdp_session(page)

    def touch(kind: str, x: float) -> None:
        # A stable ``id`` ties start→move→end into one active pointer, so
        # Chromium fires correlated primary-touch pointer events (and honours
        # setPointerCapture). A brief settle lets React commit each move's dx
        # before the next event / release reads it.
        points = [] if kind == "touchEnd" else [{"x": x, "y": cy, "id": 0}]
        cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})
        page.wait_for_timeout(20)

    try:
        cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
        touch("touchStart", cx)
        # First move locks the axis (past the 12px activation); the rest carry
        # it past the 72px commit threshold.
        touch("touchMove", cx + sign * 24)
        touch("touchMove", cx + dx * 0.6)
        touch("touchMove", cx + dx)
        touch("touchEnd", cx + dx)
    finally:
        cdp.detach()


def test_swipe_left_archives_session(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Swiping a row left runs the archive path and durably archives the session.

    The default direction map (swipe-left → archive) drives ``runArchive``
    (stop→archive), the same handler the kebab's Archive item uses. Parked on
    session A, swipe session B's row: the server ``archived`` flag on B must
    flip, and the page must stay on A (no trailing-click navigation into B).
    """
    base_url, session_a, session_b = seeded_session_pair
    title_b = f"e2e-swipe-archive-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_b, title_b)

    page.goto(f"{base_url}/c/{session_a}")
    url_before = page.url
    row = _reveal_row(page, title_b)

    _swipe_row(page, row, -90)

    # Trailing-click suppression: the committed swipe must not let session B's
    # <Link> navigate. Give a stray click time to land, then assert we're still
    # on session A's route.
    page.wait_for_timeout(200)
    assert page.url == url_before, "swipe must not navigate into the swiped row"

    deadline = time.monotonic() + 15.0
    archived = False
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_b}", timeout=10.0)
        if resp.status_code == 200 and resp.json().get("archived") is True:
            archived = True
            break
        time.sleep(0.5)
    assert archived, "swipe-left should archive the session on the server"

    # Restore so the shared session-scoped server isn't left with a hidden row.
    httpx.patch(
        f"{base_url}/v1/sessions/{session_b}",
        json={"archived": False},
        timeout=10.0,
    ).raise_for_status()


def test_swipe_right_opens_delete_confirm_and_cancels(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Swiping right (mapped to delete) opens the confirm dialog; Cancel deletes nothing.

    Swipe→delete never deletes immediately — it opens the same dialog the kebab
    uses. Parked on session A, swipe session B's row: the dialog opens, the page
    stays on A (no trailing-click navigation into B), and cancelling leaves B in
    place on the server.
    """
    base_url, session_a, session_b = seeded_session_pair
    title_b = f"e2e-swipe-delete-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_b, title_b)

    # Map swipe-right → delete for this device before the app boots and reads it.
    page.add_init_script(
        "window.localStorage.setItem('omnigent:swipe-actions',"
        " JSON.stringify({left: 'archive', right: 'delete'}))"
    )
    page.goto(f"{base_url}/c/{session_a}")
    url_before = page.url
    row = _reveal_row(page, title_b)

    _swipe_row(page, row, 90)

    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Delete conversation?")
    # Neither the swipe's trailing click nor opening the dialog navigated away.
    assert page.url == url_before, "swipe must not navigate into the swiped row"
    # The dialog gates the delete — nothing is removed while it's open.
    assert httpx.get(f"{base_url}/v1/sessions/{session_b}", timeout=10.0).status_code == 200

    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    expect(row).to_be_visible()
    # Still on session A, and B is still present on the server after cancelling.
    assert page.url == url_before
    assert httpx.get(f"{base_url}/v1/sessions/{session_b}", timeout=10.0).status_code == 200
