"""Personal project ordering through the sidebar and persisted API."""

import re
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect


def test_drag_project_order_persists_and_resets(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    prefix = f"Order-{uuid.uuid4().hex[:6]}"
    names = [f"{prefix} {suffix}" for suffix in ("BUG", "DOC", "WORK")]
    ids = []
    for name in names:
        response = httpx.post(f"{base_url}/v1/projects", json={"name": name})
        response.raise_for_status()
        ids.append(response.json()["id"])
    try:
        page.goto(f"{base_url}/c/{session_id}")
        group = page.get_by_role("button", name="Projects", exact=True)
        if group.get_attribute("aria-expanded") == "false":
            group.click()
        header = page.locator('button[data-slot="context-menu-trigger"]').filter(has_text=names[2])
        header.hover()
        # A normal click and Enter keep their expand/collapse behavior.
        header.click()
        expect(header).to_have_attribute("aria-expanded", "true")
        header.press("Enter")
        expect(header).to_have_attribute("aria-expanded", "false")
        header.click()
        expect(header).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".lucide-grip-vertical")).to_have_count(0)
        source = page.get_by_role("button", name=names[2], exact=True)
        target = page.get_by_role("button", name=names[0], exact=True)
        start = source.bounding_box()
        end = target.bounding_box()
        assert start and end
        page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
        page.mouse.down()
        page.mouse.move(start["x"] + start["width"] / 2, start["y"] - 8, steps=3)
        page.mouse.move(end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=10)
        expect(page.get_by_test_id("project-order-insertion")).to_be_visible()
        with page.expect_response(
            lambda r: r.url.endswith("/v1/projects/order") and r.request.method == "PUT"
        ) as saved:
            page.mouse.up()
        assert saved.value.ok
        stored = saved.value.json()["ordered_project_ids"]
        assert [id for id in stored if id in ids] == [ids[2], ids[0], ids[1]]
        expect(header).to_have_attribute("aria-expanded", "true")
        page.reload()
        expect(header).to_have_attribute("aria-expanded", "true")
        headers = page.locator(f'button[data-project-order-name^="{prefix}"]')
        expect(headers).to_have_count(3)
        for index, name in enumerate((names[2], names[0], names[1])):
            expect(headers.nth(index)).to_have_attribute("data-project-order-name", name)

        # Keyboard sorting shares the same persistence path.
        keyboard_header = page.get_by_role("button", name=names[2], exact=True)
        keyboard_header.focus()
        keyboard_header.press("Space")
        expect(keyboard_header).to_have_attribute("aria-pressed", "true")
        keyboard_header.press("ArrowDown")
        expect(page.get_by_test_id("project-order-insertion")).to_be_visible()
        with page.expect_response(
            lambda r: r.url.endswith("/v1/projects/order") and r.request.method == "PUT"
        ) as saved:
            keyboard_header.press("Space")
        assert saved.value.ok
        assert [id for id in saved.value.json()["ordered_project_ids"] if id in ids] == ids[
            :1
        ] + ids[2:] + ids[1:2]

        # Reset through the web UI restores alphabetical discovery.
        group.hover()
        page.get_by_test_id("project-list-actions").click()
        page.get_by_role("menuitem", name="Sort projects by", exact=True).hover()
        expect(page.get_by_role("menuitemradio", name="Manual order", exact=True)).to_be_checked()
        with page.expect_response(
            lambda r: r.url.endswith("/v1/projects/order") and r.request.method == "PUT"
        ) as reset:
            page.get_by_role("menuitemradio", name="Alphabetically", exact=True).click()
        assert reset.value.json()["sort_mode"] == "alphabetical"
        assert (
            reset.value.json()["ordered_project_ids"] == saved.value.json()["ordered_project_ids"]
        )
        for index, name in enumerate(names):
            expect(headers.nth(index)).to_have_attribute("data-project-order-name", name)
        # The saved manual order survives alphabetical mode and a full reload.
        page.reload()
        group.hover()
        page.get_by_test_id("project-list-actions").click()
        page.get_by_role("menuitem", name="Sort projects by", exact=True).hover()
        expect(
            page.get_by_role("menuitemradio", name="Alphabetically", exact=True)
        ).to_be_checked()
        with page.expect_response(
            lambda r: r.url.endswith("/v1/projects/order") and r.request.method == "PUT"
        ) as manual:
            page.get_by_role("menuitemradio", name="Manual order", exact=True).click()
        assert manual.value.json() == saved.value.json()
        for index, name in enumerate((names[0], names[2], names[1])):
            expect(headers.nth(index)).to_have_attribute("data-project-order-name", name)
        # A session drag still files the session into a project.
        sessions = page.get_by_role("button", name="Sessions", exact=True)
        if sessions.get_attribute("aria-expanded") == "false":
            sessions.click()
        row = page.locator(f'a[href="/c/{session_id}"]').first
        expect(row).to_be_visible()
        source_box = row.bounding_box()
        destination_box = (
            page.locator('button[data-slot="context-menu-trigger"]')
            .filter(
                has_text=names[1],
            )
            .bounding_box()
        )
        assert source_box and destination_box
        x = source_box["x"] + source_box["width"] / 2
        y = source_box["y"] + source_box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 10, y, steps=3)
        page.mouse.move(
            destination_box["x"] + destination_box["width"] / 2,
            destination_box["y"] + destination_box["height"] / 2,
            steps=10,
        )
        with page.expect_response(
            lambda r: f"/v1/sessions/{session_id}" in r.url and r.request.method == "PATCH",
        ) as filed:
            page.mouse.up()
        assert filed.value.ok
        assert httpx.get(f"{base_url}/v1/sessions/{session_id}").json()["project_id"] == ids[1]
    finally:
        for id in ids:
            httpx.delete(f"{base_url}/v1/projects/{id}")
        httpx.put(f"{base_url}/v1/projects/order", json={"ordered_project_ids": None})


