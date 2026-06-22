"""Browser e2e for the sidebar's session groups.

Groups group conversations under named, collapsible sidebar sections.
Membership is stored server-side as a ``conversation_labels`` row with the
reserved key ``"group"`` (no new table — see
``sqlalchemy_store.list_groups`` / the ``group`` filter on
``list_conversations``). The web UI moves a session via the row kebab's
**"Move to group"** submenu (``data-testid="move-to-group"``), which
calls ``PATCH /v1/sessions/{id}`` with ``{labels:{group}}`` (an empty
value removes the label).

These drive the real chain the ``Sidebar`` unit tests mock out: the kebab
submenu → the PATCH → the refreshed ``GET /v1/sessions/groups`` and
``GET /v1/sessions`` lists → the row landing under (or leaving) a group
section. Groups render collapsed by default, so the tests expand the
section to assert membership.
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title via ``PATCH /v1/sessions/{id}`` so its row
    is easy to spot among other tests' sessions in the shared server."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar ``<section>`` whose collapse-header button reads
    *title* (e.g. "Recent" or a group name). The per-section count is
    ``aria-hidden``, so the header's accessible name stays the bare title."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _move_to_new_group(page: Page, row: Locator, name: str) -> None:
    """Drive the row kebab → "Move to group" → "New group…" flow,
    typing *name* and committing with Enter."""
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    # Open the submenu flyout, then start the inline new-group input.
    page.get_by_test_id("move-to-group").click()
    page.get_by_role("menuitem", name="New group…").click()
    new_input = page.get_by_placeholder("Group name…")
    new_input.fill(name)
    new_input.press("Enter")


def test_move_session_into_new_group(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Creating a group from the kebab moves the row into it.

    The session starts under "Recent"; after "Move to group → New
    group…", a group section with that name appears and the row
    lives under it (once expanded) and no longer under "Recent".
    """
    base_url, session_id = seeded_session
    title = f"e2e-col-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    group = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_section(page, "Recent").locator(f'a[href="/c/{session_id}"]')).to_be_visible()

    _move_to_new_group(page, row, group)

    # The group header appears; groups render collapsed by default,
    # so expand it before asserting membership.
    header = page.get_by_role("button", name=group, exact=True)
    expect(header).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", "false")
    header.click()

    expect(_section(page, group).locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Recent").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)


def test_remove_session_from_group(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Removing a session from its group drops it back under "Recent".

    Moves the row into a fresh group first, then uses the kebab's
    "Remove from group" item and asserts the row returns to "Recent".
    """
    base_url, session_id = seeded_session
    title = f"e2e-col-rm-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    group = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    _move_to_new_group(page, row, group)

    header = page.get_by_role("button", name=group, exact=True)
    expect(header).to_be_visible()
    header.click()

    # Remove via the kebab's "Remove from group" item (only shown when the
    # session is in a group).
    group_row = (
        _section(page, group)
        .locator("li")
        .filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    )
    group_row.hover()
    group_row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("move-to-group").click()
    page.get_by_role("menuitem", name=re.compile("Remove from group")).click()

    # Back under "Recent", and the now-empty group section is gone.
    expect(_section(page, "Recent").locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(page.get_by_role("button", name=group, exact=True)).to_have_count(0)
