"""Browser e2e for the sidebar's session projects.

Projects group conversations under named, collapsible folders inside a
"Projects" sidebar group. Membership is stored server-side as a
``conversation_labels`` row with the reserved key ``"omni_project"`` (no new
table — see ``sqlalchemy_store.list_projects`` / the ``project`` filter on
``list_conversations``). The web UI moves a session via the row kebab's
**"Change project"** submenu (``data-testid="move-to-project"``), which calls
``PATCH /v1/sessions/{id}`` with ``{labels:{omni_project}}`` (an empty value
removes the label).

These drive the real chain the ``Sidebar`` unit tests mock out: the kebab
submenu → the PATCH → the refreshed ``GET /v1/sessions/projects`` and
``GET /v1/sessions`` lists → the row landing under (or leaving) a project
folder. Project folders render collapsed by default, so the tests expand the
folder to assert membership.
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title via ``PATCH /v1/sessions/{id}`` so its row
    is easy to spot among other tests' sessions in the shared server."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar ``<section>`` whose collapse-header button reads
    *title* (e.g. "Chats" or a project name). Section headers carry no count or
    icon, so the header's accessible name is the bare title."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _move_to_new_project(page: Page, row: Locator, name: str) -> None:
    """Drive the row kebab → "Change project" → "New project…" flow,
    typing *name* and committing with Enter."""
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    # Open the submenu flyout, then start the inline new-project input.
    page.get_by_test_id("move-to-project").click()
    page.get_by_role("menuitem", name="New project…").click()
    new_input = page.get_by_placeholder("Project name…")
    new_input.fill(name)
    new_input.press("Enter")


def test_move_session_into_new_project(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Creating a project from the kebab moves the row into it.

    The session starts under "Chats"; after "Change project → New project…",
    a project folder with that name appears under the "Projects" group and the
    row lives under it (once expanded) and no longer under "Chats".
    """
    base_url, session_id = seeded_session
    title = f"e2e-proj-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_section(page, "Chats").locator(f'a[href="/c/{session_id}"]')).to_be_visible()

    _move_to_new_project(page, row, project)

    # The project folder appears; folders render collapsed by default, so expand
    # it before asserting membership.
    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", "false")
    header.click()

    expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Chats").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)


def test_remove_session_from_project(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Removing a session from its project drops it back under "Chats".

    Moves the row into a fresh project first, then uses the kebab's
    "Remove from project" item and asserts the row returns to "Chats".
    """
    base_url, session_id = seeded_session
    title = f"e2e-proj-rm-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    _move_to_new_project(page, row, project)

    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible()
    header.click()

    # Remove via the kebab's "Remove from project" item (only shown when the
    # session is in a project).
    project_row = (
        _section(page, project)
        .locator("li")
        .filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    )
    project_row.hover()
    project_row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("move-to-project").click()
    page.get_by_role("menuitem", name=re.compile("Remove from project")).click()
    # Removal is confirmed (it may delete the implicit project) — accept it.
    page.get_by_role("button", name="Remove from project", exact=True).click()

    # Back under "Chats", and the now-empty project folder is gone.
    expect(_section(page, "Chats").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(page.get_by_role("button", name=project, exact=True)).to_have_count(0)
