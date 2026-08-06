"""Browser e2e for the project-folder header's context menu.

Project folder headers used to expose their actions only through a
hover-revealed kebab: a right-click fell through to the browser's native menu,
and touch — which has no right-click at all — could not reach them. The header
button now also carries a Radix ``ContextMenuTrigger``, which brings BOTH
gestures with it (``contextmenu`` for mouse; a built-in 700ms pointerdown timer
for touch), so the same actions open either way. The kebab is unchanged.

Both menus render from one shared item body (``ProjectFolderMenuItems`` in
``Sidebar.tsx``) — one under a Radix ``DropdownMenu`` (the kebab), one under a
``ContextMenu`` (these gestures) — mirroring how the session rows do it.

This guards the wiring the mocked unit tests can't exercise end-to-end:

- Desktop right-click suppresses the native menu and opens the app's, and
  picking an item drives the real dialog.
- A **real touch long-press** opens the same menu. Dispatched via CDP
  ``Input.dispatchTouchEvent`` (touchStart → hold → touchEnd) on a
  ``has_touch`` context, because that is the only way to produce genuine
  ``touchstart``/``pointerdown`` sequences here: ``page.touchscreen.tap()`` is
  instantaneous and cannot hold, and a synthetic ``dispatch_event("pointerdown")``
  emits no ``touchstart`` at all — it under-exercises the pipeline and can pass
  while real touch fails.
- Neither gesture toggles the folder's expand/collapse (the header button's
  onClick), while a plain left-click still does.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

# Phone-width viewport, below the 768px `md` breakpoint: the sidebar is the
# mobile overlay there, which is where a long-press actually matters.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# Comfortably past Radix's 700ms long-press timer.
_LONG_PRESS_MS = 900


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title so its row is easy to spot on the shared
    server (other tests' sessions live there too)."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _create_project(base_url: str, name: str) -> None:
    """Create an empty project via ``POST /v1/projects``.

    Seeded over the API rather than through the sidebar's + button: that button
    is hover-revealed on desktop and hidden entirely on the mobile overlay, and
    what's under test here is the folder header's context menu — not project
    creation (``test_sidebar_projects.py`` already covers that UI path).
    """
    resp = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    resp.raise_for_status()


def _folder_header(page: Page, project: str) -> Locator:
    """The project folder's collapse-toggle header button.

    A DOM locator rather than ``get_by_role``: an open Radix menu is modal and
    ``aria-hidden``s the rest of the tree, so role queries can't see the header
    while its own context menu is up — which is exactly when these tests need to
    read ``aria-expanded``. Project names here are unique hex, so the text filter
    is unambiguous.
    """
    return page.locator("h2 button").filter(has_text=project)


def _expanded(page: Page, project: str) -> str:
    """Read the folder's current expand state as the raw ``aria-expanded`` value.

    Read rather than asserted against a constant: a freshly created project may
    render already-expanded, and what the context-menu tests care about is that
    the gesture leaves the state UNCHANGED, whatever it started as.
    """
    value = _folder_header(page, project).get_attribute("aria-expanded")
    assert value is not None, "folder header is missing aria-expanded"
    return value


def _long_press(
    page: Page,
    target: Locator,
    menu_testid: str = "rename-project",
    hold_ms: int = _LONG_PRESS_MS,
) -> bool:
    """Long-press *target* with real touch events and report whether the menu opened.

    Uses CDP ``Input.dispatchTouchEvent`` so the page sees a genuine
    ``touchstart`` → ``pointerdown`` sequence (Playwright's ``touchscreen.tap``
    is instantaneous and cannot hold a press, and a synthetic
    ``dispatch_event("pointerdown")`` emits no ``touchstart`` at all).

    Polls for the menu *during* the hold rather than only after release: the
    menu opens mid-gesture off Radix's 700ms timer, and a post-release-only
    check can miss it entirely.

    :param page: The page whose CDP session dispatches the touch.
    :param target: Element to press; its bounding box centre is the touch point.
    :param menu_testid: An item test-id that only the expected menu renders.
    :param hold_ms: How long to hold before releasing, in milliseconds.
    :returns: Whether the menu was observed open during (or at the end of) the press.
    """
    box = target.bounding_box()
    assert box is not None, "long-press target has no bounding box"
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2

    cdp = page.context.new_cdp_session(page)
    opened = False
    try:
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
        )
        # Peak-poll through the hold; stop as soon as the menu is up.
        deadline = time.monotonic() + hold_ms / 1000
        while time.monotonic() < deadline:
            if page.get_by_test_id(menu_testid).count() > 0:
                opened = True
                break
            page.wait_for_timeout(50)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        # A menu that opens right at the release still counts.
        if not opened:
            opened = page.get_by_test_id(menu_testid).count() > 0
    finally:
        cdp.detach()
    return opened


@pytest.fixture
def project_page(page: Page, seeded_session: tuple[str, str]) -> Iterator[tuple[Page, str]]:
    """A desktop page with a fresh, empty project folder in the sidebar.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created session.
    :returns: ``(page, project_name)``.
    """
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, f"e2e-projctx-{uuid.uuid4().hex[:8]}")
    project = f"Project {uuid.uuid4().hex[:6]}"

    _create_project(base_url, project)
    page.goto(f"{base_url}/c/{session_id}")
    expect(_folder_header(page, project)).to_be_visible()

    yield (page, project)


def test_right_click_opens_project_folder_menu(project_page: tuple[Page, str]) -> None:
    """Right-clicking a folder header opens the kebab's actions, and they work.

    :param project_page: ``(page, project_name)`` with the folder in the sidebar.
    """
    page, project = project_page
    header = _folder_header(page, project)

    # Whatever state the folder is in, the right-click must not change it.
    before = _expanded(page, project)

    # Radix's ContextMenuTrigger preventDefaults the native contextmenu and
    # opens the app menu at the cursor.
    header.click(button="right")

    # The full kebab action set, same testids — it renders from the shared
    # ProjectFolderMenuItems body. (The kebab DropdownMenu is closed, so these
    # are the context menu's own items.)
    expect(page.get_by_test_id("rename-project")).to_be_visible()
    expect(page.get_by_test_id("project-settings")).to_be_visible()
    expect(page.get_by_test_id("delete-project")).to_be_visible()

    # Opening the menu did NOT collapse/expand the folder.
    expect(header).to_have_attribute("aria-expanded", before)

    # The item drives the real dialog, same as the kebab's Rename.
    page.get_by_test_id("rename-project").click()
    expect(page.get_by_test_id("rename-project-confirm")).to_be_visible()


def test_left_click_still_toggles_the_folder(project_page: tuple[Page, str]) -> None:
    """A plain left-click on the header still expands and collapses the folder.

    :param project_page: ``(page, project_name)`` with the folder in the sidebar.
    """
    page, project = project_page
    header = _folder_header(page, project)

    # Toggle relative to the starting state (a fresh project may already be
    # expanded), so this asserts the flip rather than an absolute value.
    before = _expanded(page, project)
    flipped = "false" if before == "true" else "true"

    header.click()
    expect(header).to_have_attribute("aria-expanded", flipped)
    header.click()
    expect(header).to_have_attribute("aria-expanded", before)


def test_touch_long_press_opens_project_folder_menu(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A real touch long-press on the folder header opens the same menu.

    Runs on its own ``has_touch``/``is_mobile`` phone-width context: mobile is
    the case that had no way to reach these actions at all before, since touch
    cannot right-click.

    Includes a **positive control** — the same press on a session row, whose
    long-press menu already worked before this change — so a negative result on
    the folder header means the folder is broken rather than that the CDP touch
    plumbing failed to reach the page.

    :param browser: Playwright browser fixture (a fresh context is made here).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created session.
    """
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, f"e2e-projctx-touch-{uuid.uuid4().hex[:8]}")
    project = f"Project {uuid.uuid4().hex[:6]}"

    _create_project(base_url, project)

    context = browser.new_context(
        has_touch=True,
        is_mobile=True,
        viewport=_MOBILE_VIEWPORT,
    )
    try:
        page = context.new_page()
        # The mobile sidebar starts closed; ?sidebar=open is the one-shot param
        # the notification-tap destination uses.
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")

        header = _folder_header(page, project)
        expect(header).to_be_visible()
        before = _expanded(page, project)

        # Detector validation: prove the CDP touch pipeline actually reaches the
        # page BEFORE trusting a negative on the folder. A session row's
        # long-press menu is the known-good reference; if this press produces
        # nothing, the plumbing is at fault and the folder result is meaningless.
        row = page.locator(f'a[href="/c/{session_id}"]')
        expect(row).to_be_visible()
        assert _long_press(page, row, menu_testid="rename-conversation"), (
            "CDP touch long-press did not reach the page (control failed) — "
            "the folder assertion below would be meaningless"
        )
        # Dismiss the control's menu so it can't be mistaken for the folder's.
        page.keyboard.press("Escape")
        expect(page.get_by_test_id("rename-conversation")).to_have_count(0)

        # The actual assertion: long-press the folder header.
        assert _long_press(page, header), (
            "long-press on the project folder header did not open the context menu"
        )

        expect(page.get_by_test_id("rename-project")).to_be_visible()
        expect(page.get_by_test_id("project-settings")).to_be_visible()
        expect(page.get_by_test_id("delete-project")).to_be_visible()

        # The long-press must not have toggled the folder: Radix opens the menu
        # mid-gesture, and the press's trailing click would otherwise collapse
        # or expand it under the just-opened menu.
        expect(_folder_header(page, project)).to_have_attribute("aria-expanded", before)
    finally:
        context.close()
