"""Browser e2e for Settings → Session management bulk actions.

``/settings/sessions`` lists active sessions with multi-select Archive /
Delete. Shared or non-owned rows stay visible but cannot be selected.
These tests drive the real SPA + live server chain: seed sessions over
REST, open the Settings surface, select owned rows, and assert both the
UI update and the durable server-side effect (matching the sidebar bulk
action e2es in ``test_sidebar_bulk_actions.py``).
"""

from __future__ import annotations

import time
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title via ``PATCH /v1/sessions/{id}``."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _row(page: Page, session_id: str) -> Locator:
    """Locate a Session management row by its durable session id."""
    return page.locator(f'[data-testid="session-mgmt-row"][data-session-id="{session_id}"]')


def test_settings_nav_opens_session_management(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Settings nav exposes Session management and routes to ``/settings/sessions``.

    Verifies:
    - The settings button opens the settings surface.
    - ``settings-nav-sessions`` is present under the Sessions group.
    - Clicking it lands on the Session management heading.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_test_id("settings-button").click()
    page.wait_for_url("**/settings**", timeout=30_000)

    nav = page.get_by_test_id("settings-nav-sessions")
    expect(nav).to_be_visible()
    expect(nav).to_have_attribute("href", "/settings/sessions")
    nav.click()

    page.wait_for_url("**/settings/sessions", timeout=30_000)
    expect(page.get_by_role("heading", name="Session management")).to_be_visible()


def test_bulk_archive_active_sessions_from_settings(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Selecting two active sessions and clicking Archive flips both server flags.

    Verifies:
    - Both seeded rows appear on Session management.
    - Selecting them updates the action-bar count.
    - Archive removes them from the active list.
    - ``GET /v1/sessions/{id}`` reports ``archived: true`` for each.
    """
    base_url, session_a, session_b = seeded_session_pair
    suffix = uuid.uuid4().hex[:8]
    _set_title(base_url, session_a, f"e2e-settings-archive-a-{suffix}")
    _set_title(base_url, session_b, f"e2e-settings-archive-b-{suffix}")

    page.goto(f"{base_url}/settings/sessions")
    expect(page.get_by_role("heading", name="Session management")).to_be_visible()

    row_a = _row(page, session_a)
    row_b = _row(page, session_b)
    expect(row_a).to_be_visible()
    expect(row_b).to_be_visible()

    row_a.click()
    row_b.click()
    expect(page.get_by_text("2 selected")).to_be_visible()

    archive_btn = page.get_by_test_id("session-mgmt-archive")
    expect(archive_btn).to_be_enabled()
    archive_btn.click()

    expect(row_a).to_have_count(0)
    expect(row_b).to_have_count(0)

    for session_id in (session_a, session_b):
        deadline = time.monotonic() + 15.0
        archived = False
        while time.monotonic() < deadline:
            resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
            if resp.status_code == 200 and resp.json().get("archived") is True:
                archived = True
                break
            time.sleep(0.5)
        assert archived, f"session {session_id} should be archived after settings bulk archive"

    # Restore so fixture teardown (and other tests) see non-archived sessions.
    for session_id in (session_a, session_b):
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"archived": False},
            timeout=10.0,
        ).raise_for_status()


def test_bulk_delete_active_sessions_from_settings(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Delete requires confirmation and removes the session from UI + store.

    Verifies:
    - Selecting a row and clicking Delete opens the confirmation dialog.
    - Confirming removes the row from Session management.
    - ``GET /v1/sessions/{id}`` returns 404.
    """
    base_url, session_id = seeded_session
    title = f"e2e-settings-delete-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/settings/sessions")
    expect(page.get_by_role("heading", name="Session management")).to_be_visible()

    row = _row(page, session_id)
    expect(row).to_be_visible()
    row.click()
    expect(page.get_by_text("1 selected")).to_be_visible()

    page.get_by_test_id("session-mgmt-delete").click()

    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Delete 1 session(s)?")
    expect(dialog).to_contain_text("Branches are not cleaned up")

    page.get_by_test_id("session-mgmt-confirm-delete").click()

    expect(_row(page, session_id)).to_have_count(0)

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


def test_select_all_and_clear_on_session_management(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Select all covers loaded owned rows; Clear resets the selection.

    Verifies:
    - Select all flips the button to Deselect all and leaves None selected empty.
    - Clear returns to None selected.
    """
    base_url, session_id = seeded_session
    title = f"e2e-settings-selectall-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/settings/sessions")
    expect(_row(page, session_id)).to_be_visible()

    expect(page.get_by_text("None selected")).to_be_visible()

    page.get_by_test_id("session-mgmt-select-all").click()
    expect(page.get_by_text("None selected")).to_have_count(0)
    # Button label flips once every loaded owned row is selected.
    expect(page.get_by_test_id("session-mgmt-select-all")).to_have_text("Deselect all")

    page.get_by_test_id("session-mgmt-clear").click()
    expect(page.get_by_text("None selected")).to_be_visible()
    expect(page.get_by_test_id("session-mgmt-select-all")).to_have_text("Select all")
