"""Browser coverage for project-folder context-menu actions."""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, Route, expect


def _create_project(page: Page, name: str) -> None:
    """Create an empty project from the Projects header action."""
    # Fine-pointer layouts reveal this control only while its header is hovered.
    page.get_by_role("button", name="Projects", exact=True).hover()
    page.get_by_test_id("new-project").click()
    page.get_by_placeholder("Project name…").fill(name)
    page.get_by_test_id("new-project-confirm").click()
    expect(page.get_by_test_id("new-project-confirm")).to_have_count(0)


def _folder_header(page: Page, project: str) -> Locator:
    """Locate a project folder's collapse-toggle button."""
    return page.locator('button[data-slot="context-menu-trigger"]').filter(has_text=project)


def _expanded(header: Locator) -> str:
    """Read the raw expanded state for comparison across an action."""
    value = header.get_attribute("aria-expanded")
    assert value is not None, "folder header is missing aria-expanded"
    return value


def _project_id(base_url: str, project: str) -> str:
    """Resolve a fresh project's id for setup and server-side checks."""
    response = httpx.get(f"{base_url}/v1/projects", timeout=10.0)
    response.raise_for_status()
    return next(item["id"] for item in response.json()["data"] if item["name"] == project)


def _open_delete_dialog(page: Page, project: str, menu_kind: str) -> None:
    """Open Delete project through the requested project-menu entry point."""
    header = _folder_header(page, project)
    if menu_kind == "context":
        header.click(button="right")
    else:
        page.get_by_role("button", name=f"Project actions for {project}", exact=True).click()
    page.get_by_test_id("delete-project").click()
    expect(page.get_by_role("dialog")).to_be_visible()


@pytest.fixture
def project_page(page: Page, seeded_session: tuple[str, str]) -> tuple[Page, str]:
    """Open a desktop sidebar containing a fresh project folder."""
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")
    _create_project(page, project)
    expect(_folder_header(page, project)).to_be_visible()

    return page, project


@pytest.fixture
def touch_project_page(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> Iterator[tuple[Page, str]]:
    """Open a touch-enabled mobile sidebar with a fresh project folder."""
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    context = browser.new_context(
        has_touch=True,
        viewport={"width": 767, "height": 720},
    )
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")
        _create_project(page, project)
        header = _folder_header(page, project)
        expect(header).to_be_visible()
        header.evaluate(
            "element => element.scrollIntoView({ block: 'center', behavior: 'instant' })"
        )
        expect(header).to_be_in_viewport()
        yield page, project
    finally:
        context.close()


def _touch_point(header: Locator, *, x_offset: float = 0) -> dict[str, float | int]:
    """Return a CDP touch point within the folder header."""
    bounds = header.bounding_box()
    assert bounds is not None, "folder header has no touch target bounds"
    return {
        "id": 0,
        "x": bounds["x"] + bounds["width"] / 2 + x_offset,
        "y": bounds["y"] + bounds["height"] / 2,
    }


def _assert_touch_point_hits(header: Locator, point: dict[str, float | int]) -> None:
    """Confirm that a CDP touch point targets the folder header."""
    hit_target = header.evaluate(
        """(element, point) => {
            const target = document.elementFromPoint(point.x, point.y);
            return { contains: element.contains(target), target: target?.outerHTML };
        }""",
        point,
    )
    assert hit_target["contains"], hit_target


def test_right_click_opens_project_folder_menu(project_page: tuple[Page, str]) -> None:
    """Right-click opens the shared actions without toggling the folder."""
    page, project = project_page
    header = _folder_header(page, project)
    before = _expanded(header)

    header.click(button="right")

    expect(page.get_by_test_id("rename-project")).to_be_visible()
    expect(page.get_by_test_id("project-settings")).to_be_visible()
    expect(page.get_by_test_id("delete-project")).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", before)

    page.get_by_test_id("rename-project").click()
    expect(page.get_by_test_id("rename-project-confirm")).to_be_visible()
    name_input = page.get_by_role("dialog").locator("input")
    expect(name_input).to_be_focused()
    assert name_input.evaluate("input => [input.selectionStart, input.selectionEnd]") == [
        0,
        len(project),
    ]


def test_click_dismissing_menu_does_not_toggle_then_next_click_toggles(
    project_page: tuple[Page, str],
) -> None:
    """A dismissing click is inert; the next click toggles the folder."""
    page, project = project_page
    header = _folder_header(page, project)

    header.click(button="right")
    expect(page.get_by_test_id("rename-project")).to_be_visible()
    before = _expanded(header)
    flipped = "false" if before == "true" else "true"
    bounds = header.bounding_box()
    assert bounds is not None, "folder header has no click target bounds"

    page.mouse.click(
        bounds["x"] + bounds["width"] / 2,
        bounds["y"] + bounds["height"] / 2,
    )
    expect(page.get_by_test_id("rename-project")).not_to_be_visible()
    expect(header).to_have_attribute("aria-expanded", before)

    header.click()
    expect(header).to_have_attribute("aria-expanded", flipped)


def test_left_click_still_toggles_the_folder(project_page: tuple[Page, str]) -> None:
    """Plain left-click still expands and collapses the folder."""
    page, project = project_page
    header = _folder_header(page, project)
    before = _expanded(header)
    flipped = "false" if before == "true" else "true"

    expect(header).to_have_attribute("data-slot", "context-menu-trigger")
    header.click()
    expect(header).to_have_attribute("aria-expanded", flipped)
    header.click()
    expect(header).to_have_attribute("aria-expanded", before)


def test_keyboard_opens_project_folder_menu(project_page: tuple[Page, str]) -> None:
    """The Menu key opens the folder actions from the focused header."""
    page, project = project_page
    header = _folder_header(page, project)
    before = _expanded(header)

    header.focus()
    header.press("ContextMenu")

    expect(page.get_by_test_id("rename-project")).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", before)


def test_touch_long_press_opens_project_folder_menu(
    touch_project_page: tuple[Page, str],
) -> None:
    """A stationary touch hold opens actions without toggling or selecting."""
    page, project = touch_project_page
    header = _folder_header(page, project)
    before = _expanded(header)
    cdp = page.context.new_cdp_session(page)
    point = _touch_point(header)
    _assert_touch_point_hits(header, point)

    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    try:
        page.wait_for_timeout(750)
        expect(page.get_by_test_id("rename-project")).to_be_visible()
    finally:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    user_select = header.evaluate("element => getComputedStyle(element).userSelect")
    assert user_select == "none"
    # Desktop Chrome does not synthesize the iOS selection; computed style and
    # the unit class check provide the discriminating coverage.
    selection = page.evaluate("window.getSelection()?.toString() ?? ''")
    assert selection == "", f"project title is selected: {selection!r}"
    expect(header).to_have_attribute("aria-expanded", before)


def test_moving_touch_hold_does_not_open_project_folder_menu(
    touch_project_page: tuple[Page, str],
) -> None:
    """Touch movement cancels the pending folder context menu."""
    page, project = touch_project_page
    header = _folder_header(page, project)
    before = _expanded(header)
    cdp = page.context.new_cdp_session(page)
    point = _touch_point(header)
    _assert_touch_point_hits(header, point)

    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [point]},
    )
    try:
        page.wait_for_timeout(100)
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [_touch_point(header, x_offset=20)]},
        )
        page.wait_for_timeout(700)
        expect(page.get_by_test_id("rename-project")).not_to_be_visible()
    finally:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    expect(header).to_have_attribute("aria-expanded", before)


