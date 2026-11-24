"""Global policies' rows are Settings catalog rows: a link to one opens Policies at its control.

Settings search sends a chosen result to `/settings/<section>#<row>`, and so does any link to a
row. Policies only shows for an admin, so these run against a dedicated accounts-mode server
(see `_accounts_server.py`) signed in as its seeded admin.
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
    server_tmp = tmp_path_factory.mktemp("e2e_ui_policies_rows")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


@pytest.mark.parametrize(
    ("row", "focused"),
    [
        ("policies-add", "Add policy"),
        # The policy list reveals at its own control, not a policy's switch.
        ("policies-list", "Refresh"),
    ],
    ids=["add-policy", "policy-list"],
)
def test_a_link_to_a_policies_row_opens_policies_at_its_control(
    accounts_server: AccountsServer,
    page: Page,
    row: str,
    focused: str,
) -> None:
    sign_in_as_admin(page, accounts_server)
    page.goto(f"{accounts_server.public_url}/settings/policies#{row}")

    target = page.locator(f'[data-settings-item="{row}"]').get_by_role("button", name=focused)
    expect(target).to_be_focused(timeout=10_000)


def test_adding_from_the_revealed_row_offers_the_servers_policies_then_their_settings(
    accounts_server: AccountsServer, page: Page
) -> None:
    sign_in_as_admin(page, accounts_server)
    page.goto(f"{accounts_server.public_url}/settings/policies#policies-add")
    expect(page.get_by_role("button", name="Add policy")).to_be_focused(timeout=10_000)
    page.keyboard.press("Enter")

    dialog = page.get_by_role("dialog", name="Add Global Policy")
    expect(dialog.get_by_role("textbox", name="Filter policies")).to_be_focused()
    first_policy = dialog.locator('[data-settings-row-action="choose"]').first
    expect(first_policy).to_be_visible()
    first_policy.click()

    expect(dialog.get_by_role("textbox", name="Policy name")).to_have_value(re.compile(r".+"))
    # Cancel steps back to the server's policies rather than closing.
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog.get_by_role("textbox", name="Filter policies")).to_be_visible()
