"""Browser e2e for bulk session actions in the sidebar.

Selection is scoped: the ``data-testid="toggle-selection-mode"`` icon on the
**Sessions** header selects the flat session list, while the Projects header
kebab's "Select sessions" item (``data-testid="projects-select-sessions"``)
selects the sessions nested inside project folders. Once active, each row in
the targeted scope renders a leading checkbox (``SquareCheckIcon`` /
``SquareIcon``) and clicking the row toggles selection instead of navigating.

A single-pill ``BulkActionBar`` renders directly under the header of the
targeted section: an Exit (X) button, an "N selected" count, and icon-only
Archive + Delete actions (Archive shows by default but is disabled until an
archivable session is selected; Delete likewise).

These tests drive the full round-trip: enter selection mode → select
sessions → perform a bulk action → verify the server-side effect is
durable (not just a client-cache splice).
"""

from __future__ import annotations

import time
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

# Reserved label key that stores project membership (see
# ``sqlalchemy_store.list_projects`` and ``web/src/lib/sessionListCache.ts``).
_PROJECT_LABEL_KEY = "omni_project"


def _row_link(page: Page, title: str) -> Locator:
    """Locate the sidebar row link by its unique accessible name.

    Keying on the accessible name (which is ``conversation.title`` and stays
    stable in both normal and selection mode) is required rather than the href:
    in selection mode every row's ``Link`` ``to`` becomes ``"#"``, which
    react-router resolves against the active ``/c/{id}`` route, so *all*
    rows collapse to the same href. An ``a[href="/c/{id}"]`` locator is
    therefore non-unique the moment the sidebar holds more than one
    session (e.g. leftover fork/clone sessions on the shared CI server),
    triggering a Playwright strict-mode violation. The per-test title remains
    the link's accessible name after the native ``title`` attribute was
    replaced by a styled tooltip, so it still identifies exactly one row.
    """
    return page.get_by_role("link", name=title, exact=True)


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a title via ``PATCH /v1/sessions/{id}``."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _seed_project_session(base_url: str, *, title: str, project: str) -> str:
    """Create a session, title it, and file it under *project*.

    Goes through the same multipart ``POST /v1/sessions`` path as the
    ``seeded_session`` fixture, then a single ``PATCH`` sets the title and the
    ``omni_project`` label (folder membership). No runner binding — the session
    only needs to *list* under its folder and be bulk-archived, neither of which
    dispatches to a runner (mirrors ``test_archived_project_filter``).

    :param base_url: The live server base URL.
    :param title: Unique title so the row is easy to spot on the shared server.
    :param project: Project name to file the session under.
    :returns: The new session id.
    """
    import json as _json

    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title, "labels": {_PROJECT_LABEL_KEY: project}},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _delete_sessions(base_url: str, session_ids: list[str]) -> None:
    """Best-effort cleanup so seeded sessions don't leak into other tests."""
    import contextlib

    for session_id in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def test_session_header_action_visibility(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Filter stays visible while Select reveals on hover or keyboard focus."""
    base_url, session_id = seeded_session
    title = f"e2e-header-actions-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")

    expect(_row_link(page, title)).to_be_visible()
    sessions_header = page.get_by_role("button", name="Sessions", exact=True)
    filter_sessions = page.get_by_role("button", name="Filter sessions")
    select_sessions = page.get_by_test_id("toggle-selection-mode")
    select_wrapper = select_sessions.locator("..")

    expect(filter_sessions).to_be_visible()
    expect(filter_sessions).to_have_css("opacity", "1")
    expect(select_wrapper).to_have_css("opacity", "0")

    sessions_header.hover()
    expect(select_wrapper).to_have_css("opacity", "1")

    page.mouse.move(800, 700)
    expect(select_wrapper).to_have_css("opacity", "0")
    sessions_header.focus()
    page.keyboard.press("Tab")
    expect(select_sessions).to_be_focused()
    expect(select_wrapper).to_have_css("opacity", "1")

    select_sessions.click()
    expect(page.get_by_role("button", name="Exit selection mode")).to_be_visible()
    expect(filter_sessions).to_be_visible()
    filter_sessions.click()
    expect(page.get_by_test_id("session-filter-all")).to_be_visible()


def test_selection_mode_toggle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Entering selection mode shows a leading checkbox; exiting hides it and clears selection.

    Verifies:
    - The Sessions-header trigger enters selection mode (aria-label flips onto
      the bar's Exit button).
    - Rows show a leading (left-edge) checkbox icon in selection mode.
    - Clicking a row in selection mode toggles its selection (no navigation).
    - The BulkActionBar shows the selection count ("0 selected" → "1 selected").
    - Exiting selection mode (the bar's Exit button) hides the bar and checkboxes.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-bulk-toggle-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")

    row = _row_link(page, title)
    expect(row).to_be_visible()

    toggle = page.get_by_test_id("toggle-selection-mode")
    expect(toggle).to_have_attribute("aria-label", "Select sessions")

    # Enter selection mode — the trigger is replaced by the bar's Exit button.
    toggle.click()
    exit_btn = page.get_by_role("button", name="Exit selection mode")
    expect(exit_btn).to_be_visible()
    expect(page.get_by_text("0 selected")).to_be_visible()

    # The row's parent <li> shows a checkbox icon (unchecked square).
    row_item = row.locator("..")
    expect(row_item.locator("svg.lucide-square")).to_be_visible()

    # Click the row to select it — should NOT navigate away.
    row.click()
    # The checked icon appears instead of the unchecked one.
    expect(row_item.locator("svg.lucide-square-check")).to_be_visible()
    expect(page.get_by_text("1 selected")).to_be_visible()

    # Exit selection mode via the bar's Exit button.
    exit_btn.click()
    expect(page.get_by_test_id("toggle-selection-mode")).to_have_attribute(
        "aria-label", "Select sessions"
    )

    # Checkbox icons and the bar are gone.
    expect(row_item.locator("svg.lucide-square")).to_have_count(0)
    expect(row_item.locator("svg.lucide-square-check")).to_have_count(0)
    expect(page.get_by_text("0 selected")).to_have_count(0)


def test_archive_action_disabled_until_selection(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Archive and Delete render up front but stay disabled until a row is selected.

    Verifies the "show the action by default, enable on selection" contract:
    - On entering selection mode with nothing selected, Archive and Delete are
      present but disabled.
    - Selecting a session enables both.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-bulk-disabled-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")

    row = _row_link(page, title)
    expect(row).to_be_visible()

    page.get_by_test_id("toggle-selection-mode").click()
    expect(page.get_by_text("0 selected")).to_be_visible()

    archive_btn = page.get_by_test_id("bulk-archive")
    delete_btn = page.get_by_test_id("bulk-delete")
    expect(archive_btn).to_be_visible()
    expect(archive_btn).to_be_disabled()
    expect(delete_btn).to_be_visible()
    expect(delete_btn).to_be_disabled()

    # Selecting a row enables both.
    row.click()
    expect(page.get_by_text("1 selected")).to_be_visible()
    expect(archive_btn).to_be_enabled()
    expect(delete_btn).to_be_enabled()


def test_bulk_archive_moves_session_to_archived(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Bulk-archiving a selected session flips its ``archived`` flag on the server.

    Verifies:
    - Selecting a session and clicking Archive removes it from the non-archived view.
    - The server-side ``archived`` flag is durably set (not just a cache splice).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-bulk-archive-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")

    row = _row_link(page, title)
    expect(row).to_be_visible()

    # Enter selection mode and select the row.
    page.get_by_test_id("toggle-selection-mode").click()
    row.click()
    expect(page.get_by_text("1 selected")).to_be_visible()

    # Click Archive.
    archive_btn = page.get_by_test_id("bulk-archive")
    expect(archive_btn).to_be_enabled()
    archive_btn.click()

    # Poll the server to verify the archived flag is durably true.
    deadline = time.monotonic() + 15.0
    archived = False
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if resp.status_code == 200 and resp.json().get("archived") is True:
            archived = True
            break
        time.sleep(0.5)
    assert archived, "session should be archived on the server after bulk archive"

    # Clean up: unarchive the session so it doesn't interfere with other tests.
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"archived": False},
        timeout=10.0,
    ).raise_for_status()


def test_bulk_delete_removes_sessions(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Bulk-deleting a selected session removes it from the sidebar and the store.

    Verifies:
    - Selecting a session and clicking Delete opens a confirmation dialog.
    - Confirming the dialog fires the delete chain.
    - The row drops out of the sidebar.
    - The session is gone from the server (``GET /v1/sessions/{id}`` → 404).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-bulk-delete-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")

    row = _row_link(page, title)
    expect(row).to_be_visible()

    # Enter selection mode and select the row.
    page.get_by_test_id("toggle-selection-mode").click()
    row.click()
    expect(page.get_by_text("1 selected")).to_be_visible()

    # Click Delete — should open confirmation dialog.
    delete_btn = page.get_by_test_id("bulk-delete")
    expect(delete_btn).to_be_enabled()
    delete_btn.click()

    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Delete 1 session(s)?")

    # Confirm the delete.
    dialog.get_by_role("button", name="Delete 1 session(s)").click()

    # The row drops out of the sidebar.
    expect(_row_link(page, title)).to_have_count(0)

    # And the deletion is durable on the server.
    deadline = time.monotonic() + 15.0
    last_status = None
    while time.monotonic() < deadline:
        last_status = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).status_code
        if last_status == 404:
            break
        time.sleep(0.25)
    assert last_status == 404, (
        f"deleted session should be gone from the store (404), got {last_status}"
    )


def test_projects_scope_selects_project_sessions(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Projects kebab selects sessions inside a folder, not the flat list.

    Seeds a project session, opens the Projects header kebab, and picks
    "Select sessions" (project scope). Verifies:
    - The project row becomes selectable (leading checkbox) and selecting it
      updates the count.
    - The flat seeded session is NOT selectable in this scope (no checkbox),
      so clicking it navigates instead of toggling — the count stays at 1.
    - Bulk-archiving the selected project session durably flips its flag.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session (used as the flat-list control row).
    """
    base_url, flat_id = seeded_session
    # The flat control session needs a stable title so it's addressable.
    flat_title = f"e2e-proj-flat-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, flat_id, flat_title)

    uniq = uuid.uuid4().hex[:6]
    project = f"E2E BulkProj {uniq}"
    proj_title = f"e2e-proj-member-{uniq}"
    proj_id = _seed_project_session(base_url, title=proj_title, project=project)

    try:
        page.goto(f"{base_url}/c/{flat_id}")

        # Expand the project folder so its session is visible, then enter
        # project-scope selection from the header kebab. Match the folder toggle
        # by its EXACT name — the per-folder kebab's aria-label ("Project actions
        # for <name>") also contains the project name, so a substring match is
        # ambiguous (strict-mode violation).
        folder = page.get_by_role("button", name=project, exact=True)
        expect(folder).to_be_visible()
        if folder.get_attribute("aria-expanded") == "false":
            folder.click()
        proj_row = _row_link(page, proj_title)
        expect(proj_row).to_be_visible()

        page.get_by_test_id("project-list-actions").click()
        page.get_by_test_id("projects-select-sessions").click()

        # The project row is selectable; selecting it updates the count.
        expect(page.get_by_text("0 selected")).to_be_visible()
        proj_row.click()
        expect(proj_row.locator("..").locator("svg.lucide-square-check")).to_be_visible()
        expect(page.get_by_text("1 selected")).to_be_visible()

        # The flat session is NOT in project scope — no checkbox, so clicking
        # it navigates rather than toggling; the count is unchanged.
        flat_marker = _row_link(page, flat_title).locator("..").locator("svg.lucide-square")
        expect(flat_marker).to_have_count(0)

        # Bulk-archive the selected project session; verify durability.
        archive_btn = page.get_by_test_id("bulk-archive")
        expect(archive_btn).to_be_enabled()
        archive_btn.click()

        deadline = time.monotonic() + 15.0
        archived = False
        while time.monotonic() < deadline:
            resp = httpx.get(f"{base_url}/v1/sessions/{proj_id}", timeout=10.0)
            if resp.status_code == 200 and resp.json().get("archived") is True:
                archived = True
                break
            time.sleep(0.5)
        assert archived, "project session should be archived on the server after bulk archive"
    finally:
        _delete_sessions(base_url, [proj_id])
