"""Playwright coverage for the deployment-wide Usage page release feature."""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect


def _stub_server_info(page: Page, *, usage_page: bool) -> None:
    """Advertise one deterministic ``usage_page`` feature value."""
    body = json.dumps(
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "features": {
                "usage_page": usage_page,
                "harness_install": False,
            },
            "harness_install_enabled": False,
            "installable_harnesses": [],
        }
    )

    def handle_info(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/v1/info", handle_info)


def test_usage_page_route_and_navigation_are_absent_when_feature_is_off(
    page: Page,
    live_server: str,
) -> None:
    """A direct deep link cannot bypass the default-off navigation gate."""
    _stub_server_info(page, usage_page=False)

    page.goto(f"{live_server}/usage")

    expect(page.get_by_role("heading", name="Page not found")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("usage-nav")).to_have_count(0)


def test_usage_page_route_and_navigation_are_available_when_feature_is_on(
    page: Page,
    live_server: str,
) -> None:
    """The advertised feature enables both entry points and report rendering."""
    _stub_server_info(page, usage_page=True)
    report = json.dumps(
        {
            "cost_today": 0.0,
            "cost_last_7d": 0.0,
            "cost_last_30d": 0.0,
            "total_cost_usd": 0.0,
            "daily_costs": [],
            "sessions": [],
        }
    )

    def handle_usage(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=report)

    page.route("**/v1/usage", handle_usage)
    page.goto(f"{live_server}/usage")

    expect(page.get_by_test_id("usage-nav")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name="Usage", exact=True)).to_be_visible()
    expect(page.get_by_text("$0.00", exact=True)).to_be_visible()