@pytest.mark.parametrize("menu_kind", ["context", "dropdown"])
def test_delete_project_paints_before_archive_and_commits(
    project_page: tuple[Page, str],
    seeded_session: tuple[str, str],
    menu_kind: str,
) -> None:
    """The folder leaves before archive finishes, then the server converges."""
    page, project = project_page
    base_url, session_id = seeded_session
    project_id = _project_id(base_url, project)
    held: list[Route] = []
    try:
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"project_id": project_id},
            timeout=10.0,
        ).raise_for_status()
        page.goto(base_url)
        header = _folder_header(page, project)
        if _expanded(header) == "false":
            header.click()
        expect(header).to_have_attribute("aria-expanded", "true")
        row = page.locator(f'a[href="/c/{session_id}"]')
        expect(row).to_be_visible()

        def hold_archive(route: Route) -> None:
            if route.request.method == "PATCH":
                held.append(route)
            else:
                route.continue_()

        page.route(f"**/v1/sessions/{session_id}", hold_archive)
        _open_delete_dialog(page, project, menu_kind)
        page.get_by_role("button", name="Delete project", exact=True).click()

        deadline = time.monotonic() + 10.0
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held) == 1, "archive PATCH never reached the browser route"

        # The request is still held, so these states cannot depend on a server response.
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(header).to_have_count(0)
        expect(row).to_have_count(0)
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        assert snapshot["archived"] is not True
        assert snapshot["project_id"] == project_id

        with page.expect_response(
            lambda response: (
                response.request.method == "DELETE"
                and response.url.endswith(f"/v1/projects/{project_id}")
            )
        ) as delete_response:
            held.pop(0).continue_()
        assert delete_response.value.ok

        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        assert snapshot["archived"] is True
        assert snapshot.get("project_id") is None
        assert httpx.get(f"{base_url}/v1/projects/{project_id}", timeout=10.0).status_code == 404
        expect(header).to_have_count(0)
    finally:
        while held:
            held.pop(0).continue_()
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)


def test_failed_project_archive_restores_folder_and_warns(
    project_page: tuple[Page, str],
    seeded_session: tuple[str, str],
) -> None:
    """A rejected archive restores the optimistic removal and shows a toast."""
    page, project = project_page
    base_url, session_id = seeded_session
    project_id = _project_id(base_url, project)
    try:
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"project_id": project_id},
            timeout=10.0,
        ).raise_for_status()
        page.goto(base_url)
        header = _folder_header(page, project)
        if _expanded(header) == "false":
            header.click()
        expect(header).to_have_attribute("aria-expanded", "true")
        row = page.locator(f'a[href="/c/{session_id}"]')
        expect(row).to_be_visible()

        def reject_archive(route: Route) -> None:
            if route.request.method == "PATCH":
                route.fulfill(status=500, json={"error": {"message": "Archive unavailable"}})
            else:
                route.continue_()

        page.route(f"**/v1/sessions/{session_id}", reject_archive)
        _open_delete_dialog(page, project, "dropdown")
        page.get_by_role("button", name="Delete project", exact=True).click()

        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(header).to_be_visible()
        expect(row).to_be_visible()
        expect(page.get_by_test_id("toast")).to_contain_text(f"Couldn't delete {project}")
        expect(page.get_by_test_id("toast")).to_contain_text("1 session couldn't be archived")

        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        assert snapshot["archived"] is not True
        assert snapshot["project_id"] == project_id
        assert httpx.get(f"{base_url}/v1/projects/{project_id}", timeout=10.0).status_code == 200
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)
