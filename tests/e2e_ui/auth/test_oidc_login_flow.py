"""E2E: the OIDC/SSO login journey, driven headlessly against a fake IdP.

An unauthenticated SPA navigation in OIDC mode bounces to the IdP's sign-in
page and, after the user authenticates, lands back in the app authenticated.
Deployments use a real SSO provider, so this journey can't be filmed against
the live app — the recorder would be bounced to real SSO. This test wires the
server to a fake in-process IdP (:mod:`tests.e2e_ui.auth._fake_idp`) so the
whole redirect chain runs locally and headlessly:

    SPA → /v1/me 401 (login_url) → /auth/login → 302 fake IdP /authorize
        → "Continue" → /auth/callback (code+state, signed id_token) → app

This is the reproduction lane for auth/OIDC login bugs (T3): the recorded video
shows the real sign-in page and the authenticated landing, not a proxy.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.auth._oidc_server import OIDCServer, spawn_oidc_server


@pytest.fixture(scope="module")
def oidc_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[OIDCServer]:
    """A dedicated OIDC-mode server wired to a fake IdP."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_oidc_login")
    yield from spawn_oidc_server(mock_llm_server_url, server_tmp)


def test_oidc_login_redirects_through_idp_to_authenticated_app(
    oidc_server: OIDCServer, page: Page
) -> None:
    """Navigating the SPA unauthenticated lands on the IdP sign-in page;
    continuing there returns to the app authenticated.

    Drives the full OIDC redirect chain against the fake IdP and asserts both
    ends a user observes: the sign-in page (naming the identity) and the
    authenticated app shell (the composer).
    """
    # 1. Land on the SPA unauthenticated. The client probes /v1/me, gets a 401
    #    with login_url, and redirects the browser to /auth/login, which 302s
    #    to the fake IdP's /authorize page. Playwright follows the 302s.
    page.goto(oidc_server.public_url)

    # 2. The fake IdP sign-in page is shown, naming the identity to sign in as.
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    expect(continue_link).to_contain_text(oidc_server.idp.email)

    # 3. Continue through the IdP → /auth/callback exchanges the code for a
    #    signed id_token, mints the session cookie, and 302s back to the app.
    continue_link.click()

    # 4. Back in the authenticated app: no longer on any auth page, and the
    #    app shell (the sidebar, which renders only for an authenticated
    #    session) is shown. Assert on the shell rather than the composer, since
    #    the post-login landing route need not be a chat with a composer.
    expect(page).not_to_have_url(re.compile(r"/authorize|/auth/login"), timeout=15_000)
    expect(page.locator('[data-testid="sidebar-brand"]')).to_be_visible(timeout=15_000)
