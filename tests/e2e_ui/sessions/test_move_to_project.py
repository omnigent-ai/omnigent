"""Browser e2e for moving a session from the sidebar: into a project or a nest.

Drives the real move flow the ``useConversations`` unit tests cover in
isolation: open the session row's context menu, pick *Add to project* →
a first-class project, and assert the row regroups from the flat
**Sessions** section into the (auto-expanded) project folder — then that
the membership actually committed server-side (``project_id`` on
``GET /v1/sessions/{id}``). The sidebar paints the move optimistically,
so the visual regroup must not wait on the PATCH round-trip, but the
end state must be the server's.

Nesting is driven with a real pointer drag, which jsdom can't lay out: the row
dropped on another row's center nests under it and stays nested after a reload.
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid

import httpx
from playwright.sync_api import Page, Response, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle


def _seed_session(base_url: str, *, title: str) -> str:
    """Create an unfiled session with a unique *title*; return its id."""
    create_resp = post_session_bundle(
        httpx.post, f"{base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()
    return session_id


def test_move_session_into_project_regroups_sidebar_row(
    page: Page,
    live_server: str,
) -> None:
    """Add-to-project moves the row into the folder and persists server-side."""
    uniq = uuid.uuid4().hex[:6]
    project_name = f"E2E Move {uniq}"
    title = f"e2e-move-{uniq}"

    project_resp = httpx.post(
        f"{live_server}/v1/projects",
        json={"name": project_name},
        timeout=10.0,
    )
    project_resp.raise_for_status()
    project_id = project_resp.json()["id"]
    session_id = _seed_session(live_server, title=title)
    try:
        page.goto(live_server)
        row = page.get_by_text(title, exact=True).first
        expect(row).to_be_visible()

        # Context menu → "Add to project" flyout → pick the seeded project.
        row.click(button="right")
        submenu = page.get_by_role("menuitem", name="Move session", exact=True)
        expect(submenu).to_be_visible()
        submenu.hover()
        page.get_by_role("menuitem", name="Projects", exact=True).hover()
        target = page.get_by_role("menuitem", name=project_name)
        expect(target).to_be_visible()
        target.click()

        # The row regroups into the auto-expanded project folder…
        folder_section = page.locator("section").filter(
            has=page.get_by_role("button", name=re.compile(f"^{re.escape(project_name)}"))
        )
        expect(folder_section.get_by_text(title, exact=True)).to_be_visible()
        # …and leaves the flat Sessions section (moved, not duplicated).
        sessions_section = page.locator("section").filter(
            has=page.get_by_role("button", name="Sessions", exact=True)
        )
        expect(sessions_section.get_by_text(title, exact=True)).to_have_count(0)

        # The optimistic paint must converge to a committed membership.
        deadline = time.monotonic() + 10.0
        filed_under: str | None = None
        while time.monotonic() < deadline:
            snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            snapshot.raise_for_status()
            filed_under = snapshot.json().get("project_id")
            if filed_under == project_id:
                break
            time.sleep(0.2)
        assert filed_under == project_id

        # The row stays in the folder after the reconcile refetches land.
        expect(folder_section.get_by_text(title, exact=True)).to_be_visible()
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{live_server}/v1/projects/{project_id}", timeout=10.0)


def _drag_row_onto_row_center(page: Page, source_id: str, target_id: str) -> None:
    """Drag sidebar row *source_id* by mouse and release it on row *target_id*'s center."""
    source = page.locator(f'li[data-sidebar-session-id="{source_id}"]').bounding_box()
    target = page.locator(f'li[data-sidebar-session-id="{target_id}"]').bounding_box()
    assert source is not None and target is not None
    start_x = source["x"] + source["width"] / 2
    start_y = source["y"] + source["height"] / 2
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    # Past the sensor's 5 px activation distance, then onto the target's center band.
    page.mouse.move(start_x, start_y + 10, steps=5)
    page.mouse.move(
        target["x"] + target["width"] / 2, target["y"] + target["height"] / 2, steps=10
    )
    page.mouse.up()


def _sidebar_row_ids(page: Page) -> list[str]:
    """Return the sidebar's session row ids in display order."""
    return page.locator("li[data-sidebar-session-id]").evaluate_all(
        "rows => rows.map((row) => row.dataset.sidebarSessionId)"
    )


def test_drag_nests_a_session_under_a_standalone_session(
    page: Page,
    live_server: str,
) -> None:
    """A row dropped on a standalone row's center nests under it, across a reload."""
    uniq = uuid.uuid4().hex[:6]
    # Seeded first, so the moved session is older than the session it nests under.
    child_id = _seed_session(live_server, title=f"e2e-nest-child-{uniq}")
    head_id = _seed_session(live_server, title=f"e2e-nest-head-{uniq}")
    child_row = page.locator(f'li[data-sidebar-session-id="{child_id}"]')

    def is_patch_of_child(response: Response) -> bool:
        return response.request.method == "PATCH" and response.url.endswith(
            f"/v1/sessions/{child_id}"
        )

    try:
        page.goto(live_server)
        expect(child_row).to_be_visible()
        expect(child_row).not_to_have_attribute("data-sibling-session", "true")

        with page.expect_response(is_patch_of_child) as nested:
            _drag_row_onto_row_center(page, child_id, head_id)
        assert nested.value.ok, nested.value.text()
        family = nested.value.json()["family"]
        assert (family["id"], family["representative_id"]) == (head_id, head_id)

        page.reload()
        expect(child_row).to_have_attribute("data-sibling-session", "true")
        rows = _sidebar_row_ids(page)
        assert rows.index(child_id) == rows.index(head_id) + 1

        # The non-drag path removes it again from the row menu.
        child_row.click(button="right")
        page.get_by_role("menuitem", name="Move session", exact=True).hover()
        page.get_by_role("menuitem", name="Nests", exact=True).hover()
        with page.expect_response(is_patch_of_child) as removed:
            page.get_by_role("menuitem", name="Remove from nest", exact=True).click()
        assert removed.value.ok, removed.value.text()
        assert removed.value.json().get("family") is None
        page.reload()
        expect(child_row).to_be_visible()
        expect(child_row).not_to_have_attribute("data-sibling-session", "true")
    finally:
        for session_id in (child_id, head_id):
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
