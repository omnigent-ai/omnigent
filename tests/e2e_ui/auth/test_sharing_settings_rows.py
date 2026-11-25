"""Session sharing's rows are Settings catalog rows: a link to one opens Sharing at its control.

Settings search sends a chosen result to `/settings/<section>#<row>`, and so does any link to a
row. Sharing only shows for an admin on a server with its own accounts, so these run against a
dedicated accounts-mode server (see `_accounts_server.py`) signed in as its seeded admin.
"""

from __future__ import annotations

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
    server_tmp = tmp_path_factory.mktemp("e2e_ui_sharing_rows")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


@pytest.mark.parametrize(
    ("row", "control"),
    [
        ("sharing-mode", ("combobox", "Session sharing mode")),
        ("public-access", ("switch", "Public access")),
        ("public-max-permission", ("combobox", "Maximum public permission")),
        ("default-public-sessions", ("switch", "Share new sessions by default")),
    ],
    ids=["mode", "public-access", "max-permission", "default-visibility"],
)
def test_a_link_to_a_sharing_row_opens_sharing_at_its_control(
    accounts_server: AccountsServer,
    page: Page,
    row: str,
    control: tuple[str, str],
) -> None:
    sign_in_as_admin(page, accounts_server)
    page.goto(f"{accounts_server.public_url}/settings/sharing#{row}")

    role, name = control
    target = page.locator(f'[data-settings-item="{row}"]').get_by_role(role, name=name)
    expect(target).to_be_focused(timeout=10_000)
