"""OIDC recovery through the production server's public-prefix middleware."""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.auth._oidc_server import OIDCServer, spawn_oidc_server

_BASE_PATH = "/proxy/42"


@pytest.fixture(scope="module")
def base_path_oidc_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[OIDCServer]:
    """Start the actual server with both OIDC and its public prefix configured."""
    yield from spawn_oidc_server(
        mock_llm_server_url,
        tmp_path_factory.mktemp("oidc_base_path"),
        public_client=True,
        base_path=_BASE_PATH,
    )


@pytest.fixture(autouse=True)
def reset_provider_errors(base_path_oidc_server: OIDCServer) -> Iterator[None]:
    """Keep one journey's injected response from affecting another."""
    errors = base_path_oidc_server.idp.authorization_errors
    errors.clear()
    try:
        yield
    finally:
        errors.clear()


@pytest.mark.parametrize("case", ["missing-state", "single-expiry", "repeated-expiry"])
def test_oidc_recovery_stays_under_public_prefix(
    base_path_oidc_server: OIDCServer, page: Page, case: str
) -> None:
    """Recovery, provider callback and authenticated SPA retain the public prefix."""
    server = base_path_oidc_server
    auth_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            auth_requests.append(request.url)
            if urlsplit(request.url).netloc == urlsplit(server.public_url).netloc
            and "/auth/" in urlsplit(request.url).path
            else None
        ),
    )
    if case == "missing-state":
        page.goto(
            f"{server.prefixed_url}/auth/callback"
            "?error=temporarily_unavailable&error_description=authentication_expired"
        )
    else:
        count = 1 if case == "single-expiry" else 2
        server.idp.authorization_errors.extend(
            [("temporarily_unavailable", "authentication_expired")] * count
        )
        page.goto(f"{server.prefixed_url}/")

    if case != "single-expiry":
        expect(page.get_by_role("heading", name="Sign-in unsuccessful")).to_be_visible()
        restart = page.get_by_role("link", name="Sign in again")
        assert urlsplit(restart.get_attribute("href") or "").path == f"{_BASE_PATH}/auth/login"
        restart.click()

    page.locator("#fake-idp-continue").click()
    expect(page.locator('[data-testid="sidebar-brand"]')).to_be_visible(timeout=15_000)
    assert urlsplit(page.url).path.startswith(f"{_BASE_PATH}/")
    assert page.evaluate("window.__OMNIGENT_BASE_PATH__") == _BASE_PATH
    assert (
        page.evaluate("async prefix => (await fetch(`${prefix}/v1/me`)).status", _BASE_PATH) == 200
    )
    assert auth_requests
    assert all(urlsplit(url).path.startswith(f"{_BASE_PATH}/auth/") for url in auth_requests)
