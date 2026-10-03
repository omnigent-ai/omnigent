"""E2E: resizing the terminals-panel list column by pointer and keyboard.

The full-screen Shells panel (``TerminalsPanel``) splits into a terminal
list column and an xterm pane on desktop, divided by a draggable handle
(``useResizableColumn``). The handle is a pointer-events separator: it
captures the pointer on pointerdown (so drags keep tracking off the thin
strip), carries ``touch-action: none`` plus an invisible widened hit
target for touch, and is a focusable ARIA separator resizable with
ArrowLeft/ArrowRight under the same 100–480px clamps as dragging.

The panel's only UI entry point is the mobile Shells drawer (desktop
opens shells as rail tabs instead), while the column split needs a
desktop viewport — so the test opens the panel at a phone viewport and
then widens the window, which is also a real tablet-rotation scenario
the handle must survive.

Terminals are launched over REST (the same runner path as
``sys_terminal_launch``) so no LLM turn is involved and the flow is
deterministic.
"""

from __future__ import annotations

import re
import time

import httpx
from playwright.sync_api import Page, ViewportSize, expect

# Below Tailwind's ``md`` (768px) so the mobile header menu and drawer render.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}
# Comfortably above ``md`` so the panel splits into list + xterm columns.
_DESKTOP_VIEWPORT: ViewportSize = {"width": 1400, "height": 900}

# useResizableColumn defaults: initial 176, clamps [100, 480], 20px/keypress.
_DEFAULT_WIDTH = 176
_KEY_STEP = 20
_MAX_WIDTH = 480
_DRAG_TARGET_WIDTH = 256


def _launch_terminal(base_url: str, session_id: str, session_key: str) -> None:
    """Launch an agent-declared ``zsh`` terminal via REST (no LLM turn)."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        json={"terminal": "zsh", "session_key": session_key},
        timeout=60.0,
    )
    resp.raise_for_status()


def _force_chat_first(base_url: str, session_id: str, timeout_s: float = 15.0) -> None:
    """Pin the session to the chat-first presentation before the page loads.

    When the runner starts hosting an SDK session it auto-creates an
    embedded Omnigent REPL terminal and stamps ``omnigent.ui: terminal``
    on the session. A session carrying that label renders terminal-first:
    tapping a shell row opens the shell in the MAIN view and the
    full-screen ``TerminalsPanel`` (the surface with the resizable column
    split under test) never mounts — so whether this test's entry path
    works depends on a race between the runner's stamp and the page load.
    Wait briefly for the one-time stamp (the terminal launches already
    forced the runner's session init, which runs the REPL ensure + stamp
    inline, so it lands within seconds when it lands at all), then delete
    the label (empty value clears it; the runner never re-stamps — the
    REPL-terminal ensure is guarded) so the entry path is
    deterministically chat-first.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/labels", timeout=10.0)
        resp.raise_for_status()
        if resp.json().get("labels", {}).get("omnigent.ui") == "terminal":
            break
        time.sleep(0.5)
    # Delete regardless: if the stamp never landed (REPL creation failed,
    # a logged warning path that also never retries), the label is simply
    # absent and the delete is a no-op — either way chat-first from here.
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": ""}},
        timeout=10.0,
    )
    resp.raise_for_status()
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/labels", timeout=10.0)
    resp.raise_for_status()
    assert resp.json().get("labels", {}).get("omnigent.ui") != "terminal"


def _open_terminals_panel_on_desktop(page: Page, base_url: str, session_id: str) -> None:
    """Open the full-screen Shells panel, then widen to a desktop viewport.

    Mobile flow (Conversation actions → Shells drawer → tap the ``main`` shell
    row) is the panel's entry point; widening the viewport then flips the panel
    into its desktop list/xterm split where the column handle renders.
    """
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    actions_menu = page.get_by_role("button", name="Conversation actions")
    expect(actions_menu).to_be_visible(timeout=15_000)
    actions_menu.click()
    # Accessible name includes the shell-count badge (e.g. "Shells 2").
    shells_entry = page.get_by_role("menuitem", name=re.compile(r"^Shells\b"))
    expect(shells_entry).to_be_visible(timeout=10_000)
    shells_entry.click()

    drawer = page.get_by_test_id("shells-panel-drawer")
    expect(drawer).to_have_attribute("data-state", "open")
    row = drawer.get_by_role("button").filter(has_text="main").filter(has_text="zsh")
    expect(row.first).to_be_visible(timeout=30_000)
    row.first.click()

    panel = page.get_by_test_id("terminals-panel")
    expect(panel).to_have_attribute("data-state", "open")

    page.set_viewport_size(_DESKTOP_VIEWPORT)


def _separator(page: Page):
    """The column-resize handle, scoped to the terminals panel."""
    return page.get_by_test_id("terminals-panel").get_by_role(
        "separator", name="Resize terminal list"
    )