@pytest.mark.parametrize("peek", [False, True])
def test_project_drag_preview_keeps_grab_offset(
    page: Page,
    seeded_session: tuple[str, str],
    peek: bool,
) -> None:
    """Dragging preserves open folders and anchors the preview at the grab point."""
    base_url, session_id = seeded_session
    prefix = f"Follow-{uuid.uuid4().hex[:6]}"
    names = [f"{prefix} {suffix}" for suffix in ("A", "B", "C", "Z")]
    ids = []
    for name in names:
        response = httpx.post(f"{base_url}/v1/projects", json={"name": name})
        response.raise_for_status()
        ids.append(response.json()["id"])
    try:
        page.set_viewport_size({"width": 1280, "height": 1000})
        page.goto(f"{base_url}/c/{session_id}")
        group = page.get_by_role("button", name="Projects", exact=True)
        if group.get_attribute("aria-expanded") == "false":
            group.click()
        expanded_names = [names[0], names[2], names[3]]
        for name in expanded_names:
            header = page.get_by_role("button", name=name, exact=True)
            header.click()
            expect(header).to_have_attribute("aria-expanded", "true")
            # Folder loading placeholders have a different height from empty rows.
            expect(
                header.locator("xpath=ancestor::section[1]").get_by_text(
                    "No sessions. Start a", exact=False
                )
            ).to_be_visible()
        if peek:
            page.get_by_role("button", name="Close sidebar", exact=True).click()
            page.get_by_role("button", name="Open sidebar", exact=True).hover()
            expect(page.locator("aside.conversations-sidebar")).to_have_class(
                re.compile("is-peek")
            )
        source = page.get_by_role("button", name=names[-1], exact=True)
        source.click(trial=True)
        box = source.bounding_box()
        assert box is not None
        grab_x = box["x"] + box["width"] - 70
        grab_y = box["y"] + box["height"] / 2
        page.mouse.move(grab_x, grab_y)
        page.mouse.down()
        page.mouse.move(grab_x + 12, grab_y + 12, steps=3)
        overlay = page.locator('div[class*="max-w-[16rem]"]').filter(has_text=names[-1])
        expect(overlay).to_be_visible()
        for name in names:
            header = page.get_by_role("button", name=name, exact=True)
            expect(header).to_have_attribute(
                "aria-expanded",
                "true" if name in expanded_names else "false",
            )
            if name in expanded_names:
                expect(
                    header.locator("xpath=ancestor::section[1]").get_by_text(
                        "No sessions. Start a",
                        exact=False,
                    )
                ).to_be_visible()
        for dx, dy in [(30, -25), (60, -70), (20, 15)]:
            page.mouse.move(grab_x + dx, grab_y + dy, steps=10)
            # The overlay is placed by React following the sensor update.
            page.wait_for_function(
                """({name, x, y}) => {
                    const overlay = [...document.querySelectorAll('div')].find(el =>
                        el.className.includes('max-w-[16rem]') && el.textContent === name);
                    if (!overlay) return false;
                    const rect = overlay.getBoundingClientRect();
                    return Math.abs(rect.x - x) < 3 && Math.abs(rect.y - y) < 3;
                }""",
                arg={"name": names[-1], "x": box["x"] + dx, "y": box["y"] + dy},
                timeout=3000,
            )
        preview = overlay.bounding_box()
        assert preview is not None
        assert abs(preview["width"] - box["width"]) < 3
        page.keyboard.press("Escape")
        page.mouse.up()
        if peek:
            page.goto(f"{base_url}/c/{session_id}?sidebar=open")
        for name in names:
            expect(page.get_by_role("button", name=name, exact=True)).to_have_attribute(
                "aria-expanded",
                "true" if name in expanded_names else "false",
            )
    finally:
        page.keyboard.press("Escape")
        page.mouse.up()
        for id in ids:
            httpx.delete(f"{base_url}/v1/projects/{id}")


