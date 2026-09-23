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
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.conftest import seed_committed_turn


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


def _create_project(page: Page, name: str) -> None:
    """Create an empty project via the "Projects" group header's "New project"
    (+) button, typing *name* into the dialog and confirming."""
    page.get_by_test_id("new-project").click()
    dialog_input = page.get_by_placeholder("Project name…")
    dialog_input.fill(name)
    page.get_by_test_id("new-project-confirm").click()


def _move_to_new_project(page: Page, row: Locator, name: str) -> None:
    """File *row*'s session into a fresh project *name*.

    Projects are created from the "Projects" group header's + button (the
    picker no longer offers an inline "Create new project"), so this first
    creates the empty project, then drives the row kebab → "Add to project" →
    picking that project from the list."""
    _create_project(page, name)
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    # Open the submenu flyout, then pick the just-created project by name.
    page.get_by_test_id("move-to-project").click()
    page.get_by_role("menuitem", name=name, exact=True).click()


def test_move_session_into_new_project(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Filing a session into a freshly-created project moves the row into it.

    The session starts under "Sessions"; after creating the project (+ button)
    and picking it from "Add to project", a project folder with that name
    appears under the "Projects" group and the row lives under it (once
    expanded) and no longer under "Sessions".
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


def test_fork_of_filed_session_joins_the_folder_without_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Forking a filed session shows the clone in the same folder, live.

    Two halves, both needed for the clone to land where the user is looking:
    the server files the fork into the source's project, and the client
    refreshes that folder's own ``["project-sessions", <name>]`` list. A folder
    list has no poll — it converges only on an explicit invalidation — and the
    push stream can't cover this one either (it skips the active session, which
    the fork becomes on navigate). So a fork-time invalidation is the only thing
    that puts the row there before a reload.

    Failure modes this catches:

    - The fork route drops the source's ``project_id``, so the clone is
      unfiled and surfaces under "Sessions" instead of the folder.
    - The fork dialog refreshes only the flat session list, leaving the folder
      stale until the user reloads or navigates away and back.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-proj-fork-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    project = f"Project {uuid.uuid4().hex[:6]}"

    # "Fork from here" is the only fork entry point and it anchors on a
    # COMMITTED assistant response. What's under test is the sidebar, not the
    # model, so seed the exchange instead of driving a turn.
    seed_committed_turn(session_id, prompt="ping", reply="pong")

    page.goto(f"{base_url}/c/{session_id}")

    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    expect(assistant).to_have_count(1, timeout=30_000)

    _move_to_new_project(page, _row(page, session_id), project)
    expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_be_visible()

    reply = assistant.nth(0)
    reply.hover()
    reply.get_by_test_id("fork-from-response").click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    page.get_by_test_id("fork-session-submit").click()

    # Land in the clone — a URL still on the source means the fork never
    # happened, so the folder assertion below would pass vacuously.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]

    # The whole point: the clone's row is under the SAME folder, and no reload
    # or re-navigation happens between the fork and this assertion.
    expect(_section(page, project).locator(f'a[href="/c/{fork_id}"]')).to_be_visible(
        timeout=20_000
    )
    expect(_section(page, "Sessions").locator(f'a[href="/c/{fork_id}"]')).to_have_count(0)


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


# A phone-width viewport, below the 768px `md` breakpoint, so the sidebar
# renders as the mobile overlay — a genuine layout/width concern. It does NOT
# by itself hide the folder's new-session pencil: that reveal is gated on input
# capability (a hover+fine pointer), not width, so tests that need the pencil
# gone pair this viewport with a `has_touch` context.
_MOBILE_VIEWPORT = {"width": 390, "height": 780}
_TABLET_VIEWPORT = {"width": 834, "height": 1112}