def _list_panel_width(page: Page) -> float:
    """Measured width of the terminal-list column (the handle's next sibling)."""
    return _separator(page).evaluate("el => el.nextElementSibling.getBoundingClientRect().width")


def test_terminals_column_resizes_by_pointer_and_keyboard(
    page: Page,
    terminal_session: tuple[str, str],
) -> None:
    """Drag, keyboard-resize, and row-tap isolation of the list column.

    One flow (panel setup is the expensive part): drag the handle to a new
    width and verify it sticks after release, step it with arrow keys on
    the focused separator, and confirm interactions with adjacent list
    rows — tapping a row, wheel-scrolling over the list — never resize.
    """
    base_url, session_id = terminal_session
    # Two shells so the negative row-tap check can select a non-active row
    # (tapping the active row toggles the xterm closed, hiding the handle).
    _launch_terminal(base_url, session_id, "main")
    _launch_terminal(base_url, session_id, "aux")
    _force_chat_first(base_url, session_id)

    _open_terminals_panel_on_desktop(page, base_url, session_id)

    handle = _separator(page)
    expect(handle).to_be_visible(timeout=30_000)

    # Touch affordance: competing gestures must not pan/zoom during a drag.
    assert handle.evaluate("el => getComputedStyle(el).touchAction") == "none"
    expect(handle).to_have_attribute("aria-valuenow", str(_DEFAULT_WIDTH))
    assert abs(_list_panel_width(page) - _DEFAULT_WIDTH) < 2

    # --- Pointer drag: press on the handle, pull right, release. The width
    # follows the pointer (measured from the split row's left edge) and the
    # new width persists after the pointer lifts. hover() first: the panel
    # slides in (translate transition), and hover's actionability check waits
    # for the handle's position to stabilize before we read its box.
    handle.hover()
    box = handle.bounding_box()
    assert box is not None, "resize handle should have a bounding box"
    container_left = handle.evaluate("el => el.parentElement.getBoundingClientRect().left")
    start_y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] / 2, start_y)
    page.mouse.down()
    page.mouse.move(container_left + _DRAG_TARGET_WIDTH, start_y, steps=8)
    page.mouse.up()

    expect(handle).to_have_attribute("aria-valuenow", str(_DRAG_TARGET_WIDTH))
    assert abs(_list_panel_width(page) - _DRAG_TARGET_WIDTH) < 2

    # Persists after release: pointer movement without a pressed button
    # must not keep resizing (the drag really ended on pointerup).
    page.mouse.move(container_left + 400, start_y)
    expect(handle).to_have_attribute("aria-valuenow", str(_DRAG_TARGET_WIDTH))

    # --- Keyboard: the handle is a focusable separator; arrow keys step the
    # width by 20px under the same clamps as dragging.
    handle.focus()
    page.keyboard.press("ArrowLeft")
    expect(handle).to_have_attribute("aria-valuenow", str(_DRAG_TARGET_WIDTH - _KEY_STEP))
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    keyboard_width = _DRAG_TARGET_WIDTH + _KEY_STEP
    expect(handle).to_have_attribute("aria-valuenow", str(keyboard_width))
    assert abs(_list_panel_width(page) - keyboard_width) < 2

    # Clamp: enough presses to overshoot maxWidth (480) pin the width there;
    # one more press must not push past it.
    for _ in range((_MAX_WIDTH - keyboard_width) // _KEY_STEP + 2):
        page.keyboard.press("ArrowRight")
    expect(handle).to_have_attribute("aria-valuenow", str(_MAX_WIDTH))
    page.keyboard.press("ArrowRight")
    expect(handle).to_have_attribute("aria-valuenow", str(_MAX_WIDTH))
    assert abs(_list_panel_width(page) - _MAX_WIDTH) < 2
    keyboard_width = _MAX_WIDTH

    # --- Negative: interacting with the list next to the handle must not
    # resize. Tapping the ``aux`` row (its center, well clear of the handle's
    # invisible hit pad at the column boundary) selects that shell...
    panel = page.get_by_test_id("terminals-panel")
    aux_row = panel.get_by_role("button").filter(has_text="aux").filter(has_text="zsh")
    expect(aux_row.first).to_be_visible()
    aux_row.first.click()
    # Anchored: the inactive row carries hover:bg-accent/60, which a bare
    # "bg-accent" substring would also match.
    expect(aux_row.first).to_have_class(re.compile(r"(?:^|\s)bg-accent(?:\s|$)"))

    # ...and a wheel scroll over the list is plain scrolling, not a resize.
    row_box = aux_row.first.bounding_box()
    assert row_box is not None
    page.mouse.move(row_box["x"] + row_box["width"] / 2, row_box["y"] + 5)
    page.mouse.wheel(0, 120)

    expect(handle).to_have_attribute("aria-valuenow", str(keyboard_width))
    assert abs(_list_panel_width(page) - keyboard_width) < 2
