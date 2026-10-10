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
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.auth._oidc_server import OIDCServer, spawn_oidc_server


@pytest.fixture(
    scope="module", params=[False, True], ids=["discovery-confidential", "explicit-public-ps256"]
)
def oidc_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[OIDCServer]:
    """A dedicated OIDC-mode server wired to a fake IdP."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_oidc_login")
    yield from spawn_oidc_server(mock_llm_server_url, server_tmp, public_client=request.param)


@pytest.fixture(autouse=True)
def provider_errors(oidc_server: OIDCServer) -> Iterator[list[tuple[str, str]]]:
    """Isolate injected provider responses between browser journeys."""
    errors = oidc_server.idp.authorization_errors
    errors.clear()
    try:
        yield errors
    finally:
        errors.clear()


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


@pytest.mark.parametrize("expired", [False, True], ids=["normal", "expired"])
def test_oidc_cli_ticket_completes_through_browser(
    oidc_server: OIDCServer,
    provider_errors: list[tuple[str, str]],
    page: Page,
    expired: bool,
) -> None:
    """The CLI ticket authenticates once, including recovery from provider expiry."""
    if expired:
        provider_errors.append(("temporarily_unavailable", "authentication_expired"))
    response = page.request.post(f"{oidc_server.base_url}/auth/cli-login")
    assert response.status == 200
    ticket = response.json()
    page.goto(f"{oidc_server.public_url}{ticket['login_url']}")
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    continue_link.click()
    expect(page.get_by_role("heading", name="Login successful")).to_be_visible(timeout=15_000)
    poll = page.request.get(
        f"{oidc_server.base_url}/auth/cli-poll", params={"ticket": ticket["ticket"]}
    )
    assert poll.status == 200
    assert poll.json()["user_id"] == oidc_server.idp.email
    assert poll.json()["token"]
    replay = page.request.get(
        f"{oidc_server.base_url}/auth/cli-poll", params={"ticket": ticket["ticket"]}
    )
    assert replay.status == 410


def test_oidc_expiry_retries_once_then_signs_in(
    oidc_server: OIDCServer, provider_errors: list[tuple[str, str]], page: Page
) -> None:
    """A provider expiry restarts authorization with fresh state and PKCE."""
    provider_errors.append(("temporarily_unavailable", "authentication_expired"))
    authorizations: list[str] = []
    page.on(
        "request",
        lambda request: (
            authorizations.append(request.url)
            if urlsplit(request.url).path.endswith("/authorize")
            else None
        ),
    )
    page.goto(oidc_server.public_url)
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    assert len(authorizations) == 2
    first, second = [parse_qs(urlsplit(url).query) for url in authorizations]
    assert first["state"] != second["state"]
    assert first["code_challenge"] != second["code_challenge"]
    continue_link.click()
    expect(page.locator('[data-testid="sidebar-brand"]')).to_be_visible(timeout=15_000)
    assert page.evaluate("async () => (await fetch('/v1/me')).status") == 200


@pytest.mark.parametrize(
    ("injected_errors", "message"),
    [
        pytest.param(
            (("temporarily_unavailable", "authentication_expired"),) * 2,
            "Your sign-in session expired.",
            id="repeated-expiry",
        ),
        pytest.param(
            (("access_denied", "private-provider-detail"),),
            "Sign-in could not be completed at the identity provider.",
            id="provider-denial",
        ),
    ],
)
def test_oidc_provider_error_offers_manual_restart(
    oidc_server: OIDCServer,
    provider_errors: list[tuple[str, str]],
    page: Page,
    tmp_path: Path,
    injected_errors: tuple[tuple[str, str], ...],
    message: str,
) -> None:
    """Repeated expiry or denial offers a safe, usable manual restart."""
    provider_errors.extend(injected_errors)
    page.goto(oidc_server.public_url)
    expect(page.get_by_role("heading", name="Sign-in unsuccessful")).to_be_visible()
    expect(page.get_by_text(message, exact=True)).to_be_visible()
    assert provider_errors == []
    body = page.locator("body").inner_text()
    for _, description in injected_errors:
        assert description not in body
    page.screenshot(path=str(tmp_path / "oidc-recovery.png"))
    assert page.evaluate("async () => (await fetch('/v1/me')).status") == 401
    restart = page.get_by_role("link", name="Sign in again")
    expect(restart).to_be_visible()
    restart.click()
    page.locator("#fake-idp-continue").click()
    expect(page.locator('[data-testid="sidebar-brand"]')).to_be_visible(timeout=15_000)
    assert page.evaluate("async () => (await fetch('/v1/me')).status") == 200


def test_oidc_missing_state_recovery_starts_new_browser_login(
    oidc_server: OIDCServer, page: Page, tmp_path: Path
) -> None:
    """A callback without state offers a fresh browser sign-in."""
    page.goto(
        f"{oidc_server.public_url}/auth/callback"
        "?error=temporarily_unavailable&error_description=authentication_expired"
    )
    expect(page.get_by_role("heading", name="Sign-in unsuccessful")).to_be_visible()
    expect(
        page.get_by_text("If you started from the Omnigent app, return to it and try again.")
    ).to_be_visible()
    page.screenshot(path=str(tmp_path / "oidc-unverified-recovery.png"))
    page.get_by_role("link", name="Sign in again").click()
    page.locator("#fake-idp-continue").click()
    expect(page.locator('[data-testid="sidebar-brand"]')).to_be_visible(timeout=15_000)
