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

    filter_sessions.hover()
    expect(page.get_by_role("tooltip")).to_have_text("Filter sessions")
    page.mouse.move(800, 700)
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
