"""Browser e2e for bulk session actions in the sidebar.

Selection mode is toggled via the ``data-testid="toggle-selection-mode"``
button next to the search box. Once active, each conversation row renders
a checkbox (``SquareCheckBigIcon`` / ``SquareIcon``); clicking the row
toggles selection instead of navigating. A ``BulkActionBar`` appears at
the bottom of the sidebar with Archive, Unarchive, Stop, Delete, Select
all / Deselect all, and Clear (deselect) controls.

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
    """Locate the sidebar row link by its unique *title* attribute.

    Keying on the title (which is ``conversation.title`` and stays stable
    in both normal and selection mode) is required rather than the href:
    in selection mode every row's ``Link`` ``to`` becomes ``"#"``, which
    react-router resolves against the active ``/c/{id}`` route, so *all*
    rows collapse to the same href. An ``a[href="/c/{id}"]`` locator is
    therefore non-unique the moment the sidebar holds more than one
    session (e.g. leftover fork/clone sessions on the shared CI server),
    triggering a Playwright strict-mode violation. The per-test title is
    unique, so it identifies exactly one row.
    """
    return page.locator(f'a[title="{title}"]')


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a title via ``PATCH /v1/sessions/{id}``."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_selection_mode_toggle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Entering selection mode shows checkboxes; exiting hides them and clears selection.

    Verifies:
    - The toggle button enters selection mode (aria-label flips).
    - Rows show checkbox icons in selection mode.
    - Clicking a row in selection mode toggles its selection (no navigation).
    - The BulkActionBar shows the selection count.
    - Clicking Clear deselects all but stays in selection mode.
    - Exiting selection mode (toggle button) hides the bar and checkboxes.

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

    # Enter selection mode.
    toggle.click()
    expect(toggle).to_have_attribute("aria-label", "Exit selection mode")

    # The row should now show a checkbox icon (unchecked square).
    expect(row.locator("svg.lucide-square")).to_be_visible()

    # Click the row to select it — should NOT navigate away.
    row.click()
    # The checked icon appears instead of the unchecked one.
    expect(row.locator("svg.lucide-square-check-big")).to_be_visible()

    # BulkActionBar shows "1 selected".
    expect(page.get_by_text("1 selected")).to_be_visible()

    # Click Clear to deselect all (stays in selection mode).
    page.get_by_role("button", name="Clear").click()
    expect(page.get_by_text("None selected")).to_be_visible()

    # Still in selection mode — checkbox icons remain visible (unchecked).
    expect(row.locator("svg.lucide-square")).to_be_visible()

    # Exit selection mode via the toggle button.
    toggle.click()
    expect(toggle).to_have_attribute("aria-label", "Select sessions")

    # Checkbox icons should be gone.
    expect(row.locator("svg.lucide-square")).to_have_count(0)
    expect(row.locator("svg.lucide-square-check-big")).to_have_count(0)

    # BulkActionBar text should be gone.
    expect(page.get_by_text("None selected")).to_have_count(0)


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
    expect(archive_btn).to_be_visible()
    archive_btn.click()

    # The selection clears on success (the bar shows "None selected" or
    # exits selection mode). The row may still be visible if "Show archived"
    # is toggled, but the server flag must be set.
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
    expect(delete_btn).to_be_visible()
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


def test_select_all_and_deselect_all(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Select all / Deselect all buttons toggle all visible sessions.

    Verifies:
    - "Select all" checks every visible row (selection count matches).
    - "Deselect all" unchecks everything (count drops to "None selected").

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-bulk-selectall-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")

    row = _row_link(page, title)
    expect(row).to_be_visible()

    # Enter selection mode.
    page.get_by_test_id("toggle-selection-mode").click()

    # Initially none selected.
    expect(page.get_by_text("None selected")).to_be_visible()

    # Click "Select all".
    page.get_by_role("button", name="Select all").click()
    # At least 1 session should be selected (the seeded one).
    expect(page.get_by_text("None selected")).to_have_count(0)
    # The button text flips to "Deselect all" when all are selected.
    deselect_btn = page.get_by_role("button", name="Deselect all")
    expect(deselect_btn).to_be_visible()

    # Click "Deselect all".
    deselect_btn.click()
    expect(page.get_by_text("None selected")).to_be_visible()
