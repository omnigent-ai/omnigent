"""The session-drag preview must stay glued to the cursor.

The sidebar ``<aside>`` always carries a Tailwind ``translate`` utility
(``translate-x-0`` / ``-translate-x-full``), and any non-``none`` CSS
``translate`` makes it the containing block for ``position: fixed``
descendants. The dnd-kit ``<DragOverlay>`` renders inline inside the aside
(not portaled to ``<body>``), so the overlay's fixed viewport coordinates
resolve against the aside's box instead of the viewport. Docked, the aside
sits at (0, 0) and the error is invisible; while the sidebar PEEKS (floating
card at ``inset-2``) every drag renders the preview offset from the pointer
by the card's offset — the "session drag jumps away from the cursor" report.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

# A visible dot glued to the real pointer so recorded failure footage shows
# the preview drifting away from the cursor. Inert for assertions.
_CURSOR_DOT_JS = """
() => {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;z-index:2147483647;width:14px;height:14px;'
    + 'border-radius:50%;background:rgba(255,45,85,.9);border:2px solid white;'
    + 'pointer-events:none;transform:translate(-50%,-50%);left:-100px;top:-100px;'
    + 'box-shadow:0 0 6px rgba(0,0,0,.5)';
  document.body.appendChild(d);
  window.addEventListener('pointermove', (e) => {
    d.style.left = e.clientX + 'px';
    d.style.top = e.clientY + 'px';
  }, true);
}
"""


def _seed_titled_session(base_url: str, title: str) -> str:
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0
    ).raise_for_status()
    return session_id


def _drag_and_measure_jump(page: Page, title: str) -> tuple[float, float]:
    """Drag the row ``title`` and return the overlay's offset from the pointer.

    Returns (dx, dy) between where the drag preview actually renders and where
    a preview glued to the cursor would sit (the row's rect at grab time plus
    the pointer's travel). A correct drag returns (0, 0).
    """
    row = page.get_by_role("link", name=title, exact=True)
    expect(row).to_be_visible()
    box = row.bounding_box()
    assert box is not None
    grab_x, grab_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(grab_x, grab_y)
    page.mouse.down()
    # Pass the 5px MouseSensor activation distance, then travel.
    page.mouse.move(grab_x + 4, grab_y + 4)
    page.mouse.move(grab_x + 12, grab_y + 12)
    target_x, target_y = grab_x + 60, grab_y + 80
    page.mouse.move(target_x, target_y, steps=10)
    page.wait_for_timeout(400)

    overlay = page.locator('div[class*="max-w-[16rem]"]').filter(has_text=title)
    expect(overlay.first).to_be_visible()
    overlay_box = overlay.first.bounding_box()
    assert overlay_box is not None

    # Wander while holding so the (mis)alignment is obvious in recordings.
    page.mouse.move(target_x + 40, target_y - 30, steps=10)
    page.mouse.move(target_x, target_y, steps=10)
    page.wait_for_timeout(600)

    # Cancel: measurement must not file the session anywhere.
    page.keyboard.press("Escape")
    page.mouse.up()
    page.wait_for_timeout(200)

    expected_x = box["x"] + (target_x - grab_x)
    expected_y = box["y"] + (target_y - grab_y)
    return overlay_box["x"] - expected_x, overlay_box["y"] - expected_y


def _seed_rows(base_url: str, count: int = 8) -> tuple[list[str], list[str]]:
    run = uuid.uuid4().hex[:6]
    titles = [f"drag-{run}-{i:02d}" for i in range(count)]
    return titles, [_seed_titled_session(base_url, t) for t in titles]


def _cleanup(base_url: str, session_ids: list[str]) -> None:
    for session_id in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def test_peek_drag_overlay_tracks_pointer(page: Page, seeded_session: tuple[str, str]) -> None:
    """Dragging a session inside the PEEKING sidebar keeps the preview on the cursor."""
    base_url, session_id = seeded_session
    titles, sids = _seed_rows(base_url)
    try:
        page.set_viewport_size({"width": 1280, "height": 720})
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_role("link", name=titles[-1], exact=True)).to_be_visible(timeout=15000)
        page.evaluate(_CURSOR_DOT_JS)

        # Collapse the sidebar (⌘⌥[ / Ctrl+Alt+[), then dwell on the chat
        # header's "Open sidebar" toggle past the 400ms peek delay.
        page.keyboard.press("Control+Alt+BracketLeft")
        toggle = page.get_by_role("button", name="Open sidebar")
        expect(toggle).to_be_visible()
        toggle_box = toggle.bounding_box()
        assert toggle_box is not None
        page.mouse.move(
            toggle_box["x"] + toggle_box["width"] / 2,
            toggle_box["y"] + toggle_box["height"] / 2,
        )
        page.wait_for_timeout(700)
        aside = page.locator("aside.conversations-sidebar")
        expect(aside).to_have_class(re.compile(r"is-peek"))

        dx, dy = _drag_and_measure_jump(page, titles[0])
        assert abs(dx) <= 2 and abs(dy) <= 2, (
            f"drag preview jumped ({dx:.1f}, {dy:.1f})px away from the cursor while "
            "the sidebar was peeking (dnd-kit DragOverlay anchored to the translated "
            "aside instead of the viewport)"
        )
    finally:
        _cleanup(base_url, sids)


def test_docked_drag_overlay_tracks_pointer(page: Page, seeded_session: tuple[str, str]) -> None:
    """Baseline: the docked-sidebar drag already tracks the cursor and must stay so."""
    base_url, session_id = seeded_session
    titles, sids = _seed_rows(base_url)
    try:
        page.set_viewport_size({"width": 1280, "height": 720})
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_role("link", name=titles[-1], exact=True)).to_be_visible(timeout=15000)
        page.evaluate(_CURSOR_DOT_JS)

        dx, dy = _drag_and_measure_jump(page, titles[0])
        assert abs(dx) <= 2 and abs(dy) <= 2, (
            f"drag preview jumped ({dx:.1f}, {dy:.1f})px away from the cursor in the "
            "docked sidebar"
        )
    finally:
        _cleanup(base_url, sids)
