"""Playwright coverage for the deployment-wide Usage page release feature."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.e2e_ui.conftest import _server_state


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


@pytest.mark.min_server_version("0.15.0")
def test_usage_report_link_and_chat_reload_keep_complete_subtree_usage(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Stored parent/archived-child costs reach both real HTTP usage surfaces."""
    base_url, session_id = seeded_session
    database_uri = _server_state.get("database_uri")
    if not database_uri:
        pytest.skip("usage seeding requires the isolated spawned server")
    store = SqlAlchemyConversationStore(str(database_uri))
    parent = store.get_conversation(session_id)
    assert parent is not None
    title = f"Persisted subtree usage {session_id}"
    store.update_conversation(session_id, title=title)
    child = store.create_conversation(
        agent_id=parent.agent_id, kind="sub_agent", parent_conversation_id=session_id
    )
    store.update_conversation(child.id, archived=True)
    store.set_session_usage(
        session_id,
        {
            "total_cost_usd": 1.0,
            "by_model": {"parent-model": {"input_tokens": 100, "total_cost_usd": 1.0}},
        },
    )
    store.set_session_usage(
        child.id,
        {
            "total_cost_usd": 2.5,
            "by_model": {"child-model": {"input_tokens": 200, "total_cost_usd": 2.5}},
        },
    )
    _stub_server_info(page, usage_page=True)
    session_url = f"{base_url}/v1/sessions/{session_id}"
    usage_url = f"{session_url}/usage"
    try:
        # Seeded storage has no usage SSE events: chat must hydrate over HTTP.
        with page.expect_response(f"{base_url}/v1/usage") as report_response:
            page.goto(f"{base_url}/usage")
        assert report_response.value.ok
        report = report_response.value.json()
        report_session = next(row for row in report["sessions"] if row["id"] == session_id)
        assert report_session["cost_usd"] == 3.5
        assert report_session["models"] == {"parent-model": 1.0, "child-model": 2.5}
        row = page.locator("table tr", has_text=title)
        expect(row.get_by_text("$3.50", exact=True)).to_be_visible()

        for reloading, child_cost in ((False, 2.5), (True, 4.0)):
            if reloading:
                store.set_session_usage(
                    child.id,
                    {
                        "total_cost_usd": child_cost,
                        "by_model": {
                            "child-model": {"input_tokens": 300, "total_cost_usd": child_cost}
                        },
                    },
                )
            with (
                page.expect_response(
                    lambda response: (
                        response.url.split("?", 1)[0] == session_url
                        and response.request.method == "GET"
                    )
                ) as snapshot_response,
                page.expect_response(usage_url) as usage_response,
            ):
                if reloading:
                    page.reload(wait_until="domcontentloaded")
                else:
                    row.get_by_role("link", name=title).click()

            snapshot = snapshot_response.value.json()
            assert snapshot["usage_included"] is False
            assert snapshot["total_cost_usd"] is None
            assert snapshot["usage_by_model"] is None
            assert usage_response.value.ok
            usage = usage_response.value.json()
            assert usage["id"] == session_id
            assert usage["total_cost_usd"] == 1.0 + child_cost
            assert usage["usage_by_model"]["parent-model"]["total_cost_usd"] == 1.0
            assert usage["usage_by_model"]["child-model"]["total_cost_usd"] == child_cost

            expect(page.get_by_placeholder("Send a message…")).to_be_editable()
            trigger = page.get_by_test_id("agent-info-trigger")
            trigger.focus()
            trigger.press("Enter")
            panel = page.get_by_test_id("agent-info-panel")
            expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text(
                f"${1.0 + child_cost:.2f}"
            )
            breakdown = panel.get_by_test_id("agent-info-usage-by-model")
            breakdown.locator("summary").press("Enter")
            expect(breakdown.get_by_test_id("agent-info-model-parent-model")).to_contain_text(
                "$1.00"
            )
            expect(breakdown.get_by_test_id("agent-info-model-child-model")).to_contain_text(
                f"${child_cost:.2f}"
            )
    finally:
        deleted = httpx.delete(f"{base_url}/v1/sessions/{child.id}", timeout=10.0)
        deleted.raise_for_status()


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
