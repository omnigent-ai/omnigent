"""Browser e2e for the sidebar session drag ghost's position.

Dragging a session row shows a floating preview card (the dnd-kit
``DragOverlay``). The card must render under the mouse at the grab point:
its top-left must equal the dragged row's initial rect translated by the
pointer's movement. Because the overlay is ``position: fixed`` *inside*
``.conversations-sidebar`` — an element whose Tailwind ``translate-x-0``
(computed ``translate: 0px``) makes it the containing block for fixed
descendants — the ghost is displaced by the sidebar's own viewport offset
whenever the sidebar floats:

- docked sidebar (offset 0,0): the ghost tracks the pointer — guarded here
  so a fix can't regress the working case;
- hover-peek floating card (``inset-2`` → offset 8,8): ghost off by 8px
  right/down;
- peek card under the macOS Electron shell (``top: 2.75rem`` → offset
  8,44): the ghost hangs a full row below the cursor, fully detached from
  the pointer.

The mac-shell case drives the real SPA in Chromium with the desktop
bridge flag (``window.omnigentDesktop``) and a Macintosh UA, which is
exactly how ``isMacElectronShell()`` keys the ``data-electron-mac``
styles.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Browser, Page, expect

# The dnd-kit DragOverlay wrapper: the only fixed-position div whose text is
# exactly the dragged session's title (the preview card renders the label).
_OVERLAY_RECT_JS = """(label) => {
  const els = [...document.querySelectorAll('div')].filter(
    (d) => d.style && d.style.position === 'fixed' &&
           d.textContent.trim() === label);
  if (!els.length) return null;
  const r = els[0].getBoundingClientRect();
  return {left: r.left, top: r.top, width: r.width, height: r.height};
}"""

_MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# The ghost's top-left must match the dragged row's rect + pointer delta to
# within a couple of px (sub-pixel layout rounding); the reproduced offsets
# are 8px (browser peek) and 44px (mac-shell peek), far beyond it.
_TOLERANCE_PX = 3.0


def _retitle(base_url: str, session_id: str, title: str) -> None:
    """Give the seeded session a unique *title* the overlay finder can key on."""
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()


def _open_peek(page: Page, collapse_chord: str = "Control+Alt+BracketLeft") -> None:
    """Collapse the docked sidebar, then dwell-hover the header toggle until
    the floating peek card is open.

    The sidebar-toggle hotkey is platform-exact (⌘⌥[ on macOS, Ctrl+Alt+[
    elsewhere), so a test simulating the mac shell must pass the Meta chord.
    """
    page.keyboard.press(collapse_chord)
    page.wait_for_timeout(400)
    toggle = page.get_by_role("button", name="Open sidebar")
    expect(toggle.first).to_be_visible()
    toggle.first.hover()
    page.wait_for_timeout(900)
    is_peek = page.evaluate(
        "() => document.querySelector('.conversations-sidebar')"
        "?.classList.contains('is-peek') ?? false"
    )
    assert is_peek, "hover-peek sidebar card did not open"


def _drag_row_and_measure_ghost(page: Page, session_id: str, title: str) -> dict[str, float]:
    """Mouse-drag the session's sidebar row and measure the drag ghost.

    Grabs the row, moves the pointer 120px right and 150px down (staying
    inside the sidebar), and returns the ghost's positional error: actual
    overlay rect minus the expected rect (row's initial rect + pointer
    delta). Releases the mouse over nothing droppable, so the drop is a
    no-op and the session stays where it was.
    """
    row = page.locator(".conversations-sidebar li").filter(
        has=page.locator(f'a[href="/c/{session_id}"]')
    )
    expect(row.first).to_be_visible()
    box = row.first.bounding_box()
    assert box is not None
    grab = (box["x"] + min(40.0, box["width"] / 2), box["y"] + box["height"] / 2)
    page.mouse.move(*grab)
    page.mouse.down()
    # Cross the MouseSensor's 5px activation distance so the drag starts.
    page.mouse.move(grab[0] + 10, grab[1], steps=3)
    page.wait_for_timeout(150)
    # Slow, watchable glide so the ghost's placement is legible on video.
    pointer = (grab[0] + 120, grab[1] + 150)
    page.mouse.move(*pointer, steps=30)
    page.wait_for_timeout(400)
    overlay = page.evaluate(_OVERLAY_RECT_JS, title)
    assert overlay is not None, "drag overlay (session preview card) never appeared"
    expected_left = box["x"] + (pointer[0] - grab[0])
    expected_top = box["y"] + (pointer[1] - grab[1])
    errors = {
        "left_error": overlay["left"] - expected_left,
        "top_error": overlay["top"] - expected_top,
        "pointer_x": pointer[0],
        "pointer_y": pointer[1],
        "overlay_left": overlay["left"],
        "overlay_top": overlay["top"],
        "overlay_width": overlay["width"],
        "overlay_height": overlay["height"],
    }
    # Hold the drag a beat so a recording shows the ghost's resting offset.
    page.wait_for_timeout(600)
    page.mouse.up()
    return errors


def _assert_ghost_under_pointer(errors: dict[str, float], where: str) -> None:
    assert (
        abs(errors["left_error"]) <= _TOLERANCE_PX and abs(errors["top_error"]) <= _TOLERANCE_PX
    ), (
        f"drag ghost is offset from the grab point in the {where}: "
        f"left_error={errors['left_error']:.1f}px "
        f"top_error={errors['top_error']:.1f}px (tolerance {_TOLERANCE_PX}px)"
    )


def test_docked_drag_ghost_tracks_pointer(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Docked sidebar: the drag ghost stays under the mouse (working case)."""
    base_url, session_id = seeded_session
    title = f"dragpos-docked-{uuid.uuid4().hex[:6]}"
    _retitle(base_url, session_id, title)
    page.goto(f"{base_url}/c/{session_id}")
    errors = _drag_row_and_measure_ghost(page, session_id, title)
    _assert_ghost_under_pointer(errors, "docked sidebar")


def test_peek_drag_ghost_tracks_pointer(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Hover-peek floating card: the drag ghost must still track the mouse.

    Guards the regression where the ghost rendered offset down-right by
    exactly the peek card's viewport offset (inset-2 → 8px, 8px) because the
    fixed overlay's containing block was the translated sidebar, not the
    viewport.
    """
    base_url, session_id = seeded_session
    title = f"dragpos-peek-{uuid.uuid4().hex[:6]}"
    _retitle(base_url, session_id, title)
    page.goto(f"{base_url}/c/{session_id}")
    _open_peek(page)
    errors = _drag_row_and_measure_ghost(page, session_id, title)
    _assert_ghost_under_pointer(errors, "peek sidebar card")


def test_peek_drag_ghost_tracks_pointer_on_mac_desktop_shell(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """macOS desktop shell peek card: the ghost must not detach from the mouse.

    Under ``data-electron-mac`` the peek card floats at ``top: 2.75rem``, so
    the containing-block bug displaces the ghost by (8, 44)px — the cursor
    ends up entirely outside the dragged card. The shell is simulated the
    way the SPA itself detects it: the ``window.omnigentDesktop`` bridge
    object plus a Macintosh user agent.
    """
    base_url, session_id = seeded_session
    title = f"dragpos-macpeek-{uuid.uuid4().hex[:6]}"
    _retitle(base_url, session_id, title)
    context = browser.new_context(user_agent=_MAC_UA, viewport={"width": 1280, "height": 720})
    try:
        page = context.new_page()
        page.add_init_script("window.omnigentDesktop = { kind: 'electron' };")
        page.goto(f"{base_url}/c/{session_id}")
        assert page.evaluate("() => !!document.querySelector('[data-electron-mac]')"), (
            "simulated desktop shell did not engage data-electron-mac styles"
        )
        _open_peek(page, collapse_chord="Meta+Alt+BracketLeft")
        errors = _drag_row_and_measure_ghost(page, session_id, title)
        # User-facing framing first: the grab point must stay inside the card.
        pointer_inside = (
            errors["overlay_left"]
            <= errors["pointer_x"]
            <= errors["overlay_left"] + errors["overlay_width"]
            and errors["overlay_top"]
            <= errors["pointer_y"]
            <= errors["overlay_top"] + errors["overlay_height"]
        )
        assert pointer_inside, (
            "dragged session card detached from the cursor on the mac desktop "
            f"shell: pointer=({errors['pointer_x']:.0f},{errors['pointer_y']:.0f}) "
            f"card at ({errors['overlay_left']:.0f},{errors['overlay_top']:.0f}) "
            f"{errors['overlay_width']:.0f}x{errors['overlay_height']:.0f} "
            f"(left_error={errors['left_error']:.1f}px, "
            f"top_error={errors['top_error']:.1f}px)"
        )
        _assert_ghost_under_pointer(errors, "mac desktop shell peek card")
    finally:
        context.close()
