"""Members' rows are Settings catalog rows: a link to one opens Members at its control.

Settings search sends a chosen result to `/settings/<section>#<row>`, and so does any link to a
row. Members only shows for an admin on a server with its own accounts, so these run against a
dedicated accounts-mode server (see `_accounts_server.py`) signed in as its seeded admin.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.auth._accounts_server import (
    AccountsServer,
    sign_in_as_admin,
    spawn_accounts_server,
)


@pytest.fixture(scope="module")
def accounts_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AccountsServer]:
    """A dedicated accounts-mode server with a seeded admin."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_members_search")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


@pytest.mark.parametrize(
    ("row", "focused"),
    [
        ("members-invite", "Invite member"),
        # The member list reveals at its own control, not a member's row action.
        ("members-list", "Refresh"),
    ],
    ids=["invite-member", "member-list"],
)
def test_a_link_to_a_members_row_opens_members_at_its_control(
    accounts_server: AccountsServer,
    page: Page,
    row: str,
    focused: str,
) -> None:
    sign_in_as_admin(page, accounts_server)
    page.goto(f"{accounts_server.public_url}/settings/members#{row}")

    target = page.locator(f'[data-settings-item="{row}"]').get_by_role("button", name=focused)
    expect(target).to_be_focused(timeout=10_000)


def test_inviting_from_the_revealed_row_shows_the_link_once_with_its_copy_button(
    accounts_server: AccountsServer, page: Page
) -> None:
    sign_in_as_admin(page, accounts_server)
    page.goto(f"{accounts_server.public_url}/settings/members#members-invite")
    invite = page.get_by_role("button", name="Invite member")
    expect(invite).to_be_focused(timeout=10_000)
    page.keyboard.press("Enter")

    page.get_by_role("dialog", name="Invite a member").get_by_role(
        "button", name="Create invite"
    ).click()
    link = page.get_by_role("dialog", name="Invite URL")
    expect(link.get_by_role("textbox", name="Invite URL")).to_have_value(
        re.compile(r"/register\?")
    )
    expect(link.get_by_role("button", name="Copy invite URL")).to_be_visible()
