"""Browser e2e for copying a session id from the sidebar row menu.

The dashboard needs to expose the durable ``conv_...`` id so a user can
recover a killed native terminal with a CLI resume command. The sidebar row
kebab now includes ``Copy session ID`` immediately below ``Rename``; clicking it
writes the exact conversation id, not the display title or URL, to the
clipboard.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def test_copy_session_id_menu_item_copies_conv_id(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The row kebab copies the durable session id for CLI resume.

    Failure modes this catches:

    - The menu item is absent from the real sidebar or rendered in the wrong
      action cluster.
    - The copy handler writes the row title / URL instead of ``conv_...``.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-copy-id-{uuid.uuid4().hex[:8]}"
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()

    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    row.hover()
    row.get_by_test_id("conversation-actions").click()

    rename = page.get_by_test_id("rename-conversation")
    copy = page.get_by_test_id("copy-conversation-id")
    expect(rename).to_be_visible()
    expect(copy).to_be_visible()
    rename_box = rename.bounding_box()
    copy_box = copy.bounding_box()
    assert rename_box is not None
    assert copy_box is not None
    assert rename_box["y"] < copy_box["y"]

    copy.click()

    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied == session_id
