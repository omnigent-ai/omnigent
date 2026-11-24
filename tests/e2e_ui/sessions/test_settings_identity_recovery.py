"""A mounted settings view follows identity recovery without another navigation."""

import re
from collections.abc import Iterator
from contextlib import ExitStack

import pytest
from playwright.sync_api import Page, Route, expect

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
    server_tmp = tmp_path_factory.mktemp("settings_identity_recovery")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


@pytest.mark.parametrize(
    ("settings_path", "action", "heading"),
    [("members", "Invite member", "Members"), ("policies", "Add policy", "Global Policies")],
)
def test_settings_recover_identity_while_the_view_stays_open(
    accounts_server: AccountsServer, page: Page, settings_path: str, action: str, heading: str
) -> None:
    page.goto(f"{accounts_server.public_url}/login")
    page.locator("#login-username").fill(ADMIN_USERNAME)
    page.locator("#login-password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page).not_to_have_url(re.compile(r"/login"), timeout=10_000)

    outage = {"active": True}

    def identity_probe(route: Route) -> None:
        if outage["active"]:
            route.fulfill(status=403, json={"detail": "Temporary identity outage"})
        else:
            route.fallback()

    page.route("**/v1/me", identity_probe)

    def policies_probe(route: Route) -> None:
        if outage["active"]:
            route.fulfill(status=403, json={"detail": "Temporary identity outage"})
        else:
            route.fallback()

    page.route("**/v1/policies", policies_probe)
    url = f"{accounts_server.public_url}/settings/{settings_path}"
    with page.expect_response(
        lambda response: response.url.endswith("/v1/me") and response.status == 403
    ):
        page.goto(url)
    control = page.get_by_role("button", name=action, exact=True)
    expect(control).not_to_be_visible()
    page.evaluate("document.documentElement.dataset.identityRecoveryMarker = 'kept'")

    with ExitStack() as recovery:
        recovery.enter_context(
            page.expect_response(
                lambda response: response.url.endswith("/v1/me") and response.status == 200,
                timeout=60_000,
            )
        )
        if settings_path == "policies":
            recovery.enter_context(
                page.expect_response(
                    lambda response: (
                        response.url.endswith("/v1/policies") and response.status == 200
                    ),
                    timeout=60_000,
                )
            )
        outage["active"] = False

    expect(control).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    expect(page).to_have_url(url)
    assert page.evaluate("document.documentElement.dataset.identityRecoveryMarker") == "kept"
