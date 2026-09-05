"""E2E: a backend-rejected session rename must surface visible failure feedback.

A sidebar rename is optimistic: ``useRenameConversation`` paints the new title
into every cached list in ``onMutate``, then ``PATCH /v1/sessions/{id}``
round-trips. When the PATCH fails, ``onError`` rolls the title back -- but,
unlike a failed delete (which surfaces a persistent toast), it surfaces
*nothing*: the row flickers to the new name and back with no toast and no
validation message, so the user has no idea the rename was rejected.

Deployments whose conversation-store backend restricts title characters hit
this for real: renaming a session to a title containing ``/`` is rejected by
the storage layer (``INVALID_PARAMETER_VALUE: Workspace items cannot contain
the '/' character``) and the UI silently reverts. This test reproduces that
journey against the live server by fulfilling the rename PATCH with that exact
production rejection, then asserts what the user must observe:

- the row does not keep the rejected title (rollback -- correct today), and
- a visible failure signal appears (toast or alert -- MISSING today, the bug
  this test guards), and
- the server never persisted the rejected title.

The interception is asserted (``expect_response`` sees the 400 and the handler
counts exactly one rejected PATCH) so the test cannot pass or fail vacuously
without the rename ever leaving the client.
"""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import Page, Route, expect

# A realistic user title that trips backends rejecting '/' in workspace items.
_REJECTED_TITLE = "release notes/2026-09"

# Mirrors the production storage-layer rejection observed in server logs.
_BACKEND_REJECTION = {
    "error_code": "INVALID_PARAMETER_VALUE",
    "message": "Workspace items cannot contain the '/' character",
}


def _reject_title_patches(page: Page, session_id: str) -> list[int]:
    """Fulfill title-carrying ``PATCH /v1/sessions/{session_id}`` with a 400.

    Only PATCH requests whose body carries a ``title`` are rejected -- every
    other request (session snapshot GETs, pin/label PATCHes) passes through,
    so the page otherwise behaves exactly like the real deployment.

    :param page: Playwright page, routed before the rename is committed.
    :param session_id: Session whose rename PATCH is rejected.
    :returns: Single-element mutable counter of rejected PATCHes, so the
        caller can assert the fault actually fired.
    """
    rejected = [0]

    def _handle(route: Route) -> None:
        request = route.request
        if request.method != "PATCH" or '"title"' not in (request.post_data or ""):
            route.continue_()
            return
        rejected[0] += 1
        route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps(_BACKEND_REJECTION),
        )

    page.route(f"**/v1/sessions/{session_id}", _handle)
    return rejected


def test_backend_rejected_rename_shows_failure_feedback(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A rename the backend rejects must not fail silently.

    Journey: open the session -> sidebar row kebab -> Rename -> type a
    ``/``-containing title -> Enter. The backend rejects the PATCH (400,
    ``INVALID_PARAMETER_VALUE``). The user must see failure feedback; today
    the title just flickers back with no signal at all.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    server_title_before = snap.json().get("title")

    rejected = _reject_title_patches(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    row = page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    expect(row).to_be_visible()
    link = page.locator(f'a[href="/c/{session_id}"]')

    # Open the row kebab and pick Rename. Hover first so the desktop
    # hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("rename-conversation").click()
    edit = page.get_by_test_id("rename-conversation-input")
    expect(edit).to_be_visible()
    edit.fill(_REJECTED_TITLE)

    # Commit the rename and require the rejected PATCH to actually round-trip,
    # so the assertions below can't pass or fail without the fault firing.
    with page.expect_response(
        lambda r: r.request.method == "PATCH" and f"/v1/sessions/{session_id}" in r.url
    ) as patch_info:
        edit.press("Enter")
    assert patch_info.value.status == 400, (
        f"rename PATCH should have been rejected with 400, got {patch_info.value.status}"
    )
    assert rejected[0] == 1, f"expected exactly one rejected rename PATCH, saw {rejected[0]}"

    # Rollback: the row must not keep the rejected title. This auto-retries
    # through the optimistic flicker (new name paints, then reverts).
    expect(link).not_to_contain_text(_REJECTED_TITLE)

    # THE BUG: the rejection must be visible to the user. The repo-wide
    # pattern for row mutations whose editor has already unmounted is a toast
    # (see the failed-delete toast); an inline alert would also satisfy this.
    feedback = page.get_by_test_id("toast").or_(page.get_by_role("alert")).first
    expect(feedback).to_be_visible(timeout=10_000)

    # And the server agrees the rename never landed -- the rejected title must
    # not have been persisted by some other path.
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    assert snap.json().get("title") == server_title_before, (
        f"rejected title must not persist; server title changed from "
        f"{server_title_before!r} to {snap.json().get('title')!r}"
    )
