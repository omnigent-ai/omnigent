"""Archived sessions is a Settings catalog row: a link to it, or to one of its parts, opens
Archived at that control, and a session's Delete asks before it deletes.

Settings search sends a chosen result to `/settings/<section>#<row>[:<part>]`, and so does any
link to a row. Each journey seeds its own archived session with a unique title.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.sessions._archived_sessions import delete_sessions, seed_archived_session


@pytest.fixture
def archived_title(live_server: str) -> Iterator[str]:
    """One archived session, deleted afterwards if the journey didn't."""
    title = f"e2e-archived-rows-{uuid.uuid4().hex[:6]}"
    session_id = seed_archived_session(live_server, title=title, project=None)
    yield title
    delete_sessions(live_server, [session_id])


@pytest.mark.parametrize(
    ("target", "focused"),
    [
        # The row reveals at its declared control, the expiry select.
        ("archived-sessions", "Mark archived sessions as expired after"),
        ("archived-sessions:exitSelection", "Mark archived sessions as expired after"),
        ("archived-sessions:retention", "Mark archived sessions as expired after"),
        # Select shows only once the archived list arrives, and takes focus over then.
        ("archived-sessions:select", "Select"),
    ],
    ids=["row", "hidden-part-falls-back", "part", "part-shown-once-loaded"],
)
def test_a_link_to_archived_sessions_opens_it_at_its_control(
    page: Page, live_server: str, archived_title: str, target: str, focused: str
) -> None:
    page.goto(f"{live_server}/settings/archived#{target}")

    row = page.locator('[data-settings-item="archived-sessions"]')
    expect(row.get_by_test_id("archived-row").filter(has_text=archived_title)).to_be_visible()
    expect(row.get_by_role(_role(focused), name=focused, exact=True)).to_be_focused(timeout=10_000)


def test_deleting_an_archived_session_asks_first_and_cancel_keeps_it(
    page: Page, live_server: str, archived_title: str
) -> None:
    page.goto(f"{live_server}/settings/archived#archived-sessions")
    session_row = page.get_by_test_id("archived-row").filter(has_text=archived_title)
    session_row.hover()
    session_row.get_by_role("button", name="Delete session").click()

    dialog = page.get_by_role("dialog", name="Delete session?")
    expect(dialog).to_contain_text(archived_title)
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    expect(session_row).to_be_visible()


def _role(name: str) -> str:
    """The expiry select is a combobox; every other target here is a button."""
    return "combobox" if name == "Mark archived sessions as expired after" else "button"
