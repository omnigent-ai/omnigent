"""A failed sign-out keeps the user signed in and says so (accounts mode).

When the server cannot record a logout in its shared database it answers
``POST /auth/logout`` with ``503`` and keeps the session cookie, so the session
is still live on every replica and the user can retry. The Settings → Account
"Sign out" button must then show the error and stay put rather than land on the
login page as if sign-out had worked; once the server recovers, the same button
signs out for real.

The 503 is injected at the browser's network layer (``page.route``): a real
database write failure can't be triggered from outside the server process. The
server side of the contract (503, cookie kept, retry succeeds) is covered in
``tests/server/test_session_renewal.py``; the component-level handling in
``web/src/pages/SettingsPage.test.tsx``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.server.auth import LOGOUT_NOT_RECORDED_MESSAGE
from tests.e2e_ui.auth._accounts_server import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    AccountsServer,
    spawn_accounts_server,
)


@pytest.fixture(scope="module")
def accounts_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AccountsServer]:
    """A dedicated accounts-mode server with a seeded admin."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_sign_out_failure")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


def _login(page: Page, server: AccountsServer) -> None:
    page.goto(f"{server.public_url}/login")
    page.locator("#login-username").fill(ADMIN_USERNAME)
    page.locator("#login-password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page).not_to_have_url(re.compile(r"/login"), timeout=10_000)


def test_failed_sign_out_keeps_user_signed_in_until_retry(
    accounts_server: AccountsServer, page: Page
) -> None:
    """503 → error shown, still signed in; retry after recovery → login page."""
    _login(page, accounts_server)
    page.goto(f"{accounts_server.public_url}/settings/account")
    # "Change password" only renders once the server reports accounts mode, so the
    # Sign out click below takes the accounts (POST) path, not the OIDC redirect.
    expect(page.get_by_role("button", name="Change password")).to_be_visible(timeout=10_000)
    sign_out = page.get_by_role("button", name="Sign out")

    def unrecorded_logout(route: Route) -> None:
        route.fulfill(
            status=503,
            content_type="application/json",
            json={"error": LOGOUT_NOT_RECORDED_MESSAGE},
        )

    page.route("**/auth/logout", unrecorded_logout)
    sign_out.click()

    expect(page.get_by_role("alert")).to_contain_text("Sign-out failed", timeout=10_000)
    expect(page).to_have_url(re.compile(r"/settings/account"))
    # Still signed in: a reload keeps the account page instead of bouncing to login.
    page.reload()
    expect(page.get_by_role("button", name="Change password")).to_be_visible(timeout=10_000)
    expect(page.get_by_role("main")).to_contain_text(ADMIN_USERNAME)
    expect(page).to_have_url(re.compile(r"/settings/account"))

    # The server recovers: the same button now signs out for real.
    page.unroute("**/auth/logout", unrecorded_logout)
    page.get_by_role("button", name="Sign out").click()

    expect(page).to_have_url(re.compile(r"/login"), timeout=10_000)
    page.goto(f"{accounts_server.public_url}/settings/account")
    expect(page).to_have_url(re.compile(r"/login"), timeout=10_000)
