"""The web UI reports how long the user has been idle on every API request.

The server renews a browser session only up to the user's last interaction
plus the idle window, using the ``X-Omnigent-Activity-Age`` header the SPA
attaches to every API request (see ``web/src/lib/activity.ts``). Background
polling therefore keeps a tab connected but not signed in forever.

This drives the real SPA against an accounts-mode server with a fake browser
clock: requests fired by background timers carry an age that grows with idle
time, and an interaction resets it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Request, expect

from tests.e2e_ui.auth._accounts_server import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    AccountsServer,
    spawn_accounts_server,
)

_HEADER = "x-omnigent-activity-age"


@pytest.fixture(scope="module")
def accounts_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AccountsServer]:
    """A dedicated accounts-mode server with a seeded admin."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_activity_age")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


def _api_ages(requests: list[Request]) -> list[int]:
    ages = []
    for request in requests:
        if "/v1/" not in request.url:
            continue
        value = request.headers.get(_HEADER)
        assert value is not None and value.isdigit(), (request.url, value)
        ages.append(int(value))
    return ages


def test_api_requests_report_idle_time_and_interaction_resets_it(
    accounts_server: AccountsServer, page: Page
) -> None:
    """Background polls carry a growing age; a click brings it back to ~0."""
    page.clock.install()
    page.goto(f"{accounts_server.public_url}/login")
    page.locator("#login-username").fill(ADMIN_USERNAME)
    page.locator("#login-password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page).not_to_have_url(re.compile(r"/login"), timeout=10_000)

    page.wait_for_load_state("networkidle")
    seen: list[Request] = []
    page.on("request", lambda request: seen.append(request))
    # Let the post-login burst go out first. The ages count from the real
    # typing and click on the login form; the page load itself doesn't count.
    page.clock.fast_forward("01:00")
    page.wait_for_timeout(1_500)
    seen.clear()

    # Idle for ten minutes: only background polls (sidebar, hosts) fire requests.
    page.clock.fast_forward("05:00")
    with page.expect_request(lambda r: "/v1/" in r.url, timeout=30_000):
        page.clock.fast_forward("05:00")
    page.wait_for_timeout(1_000)
    idle_ages = _api_ages(seen)
    assert idle_ages and max(idle_ages) >= 600, idle_ages

    # The user presses a key; the next poll reports a fresh interaction.
    seen.clear()
    page.keyboard.press("Shift")
    with page.expect_request(lambda r: "/v1/" in r.url, timeout=30_000):
        page.clock.fast_forward("01:30")
    page.wait_for_timeout(1_000)
    active_ages = _api_ages(seen)
    assert active_ages and max(active_ages) <= 95, active_ages
