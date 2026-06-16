"""Browser e2e for renaming a session from the sidebar.

The row kebab's "Rename" item swaps the row for an inline edit field
(``data-testid="rename-conversation-input"``); committing it (Enter)
fires ``PATCH /v1/sessions/{id}`` with the new title. The rename is
persisted server-side, so it must survive a full page reload — that's
the regression this guards: a rename that only patches the in-memory
TanStack cache (and is lost on reload) would pass the ``Sidebar`` unit
tests, which mock the mutation, but fail here.

We assert persistence two ways after a reload: the row re-renders with
the new title from a fresh ``GET /v1/sessions``, and the server's
own ``GET /v1/sessions/{id}`` snapshot returns it.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def test_rename_session_is_preserved(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Renaming via the kebab persists across a reload and on the server.

    Failure modes this catches that the mocked unit test can't:

    - The PATCH never fires (or 4xxs on wire drift) so the title reverts
      on reload.
    - The rename only patches the client cache and is lost once the
      sidebar refetches ``GET /v1/sessions`` after a reload.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    new_title = f"e2e-renamed-{uuid.uuid4().hex[:8]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    # Open the row kebab and pick Rename. Hover first so the desktop
    # hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("rename-conversation").click()

    # The inline edit field replaces the row; type the new title + Enter.
    edit = page.get_by_test_id("rename-conversation-input")
    expect(edit).to_be_visible()
    edit.fill(new_title)
    edit.press("Enter")

    # The row reflects the new title immediately (optimistic cache patch).
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_contain_text(new_title)

    # Reload: the sidebar refetches GET /v1/sessions from scratch. A
    # rename that only lived in the client cache would revert here.
    page.reload()
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_contain_text(new_title)

    # And the server agrees — the rename was persisted, not just rendered.
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    assert snap.json().get("title") == new_title, (
        f"server should persist the renamed title {new_title!r}, got {snap.json().get('title')!r}"
    )