@pytest.mark.parametrize("peek", [False, True])
def test_filed_session_drag_preview_keeps_grab_offset(
    page: Page,
    seeded_session: tuple[str, str],
    peek: bool,
) -> None:
    """A session inside an expanded project follows the cursor and can be refiled."""
    base_url, session_id = seeded_session
    prefix = f"Session-follow-{uuid.uuid4().hex[:6]}"
    names = [f"{prefix} A", f"{prefix} Z"]
    title = f"{prefix} session"
    ids = []
    for name in names:
        response = httpx.post(f"{base_url}/v1/projects", json={"name": name})
        response.raise_for_status()
        ids.append(response.json()["id"])
    try:
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"title": title, "project_id": ids[-1]},
        ).raise_for_status()
        page.set_viewport_size({"width": 1280, "height": 1000})
        page.goto(f"{base_url}/c/{session_id}")
        projects = page.get_by_role("button", name="Projects", exact=True)
        if projects.get_attribute("aria-expanded") == "false":
            projects.click()
        for name in names:
            header = page.get_by_role("button", name=name, exact=True)
            if header.get_attribute("aria-expanded") == "false":
                header.click()
        source = page.get_by_role("link", name=title, exact=True)
        expect(source).to_be_visible()
        if peek:
            page.get_by_role("button", name="Close sidebar", exact=True).click()
            page.get_by_role("button", name="Open sidebar", exact=True).hover()
            aside = page.locator("aside.conversations-sidebar")
            expect(aside).to_have_class(re.compile("is-peek"))
            expect(aside).to_have_css("pointer-events", "auto")
        box = source.locator("xpath=ancestor::li[1]").bounding_box()
        assert box is not None
        grab_x = box["x"] + box["width"] - 80
        grab_y = box["y"] + box["height"] / 2
        page.mouse.move(grab_x, grab_y)
        page.mouse.down()
        page.mouse.move(grab_x + 12, grab_y + 12, steps=3)
        overlay = page.locator('div[class*="max-w-[16rem]"]').filter(has_text=title)
        expect(overlay).to_be_visible()
        for dx, dy in [(30, -25), (60, -70), (20, 15)]:
            page.mouse.move(grab_x + dx, grab_y + dy, steps=10)
            page.wait_for_function(
                """({title, x, y}) => {
                    const overlay = [...document.querySelectorAll('div')].find(el =>
                        el.className.includes('max-w-[16rem]') && el.textContent === title);
                    if (!overlay) return false;
                    const rect = overlay.getBoundingClientRect();
                    return Math.abs(rect.x - x) < 3 && Math.abs(rect.y - y) < 3;
                }""",
                arg={"title": title, "x": box["x"] + dx, "y": box["y"] + dy},
                timeout=3000,
            )
        preview = overlay.bounding_box()
        assert preview is not None
        assert abs(preview["width"] - box["width"]) < 3
        for name in names:
            expect(page.get_by_role("button", name=name, exact=True)).to_have_attribute(
                "aria-expanded", "true"
            )
        destination = page.get_by_role("button", name=names[0], exact=True).bounding_box()
        assert destination is not None
        page.mouse.move(
            destination["x"] + destination["width"] / 2,
            destination["y"] + destination["height"] / 2,
            steps=10,
        )
        with page.expect_response(
            lambda r: f"/v1/sessions/{session_id}" in r.url and r.request.method == "PATCH"
        ) as filed:
            page.mouse.up()
        assert filed.value.ok
        assert httpx.get(f"{base_url}/v1/sessions/{session_id}").json()["project_id"] == ids[0]
    finally:
        page.keyboard.press("Escape")
        page.mouse.up()
        for project_id in ids:
            httpx.delete(f"{base_url}/v1/projects/{project_id}")
