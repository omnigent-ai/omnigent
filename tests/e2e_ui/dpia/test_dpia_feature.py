"""Playwright coverage for the default-off DPIA investigation desk."""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import Page, Route, expect


def _stub_server_info(page: Page, *, dpia: bool) -> None:
    body = json.dumps(
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "features": {
                "usage_page": False,
                "harness_install": False,
                "dpia": dpia,
            },
            "harness_install_enabled": False,
            "installable_harnesses": [],
        }
    )

    def handle_info(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/v1/info", handle_info)


def test_dpia_route_and_navigation_are_absent_when_feature_is_off(
    page: Page,
    live_server: str,
) -> None:
    _stub_server_info(page, dpia=False)

    page.goto(f"{live_server}/dpia")

    expect(page.get_by_role("heading", name="Page not found")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("dpia-nav")).to_have_count(0)


def test_dpia_case_opens_and_migrates_to_the_durable_store_when_enabled(
    page: Page,
    live_server: str,
) -> None:
    _stub_server_info(page, dpia=True)

    page.goto(f"{live_server}/dpia")

    expect(page.get_by_test_id("dpia-nav")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name="DPIA Investigation Desk")).to_be_visible()
    page.get_by_role(
        "link",
        name="Student Success Alert — AI Early-Warning and Intervention",
        exact=False,
    ).click()
    expect(
        page.get_by_role(
            "heading",
            name="Student Success Alert — AI Early-Warning and Intervention",
        )
    ).to_be_visible(timeout=30_000)
    expect(page.get_by_text("Processing model v3", exact=True)).to_be_visible()

    response = httpx.get(
        f"{live_server}/v1/dpia/cases/student-success-alert",
        timeout=10.0,
    )
    response.raise_for_status()
    assert response.json()["revision"] == 1

    page.reload()
    expect(page.get_by_text("Processing model v3", exact=True)).to_be_visible(timeout=30_000)
