"""UI journey: a shared-with viewer leaves a session to clear their sidebar.

Every other sidebar row action is owner-only, so "Leave session" is the one
action a shared-with viewer actually has: it drops their own grant so the
session stops cluttering their sidebar, without touching the owner's copy.

It is not a new menu item — the row's single destructive slot resolves by
ownership. A non-owner previously got **Delete** rendered there permanently
disabled ("only the session owner can delete"), a row that could never do
anything; Leave takes that slot.

The behavior under test:

- A **non-owner** the session is shared with sees an enabled **Leave session**
  in that slot (and NO Delete), and confirming it removes the row from their
  sidebar. Their access is genuinely gone (the session 404s), while the
  **owner still sees it**.
- The **owner** gets **Delete** in the same slot and never Leave — their grant
  is what keeps the session reachable, so leaving would orphan it.

Runs against the dedicated multi-user server for the same reason as
``test_sidebar_ownership_gating``: the shared ``live_server`` is single-user,
which hides sharing affordances entirely, so a leave can't be observed there.
The admin owns the session and a second header identity is granted access via
the REST API.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)

# Read access is the minimum a grantee can hold, and leaving must work from it
# — the route gates on read, not manage. Mirrors LEVEL_READ in
# omnigent/server/auth.py.
_LEVEL_READ = 1

_FILTER = '[data-testid="session-filter"]'
_FILTER_SHARED = '[data-testid="session-filter-shared"]'


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A NON-single-user server so the shared filter + Leave item render."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_leave_shared_multi_user")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


def _grant(server: MultiUserServer, user_id: str, level: int) -> None:
    """Grant *user_id* *level* on the admin-owned session (admin acts)."""
    resp = httpx.put(
        f"{server.base_url}/v1/sessions/{server.session_id}/permissions",
        json={"user_id": user_id, "level": level},
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=30.0,
    )
    resp.raise_for_status()


def _row(page: Page, session_id: str) -> Locator:
    """The sidebar row (``<li>``) for *session_id*, located by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _open_row_menu(page: Page, session_id: str) -> None:
    """Right-click the row to open the shared actions menu at the cursor.

    Right-click (Radix ``ContextMenu``) renders the same
    ``ConversationMenuItems`` body as the kebab, so the item testids match and
    it avoids the kebab's pointer-event timing.
    """
    link = page.locator(f'a[href="/c/{session_id}"]')
    expect(link).to_be_visible(timeout=30_000)
    link.click(button="right")


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_shared_viewer_leaves_session_and_row_disappears(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """A read-level grantee leaves: the row goes, the owner keeps the session.

    The CUJ from the feature request — "unshare yourself from a shared session"
    to keep the sidebar clean. Read access is deliberately the level used here:
    leaving needs no manage grant.
    """
    server = multi_user_server
    sid = server.session_id
    viewer_email = f"leaver-{uuid.uuid4().hex[:6]}@ui.test"
    _grant(server, viewer_email, _LEVEL_READ)

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": viewer_email})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{sid}")

        # The shared session is visible before leaving, under both the default
        # filter and the shared one.
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)
        expect(_row(page, sid)).to_be_visible(timeout=30_000)
        page.locator(_FILTER).click()
        page.locator(_FILTER_SHARED).click()
        expect(_row(page, sid)).to_be_visible(timeout=30_000)

        _open_row_menu(page, sid)
        # Non-owner → the destructive slot IS Leave: enabled, and in place of the
        # Delete that used to render there permanently disabled. Rename/Share stay
        # owner-gated, so this is the viewer's only real action.
        leave_item = page.get_by_test_id("leave-conversation")
        expect(leave_item).to_be_visible(timeout=15_000)
        expect(leave_item).not_to_have_attribute("data-disabled", re.compile(r".*"))
        expect(page.get_by_test_id("delete-conversation")).to_have_count(0)
        leave_item.click()

        # Destructive → confirm first, then the row leaves the sidebar.
        confirm = page.get_by_test_id("confirm-leave-conversation")
        expect(confirm).to_be_visible(timeout=15_000)
        confirm.click()
        expect(_row(page, sid)).to_have_count(0, timeout=30_000)

        # Access is really gone, not just hidden from the list.
        after = httpx.get(
            f"{server.base_url}/v1/sessions/{sid}",
            headers={"X-Forwarded-Email": viewer_email},
            timeout=30.0,
        )
        assert after.status_code == 404, (
            f"expected 404 reading a left session, got {after.status_code}"
        )
        # ...and the owner is unaffected — leaving drops only the leaver's grant.
        owner_view = httpx.get(
            f"{server.base_url}/v1/sessions/{sid}",
            headers={"X-Forwarded-Email": ADMIN_EMAIL},
            timeout=30.0,
        )
        assert owner_view.status_code == 200, (
            f"owner lost access after a grantee left: {owner_view.status_code}"
        )
    finally:
        ctx.close()


def test_owner_gets_delete_in_the_slot_not_leave(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """The owner's destructive slot is Delete, never Leave.

    Leaving would orphan the session, so ownership resolves the one slot to
    Delete instead — the two are mutually exclusive rather than stacked.
    """
    server = multi_user_server
    sid = server.session_id
    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": ADMIN_EMAIL})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{sid}")

        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)
        expect(_row(page, sid)).to_be_visible(timeout=30_000)

        _open_row_menu(page, sid)
        # Delete present proves the menu rendered its lifecycle group at all, so
        # the missing Leave is the ownership branch and not a mis-timed assertion.
        expect(page.get_by_test_id("delete-conversation")).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("leave-conversation")).to_have_count(0)
    finally:
        ctx.close()
