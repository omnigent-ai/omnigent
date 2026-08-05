"""Browser e2e for moving a session into a project from the sidebar.

Drives the real move flow the ``useConversations`` unit tests cover in
isolation: open the session row's context menu, pick *Add to project* →
a first-class project, and assert the row regroups from the flat
**Sessions** section into the (auto-expanded) project folder — then that
the membership actually committed server-side (``project_id`` on
``GET /v1/sessions/{id}``). The sidebar paints the move optimistically,
so the visual regroup must not wait on the PATCH round-trip, but the
end state must be the server's.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle


def _seed_session(base_url: str, *, title: str) -> str:
    """Create an unfiled session with a unique *title*; return its id."""
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
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
        submenu = page.get_by_role("menuitem", name="Add to project")
        expect(submenu).to_be_visible()
        submenu.hover()
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
