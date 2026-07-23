"""Browser e2e for the sidebar's session projects.

Projects group conversations under named, collapsible folders inside a
"Projects" sidebar group. Membership is the first-class
``omnigent_conversation_metadata.project_id`` (see ``projects`` table / the
``project`` filter on ``list_conversations``, which dual-reads it alongside the
legacy ``omni_project`` label). The web UI moves a session via the row kebab's
submenu (``data-testid="move-to-project"``), which calls
``PATCH /v1/sessions/{id}`` with ``{project_id}`` — resolving the picked name to
a project id (creating the first-class row on demand for a label-only folder),
or ``""`` to unfile.

The web UI move submenu is labelled "Add to project" (unfiled) or
"Move session" (already filed); both share ``data-testid="move-to-project"``.

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
    *title* (e.g. "Sessions" or a project name). Section headers carry no count
    or icon, so the header's accessible name is the bare title."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _move_to_new_project(page: Page, row: Locator, name: str) -> None:
    """Drive the row kebab → "Add to project" → "Create new project" flow,
    typing *name* and committing with Enter."""
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    # Open the submenu flyout, then start the inline new-project input.
    page.get_by_test_id("move-to-project").click()
    page.get_by_role("menuitem", name="Create new project").click()
    new_input = page.get_by_placeholder("Project name…")
    new_input.fill(name)
    new_input.press("Enter")


def test_move_session_into_new_project(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Creating a project from the kebab moves the row into it.

    The session starts under "Sessions"; after "Add to project → Create new
    project", a project folder with that name appears under the "Projects" group and the
    row lives under it (once expanded) and no longer under "Sessions".
    """
    base_url, session_id = seeded_session
    title = f"e2e-proj-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_section(page, "Sessions").locator(f'a[href="/c/{session_id}"]')).to_be_visible()

    _move_to_new_project(page, row, project)

    # The project folder appears and auto-expands on the move (so the session
    # you just filed is revealed without a manual click).
    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", "true")

    expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Sessions").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)


def test_remove_session_from_project(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Removing a session from its project drops it back under "Sessions".

    Moves the row into a fresh project first, then uses the kebab's
    "Remove from <project>" item and asserts the row returns to "Sessions".
    The folder itself stays (now a first-class project, it exists independently
    of its members and can be empty) — only the membership is cleared.
    """
    base_url, session_id = seeded_session
    title = f"e2e-proj-rm-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    _move_to_new_project(page, row, project)

    # The folder auto-expands on the move, so its row is already visible.
    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", "true")

    # Remove via the kebab's "Remove from <project>" item (only shown when the
    # session is in a project).
    project_row = (
        _section(page, project)
        .locator("li")
        .filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    )
    project_row.hover()
    project_row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("move-to-project").click()
    # The kebab item names the project it removes from ("Remove from <name>").
    # It unfiles immediately — no confirmation, since a first-class project
    # persists when emptied (nothing is deleted).
    page.get_by_role("menuitem", name=re.compile(rf"Remove from {re.escape(project)}")).click()

    # Back under "Sessions". The first-class project folder persists (empty),
    # since a first-class project exists independently of its members.
    expect(_section(page, "Sessions").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(page.get_by_role("button", name=project, exact=True)).to_have_count(1)
    expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_have_count(0)


# A phone-width viewport: below the 768px `md` breakpoint, so the sidebar is the
# mobile overlay and the folder header's new-session pencil (`max-md:hidden`)
# collapses into the kebab.
_MOBILE_VIEWPORT = {"width": 390, "height": 780}


def test_project_new_session_folds_into_kebab_on_mobile(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """On mobile the folder pencil hides and its action lives in the kebab.

    On desktop the project-folder header shows a hover-revealed pencil that
    starts a new session pre-filed under the project. Below the ``md``
    breakpoint the pencil is hidden (``max-md:hidden``) and the same action is
    offered as a ``md:hidden`` "New session" item inside the folder kebab,
    linking to the pre-filed composer (``/?project=<name>``). Drives the real
    responsive chain the ``Sidebar`` unit test asserts via class names.
    """
    base_url, session_id = seeded_session
    title = f"e2e-proj-mobile-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    project = f"Project {uuid.uuid4().hex[:6]}"

    # File the session into a fresh project on desktop first (the mobile overlay
    # hides the row kebab's hover affordances), then shrink to phone width.
    page.goto(f"{base_url}/c/{session_id}")
    _move_to_new_project(page, _row(page, session_id), project)
    expect(page.get_by_role("button", name=project, exact=True)).to_be_visible()

    # Shrink to phone width; the mobile sidebar starts closed, so reopen it via
    # the one-shot ``?sidebar=open`` param (the notification-tap destination).
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}?sidebar=open")

    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible()

    # Scope to THIS project's controls by their per-project accessible names —
    # the shared server carries other tests' folders, so the bare test-ids match
    # multiple pencils/kebabs (strict-mode violation).
    pencil = page.get_by_role("link", name=f"New session in {project}")
    kebab = page.get_by_role("button", name=f"Project actions for {project}")

    # The pencil is in the DOM but hidden at this width (max-md:hidden).
    expect(pencil).to_be_hidden()

    # Open the folder kebab → the mobile-only "New session" item, pre-filed
    # under this project via the ?project= composer link.
    header.hover()
    kebab.click()
    # asChild renders the item as the <a> itself, so the link href lives on it.
    menu_item = page.get_by_test_id("project-new-session-menu")
    expect(menu_item).to_be_visible()
    expect(menu_item).to_have_attribute("href", f"/?project={project.replace(' ', '%20')}")
