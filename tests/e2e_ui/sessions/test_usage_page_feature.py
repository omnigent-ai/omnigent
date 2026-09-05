"""Playwright coverage for the deployment-wide Usage page release feature."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

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


def test_session_table_shows_other_harnesses_badge(
    page: Page,
    live_server: str,
) -> None:
    """A session with sub-agent harnesses shows a '+N' badge."""
    _stub_server_info(page, usage_page=True)
    now = int(time.time())
    report = json.dumps(
        {
            "cost_today": 1.50,
            "cost_last_7d": 1.50,
            "cost_last_30d": 1.50,
            "total_cost_usd": 1.50,
            "daily_costs": [],
            "sessions": [
                {
                    "id": "conv_abc",
                    "created_at": now - 3600,
                    "updated_at": now,
                    "title": "Multi-harness session",
                    "cost_usd": 1.50,
                    "models": {"claude-opus-4-8": 1.50},
                    "harness": "claude-sdk",
                    "other_harnesses": ["antigravity", "openai-agents"],
                    "llm_model": "claude-opus-4-8",
                    "agent_name": None,
                },
            ],
        }
    )

    def handle_usage(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=report)

    page.route("**/v1/usage", handle_usage)
    page.goto(f"{live_server}/usage")

    expect(page.get_by_test_id("usage-nav")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name="Usage", exact=True)).to_be_visible()

    row = page.locator("table tr", has_text="Multi-harness session")
    expect(row).to_be_visible(timeout=10_000)
    expect(row.get_by_text("claude-sdk")).to_be_visible()
    badge = row.get_by_text("+2")
    expect(badge).to_be_visible()
    expect(badge).to_have_attribute("title", "antigravity, openai-agents")


def _setup_usage_page(page: Page, live_server: str) -> None:
    """Stub endpoints and navigate to the usage page."""
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
    expect(page.get_by_role("heading", name="Usage", exact=True)).to_be_visible(timeout=30_000)


def test_custom_date_inputs_have_max_today(page: Page, live_server: str) -> None:
    """Both custom date inputs are capped at today so future dates are blocked."""
    _setup_usage_page(page, live_server)

    page.get_by_role("button", name="Custom").click()

    today_str = datetime.now(tz=timezone.utc).date().isoformat()
    start_input = page.locator('input[type="date"]').first
    end_input = page.locator('input[type="date"]').last

    expect(end_input).to_have_attribute("max", today_str)
    start_max = start_input.get_attribute("max")
    assert start_max is not None and start_max <= today_str


def test_custom_date_end_auto_adjusts_when_start_exceeds_end(page: Page, live_server: str) -> None:
    """Setting start after end auto-corrects the end date forward."""
    _setup_usage_page(page, live_server)

    page.get_by_role("button", name="Custom").click()

    start_input = page.locator('input[type="date"]').first
    end_input = page.locator('input[type="date"]').last

    today = datetime.now(tz=timezone.utc).date()
    new_start = today.isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()

    end_input.fill(yesterday)
    start_input.fill(new_start)

    expect(end_input).to_have_value(new_start)
