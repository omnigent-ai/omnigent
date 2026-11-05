"""Browser e2e: an archived session opened from search gets a sidebar row.

Setup runs over the API before the page fixture is requested, so a recorded
run starts at the user's first navigation.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until


def _set_session(base_url: str, session_id: str, **fields: object) -> None:
    """PATCH session fields such as ``title`` or ``archived``."""
    response = httpx.patch(f"{base_url}/v1/sessions/{session_id}", json=fields, timeout=10.0)
    response.raise_for_status()


def _archived_on_server(base_url: str, session_id: str) -> bool:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return bool(response.json()["archived"])


def _sidebar(page: Page) -> Locator:
    return page.get_by_role("complementary", name="Conversations", exact=True)


def _open_archived_from_palette(page: Page, base_url: str, title: str, session_id: str) -> None:
    """Search the palette for ``title`` and open its archived match."""
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_role("dialog", name="Command palette", exact=True)
    expect(palette).to_be_visible()
    palette.get_by_role("combobox").fill(title)
    option = palette.get_by_role("option").filter(has_text=title)
    expect(option).to_be_visible(timeout=15_000)
    expect(option.get_by_text("Archived", exact=True)).to_be_visible()
    option.click()
    page.wait_for_url(f"{base_url}/c/{session_id}")
    expect(palette).not_to_be_visible()


def _row(page: Page, title: str) -> Locator:
    return (
        _sidebar(page)
        .get_by_role("listitem")
        .filter(has=page.get_by_role("link", name=title, exact=True))
    )


def test_archived_session_opened_from_search_shows_unarchive_in_sidebar(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Opening an archived search result reveals its sidebar Unarchive control."""
    base_url, session_id = seeded_session
    title = f"Quarterly report draft {uuid.uuid4().hex[:6]}"
    _set_session(base_url, session_id, title=title)
    _set_session(base_url, session_id, archived=True)

    page: Page = request.getfixturevalue("page")
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/")
    expect(_sidebar(page)).to_be_visible(timeout=30_000)
    expect(_sidebar(page).get_by_role("link", name=title, exact=True)).to_have_count(0)

    _open_archived_from_palette(page, base_url, title, session_id)

    row = _row(page, title)
    expect(row).to_be_visible(timeout=15_000)
    expect(row).to_be_in_viewport()
    row.hover()
    unarchive = row.get_by_role("button", name="Unarchive conversation")
    expect(unarchive).to_be_visible()
    unarchive.click()

    # The row's Unarchive PATCH has committed.
    poll_until(lambda: not _archived_on_server(base_url, session_id), timeout_s=15.0)
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    row.hover()
    expect(row.get_by_role("button", name="Archive conversation")).to_be_visible()
