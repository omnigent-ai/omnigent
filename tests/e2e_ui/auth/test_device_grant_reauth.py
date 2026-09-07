"""Forced re-authentication on the device-grant consent page (accounts mode).

The device-authorization consent page (`GET /oauth/device`) requires a login
started FOR the current flow: if the browser's session predates the grant, it
bounces through the SPA login page with `?reauth=1`, which forces a fresh
password entry even for an already-signed-in user. This is the anti-phishing
gate — a victim handed a one-click link (code prefilled) can't approve an
attacker-initiated grant by reflex; they must deliberately re-enter their
password against a screen naming the identity and client.

These tests drive the real browser flow end to end against an accounts-mode
server with the device grant enabled (see `_accounts_server.py`):

- `test_forced_reauth_shows_login_form_for_signed_in_user` — a user who is
  ALREADY signed in, following the (prefilled) consent link, is NOT auto-bounced
  to consent; the login form is shown and a fresh password submit is required
  before the Approve screen appears.
- `test_consent_approves_after_fresh_login` — the full happy path: fresh login →
  consent screen names the identity → Approve → "Connected".

Unit coverage for the client-side `?reauth=1` handling lives in
`web/src/pages/LoginPage.test.tsx`; this is the browser-level end-to-end proof.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

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
    """A dedicated accounts-mode server with the device grant enabled."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_device_reauth")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


def _login(page: Page, server: AccountsServer) -> None:
    """Sign in through the SPA login form, establishing a session cookie."""
    page.goto(f"{server.public_url}/login")
    page.locator("#login-username").fill(ADMIN_USERNAME)
    page.locator("#login-password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    # A successful login hard-navigates away from /login.
    expect(page).not_to_have_url(re.compile(r"/login"), timeout=10_000)


def test_forced_reauth_shows_login_form_for_signed_in_user(
    accounts_server: AccountsServer, page: Page
) -> None:
    """An already-signed-in user following the consent link is forced to
    re-authenticate: the login form is shown (no auto-bounce to consent), and
    only a fresh password submit reveals the Approve screen.

    Establishing the session BEFORE minting the grant makes the session ``iat``
    predate the grant's ``created_at`` — the exact condition the consent page
    rejects with a ``reauth=1`` bounce.
    """
    # 1. Sign in first → the browser now holds a valid session cookie whose iat
    #    predates any grant minted afterward.
    _login(page, accounts_server)

    # The gate compares second-granular timestamps (session iat < grant
    # created_at), so pause >1s to guarantee the pre-existing session is
    # strictly older than the grant — i.e. genuinely "already signed in before
    # this flow", not a same-second race. (Server-side test does the same.)
    time.sleep(1.1)

    # 2. Mint the grant AFTER login (as the Slack client would), then follow the
    #    one-click (code-prefilled) consent link — the convenient link is
    #    retained; the re-auth step is the gate.
    flow = accounts_server.start_device_flow()
    page.goto(flow.verification_uri_complete)

    # 3. Despite the valid session, the flow lands on the login page with the
    #    reauth marker — NOT the Approve screen — and the password form is shown
    #    rather than auto-returning to consent.
    expect(page).to_have_url(re.compile(r"/login\?.*reauth=1"), timeout=10_000)
    expect(page.locator("#login-password")).to_be_visible()
    # The consent Approve control is not present yet.
    expect(page.get_by_role("button", name="Approve")).to_have_count(0)

    # 4. A fresh password submit re-authenticates and returns to the consent
    #    page, which now shows the Approve screen naming the acting identity.
    page.locator("#login-username").fill(ADMIN_USERNAME)
    page.locator("#login-password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()

    expect(page).to_have_url(re.compile(r"/oauth/device"), timeout=10_000)
    expect(page.get_by_role("button", name="Approve")).to_be_visible(timeout=10_000)
    expect(page.get_by_text(ADMIN_USERNAME)).to_be_visible()


def test_consent_approves_after_fresh_login(accounts_server: AccountsServer, page: Page) -> None:
    """Happy path: a login started for THIS flow reaches the consent screen and
    approval binds the grant (the page confirms "Connected")."""
    # Mint the grant FIRST, then log in fresh by following the consent link
    # while signed out — the login that results is newer than the grant, so the
    # freshness check passes and the consent screen renders directly.
    flow = accounts_server.start_device_flow()
    page.goto(flow.verification_uri_complete)

    # Signed out → bounced to login (return_to points back at consent).
    expect(page).to_have_url(re.compile(r"/login"), timeout=10_000)
    page.locator("#login-username").fill(ADMIN_USERNAME)
    page.locator("#login-password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()

    # Back on the consent page → approve → the grant is bound.
    approve = page.get_by_role("button", name="Approve")
    expect(approve).to_be_visible(timeout=10_000)
    approve.click()
    expect(page.get_by_text("Connected")).to_be_visible(timeout=10_000)