def test_project_header_action_is_clickable_on_mobile(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Projects header keeps its new-project action visible on mobile."""
    base_url, session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}?sidebar=open")

    new_project = page.get_by_test_id("new-project")
    expect(new_project).to_be_visible()
    new_project.click()
    expect(page.get_by_placeholder("Project name…")).to_be_visible()


def test_project_header_action_is_clickable_on_touch_tablet(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """The Projects header action stays visible above ``md`` without hover."""
    base_url, session_id = seeded_session
    context = browser.new_context(viewport=_TABLET_VIEWPORT, has_touch=True)
    page = context.new_page()
    try:
        page.goto(f"{base_url}/c/{session_id}")
        assert page.evaluate("matchMedia('(hover: none)').matches")
        new_project = page.get_by_test_id("new-project")
        expect(new_project).to_be_visible()
        expect(new_project.locator("xpath=../..")).to_have_css("opacity", "1")
        new_project.click()
        expect(page.get_by_placeholder("Project name…")).to_be_visible()
    finally:
        context.close()


def test_project_new_session_folds_into_kebab_on_touch(
    browser: Browser,
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Without a fine hover pointer the folder pencil is gone; its action lives
    in the kebab menu.

    The pencil reveal is gated on input capability, not viewport width: it shows
    only where ``(hover: hover) and (pointer: fine)`` holds. On a touch device
    (``has_touch`` → ``hover: none``, ``pointer: coarse``) that never matches, so
    the pencil is ``display:none`` — genuinely absent, not merely clipped — while
    the same "New session" action stays in the folder kebab's menu, linking to
    the pre-filed composer (``/?project=<name>``). The kebab (sr-only on touch)
    is reached by keyboard, the assistive-tech path a long-press can't cover.
    """
    base_url, session_id = seeded_session
    title = f"e2e-proj-touch-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    project = f"Project {uuid.uuid4().hex[:6]}"

    # File the session into a fresh project on the (hover-capable) default page —
    # the row kebab's affordances are hover-revealed, so a touch context can't
    # drive the move. The folder is server-persisted, so the touch context below
    # sees it.
    page.goto(f"{base_url}/c/{session_id}")
    _move_to_new_project(page, _row(page, session_id), project)
    expect(page.get_by_role("button", name=project, exact=True)).to_be_visible()

    # A genuine touch profile: has_touch flips the capability media the pencil
    # gate keys on. The mobile sidebar starts closed, so reopen it via the
    # one-shot ``?sidebar=open`` param (the notification-tap destination).
    context = browser.new_context(viewport=_MOBILE_VIEWPORT, has_touch=True)
    touch = context.new_page()
    try:
        touch.goto(f"{base_url}/c/{session_id}?sidebar=open")
        # Establish the flip empirically rather than assuming it: this is the
        # exact media the pencil/kebab reveal keys on, and it must be false here.
        assert not touch.evaluate("matchMedia('(hover: hover) and (pointer: fine)').matches")

        header = touch.get_by_role("button", name=project, exact=True)
        expect(header).to_be_visible()

        # Scope to THIS project's controls by their per-project accessible names —
        # the shared server carries other tests' folders, so bare test-ids match
        # multiple pencils/kebabs (strict-mode violation).
        pencil = touch.get_by_role("link", name=f"New session in {project}")
        kebab = touch.get_by_role("button", name=f"Project actions for {project}")

        # The pencil is display:none here — genuinely absent, no duplicate of the
        # kebab's "New session" item in the a11y tree.
        expect(pencil).to_be_hidden()

        # The kebab is sr-only on touch: reached by keyboard (focus un-clips it
        # via focus-visible), then opened with Enter — the AT path a long-press
        # can't reliably dispatch.
        kebab.focus()
        kebab.press("Enter")
        # asChild renders the item as the <a> itself, so the href lives on it.
        menu_item = touch.get_by_test_id("project-new-session-menu")
        expect(menu_item).to_be_visible()
        expect(menu_item).to_have_attribute("href", f"/?project={project.replace(' ', '%20')}")
    finally:
        context.close()
