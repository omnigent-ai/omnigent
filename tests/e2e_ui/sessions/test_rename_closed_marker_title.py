"""E2E: a user title containing ``:closed:`` must not close the session.

``sys_session_close`` historically freed a closed child's unique title
slot by appending ``:closed:<child id>`` to its title, and the server
still reads that marker back out of stored titles as closed state via a
bare substring match. A sidebar rename round-trips the user's title
verbatim through ``PATCH /v1/sessions/{id}``, so a title merely
*containing* ``:closed:`` is read back as internal state: the display
title is truncated at the marker, API responses synthesize
``omnigent.closed=true``, and every later message is refused with
``409 Session is closed`` — silently, since the rename itself succeeds.

Journey (real SPA against the live server): rename a session from the
sidebar row kebab to ``notes about a :closed: door``, reload so the
sidebar and session snapshot refetch server state, then keep using the
session. Expected: the row shows the title as written and the session
still accepts messages.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, expect

_TITLE = "notes about a :closed: door"
_CLOSED_LABEL = "omnigent.closed"
_FOLLOWUP = "still here after the rename"


def _rename_via_sidebar(page: Page, base_url: str, session_id: str, title: str) -> None:
    """Rename *session_id* to *title* through the sidebar row kebab.

    Waits for the rename ``PATCH`` to round-trip and requires it to have
    been accepted, so the assertions that follow observe persisted
    server state rather than an optimistic cache paint.

    :param page: Playwright page fixture (fresh context per test).
    :param base_url: Live server base URL.
    :param session_id: Session to rename.
    :param title: Exact title to commit.
    """
    page.goto(f"{base_url}/c/{session_id}")
    row = page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    expect(row).to_be_visible()

    # Hover first so the desktop hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("rename-conversation").click()
    edit = page.get_by_test_id("rename-conversation-input")
    expect(edit).to_be_visible()
    edit.fill(title)
    with page.expect_response(
        lambda r: (
            r.request.method == "PATCH" and urlparse(r.url).path == f"/v1/sessions/{session_id}"
        )
    ) as patch_info:
        edit.press("Enter")
    assert patch_info.value.status == 200, (
        f"rename PATCH should be accepted, got {patch_info.value.status}"
    )


def test_rename_with_closed_infix_preserves_title(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The renamed title must display and persist exactly as written.

    Today the server treats everything after ``:closed:`` as a legacy
    close marker: the row re-renders as ``notes about a`` after the
    reload and the session snapshot returns the truncated title.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _rename_via_sidebar(page, base_url, session_id, _TITLE)

    # Reload: the sidebar refetches GET /v1/sessions, so the row now
    # renders the server's view of the title, not the optimistic paint.
    page.reload()
    link = page.locator(f'a[href="/c/{session_id}"]')
    expect(link).to_be_visible()
    expect(link).to_contain_text(_TITLE)

    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    assert snap.json().get("title") == _TITLE, (
        f"server should return the title as written {_TITLE!r}, got {snap.json().get('title')!r}"
    )


def test_rename_with_closed_infix_keeps_session_open(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A session renamed to a ``:closed:``-carrying title stays writable.

    Today the synthesized ``omnigent.closed=true`` label disables the
    composer ("This sub-agent session is closed") and the events route
    refuses new messages with a 409, so the session silently stops
    accepting input.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _rename_via_sidebar(page, base_url, session_id, _TITLE)

    page.reload()
    link = page.locator(f'a[href="/c/{session_id}"]')
    expect(link).to_be_visible()
    # The row text confirms the sessions list (labels included) has
    # rendered, so the composer state below reflects server state.
    expect(link).to_contain_text("notes about a")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_editable()

    composer.fill(_FOLLOWUP)
    with page.expect_response(
        lambda r: (
            r.request.method == "POST"
            and urlparse(r.url).path == f"/v1/sessions/{session_id}/events"
        )
    ) as post_info:
        page.get_by_role("button", name="Send", exact=True).click()
    assert post_info.value.status < 400, (
        f"message after the rename should be accepted, got {post_info.value.status}: "
        f"{post_info.value.text()[:200]}"
    )
    expect(page.get_by_text(_FOLLOWUP).first).to_be_visible()

    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    labels = snap.json().get("labels") or {}
    assert labels.get(_CLOSED_LABEL) is None, (
        f"a user rename must not mark the session closed; labels: {labels!r}"
    )
