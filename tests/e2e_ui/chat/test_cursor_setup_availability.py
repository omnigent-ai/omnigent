"""E2E: Cursor setup guidance in the New Chat agent picker."""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

_HOST_ID = "host_cursor_e2e"
_HOST_NAME = "cursor-e2e-host"
_AGENT_ID = "ag_cursor_e2e"


def _fulfill_hosts(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "hosts": [
                    {
                        "host_id": _HOST_ID,
                        "name": _HOST_NAME,
                        "owner": "e2e",
                        "status": "online",
                        "configured_harnesses": {"cursor-native": False},
                    }
                ]
            }
        ),
    )


def _fulfill_agents(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "data": [
                    {
                        "id": _AGENT_ID,
                        "name": "cursor-native-ui",
                        "display_name": "Cursor",
                        "description": "Cursor's coding agent",
                        "harness": "cursor-native",
                        "skills": [],
                    }
                ]
            }
        ),
    )


def _fulfill_empty_agent_scan(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({"data": []}),
    )


def test_cursor_missing_cli_shows_install_and_login_guidance(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A missing Cursor CLI is badged and explained before session launch."""
    base_url, session_id = seeded_session
    del session_id

    page.route("**/v1/hosts", _fulfill_hosts)
    page.route("**/v1/agents", _fulfill_agents)
    page.route(re.compile(r"/v1/sessions\?.*kind=any"), _fulfill_empty_agent_scan)
    page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID!r}: ["/work/repo"] }})
        );"""
    )

    page.goto(f"{base_url}/")
    composer = page.get_by_test_id("new-chat-landing-input")
    expect(composer).to_be_visible(timeout=30_000)

    warning = page.get_by_test_id("new-chat-landing-harness-warning")
    expect(warning).to_be_visible(timeout=30_000)
    expect(warning).to_contain_text(f"Cursor needs cursor-agent on {_HOST_NAME}")
    expect(warning).to_contain_text("curl https://cursor.com/install -fsS | bash")
    expect(warning).to_contain_text("cursor-agent login")
    expect(warning.locator("code")).to_have_count(2)

    # The guidance is visible before launch and remains warning-only.
    composer.fill("help me inspect this repository")
    expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
    expect(warning).to_be_visible()

    page.get_by_test_id("new-chat-landing-agent-select").click()
    badge = page.get_by_test_id(f"new-chat-landing-agent-warning-{_AGENT_ID}")
    expect(badge).to_be_visible()
    expect(badge).to_have_text("install & login")
