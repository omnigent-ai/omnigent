"""Browser e2e: an archived session's header kebab must offer Unarchive.

After a session is archived, reopening it (via its ``/c/<id>`` URL —
browser history, a shared link, or search) used to still show
"Archive" in the top-bar kebab menu. The sidebar row's context menu
toggles its label ("Archive"/"Unarchive") on ``conversation.archived``;
``HeaderConversationMenu`` rendered an unconditional "Archive" item
that PATCHed ``archived: true`` — so an archived session was offered the
action that was already taken, with no way to unarchive from its own page.

The journey drives the real archive flow first (header kebab → Archive →
redirect home), so the test covers both halves: archiving from the header
works, and the reopened archived session's menu must flip to Unarchive.
"""

from __future__ import annotations

import re
import time

import httpx
from playwright.sync_api import Page, expect


def _wait_archived(base_url: str, session_id: str) -> None:
    """Poll the store until the archive PATCH commits server-side.

    The row leaves the UI optimistically, so without this the re-navigation
    below could race the PATCH and legitimately render a non-archived
    session — a flake, not the bug under test.
    """
    deadline = time.monotonic() + 15.0
    archived = None
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if snapshot.status_code == 200:
            archived = snapshot.json()["archived"]
            if archived is True:
                return
        time.sleep(0.25)
    raise AssertionError(f"session never reported archived=true, got {archived}")


def test_archived_session_header_menu_offers_unarchive(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Reopened archived session's header kebab shows Unarchive, not Archive.

    Fails on the bug: the menu still lists an "Archive" item (and no
    unarchive affordance) even though the session is already archived.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    # 1. Open the session and archive it from the top-bar kebab menu.
    page.goto(f"{base_url}/c/{session_id}")
    trigger = page.get_by_test_id("header-conversation-actions")
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()
    expect(page.get_by_role("menu")).to_be_visible()
    page.get_by_test_id("header-archive-conversation").click()

    # Archiving the active session redirects home; the archive commits in
    # the background.
    page.wait_for_url(f"{base_url}/", timeout=10_000)
    _wait_archived(base_url, session_id)

    # 2. Reopen the archived session at its URL, as a user does from browser
    # history, a link, or search (its sidebar row is gone).
    page.goto(f"{base_url}/c/{session_id}")
    expect(trigger).to_be_visible(timeout=30_000)

    # 3. The kebab menu must offer an unarchive affordance and must not
    # re-offer "Archive" — the session is already archived. The bug rendered
    # an unconditional "Archive" item here.
    trigger.click()
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    expect(
        menu.get_by_role("menuitem", name=re.compile("unarchive", re.IGNORECASE))
    ).to_be_visible()
    expect(menu.get_by_role("menuitem", name="Archive", exact=True)).to_have_count(0)
