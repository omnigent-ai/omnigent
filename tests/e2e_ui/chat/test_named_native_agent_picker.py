"""Named native bundles retain their identity and execution mode in the picker."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
def test_named_native_agents_are_selectable_as_custom_agents(
    page: Page, live_server: str, width: int
) -> None:
    """Exercise the real picker with two bundles sharing the OpenCode harness."""
    page.set_viewport_size({"width": width, "height": 900})
    agents = [
        {"id": "ag_opencode", "name": "opencode-native-ui", "builtin": True},
        {"id": "ag_reviewer", "name": "code-reviewer", "builtin": False},
        {"id": "ag_summarizer", "name": "thread-summarizer", "builtin": False},
    ]
    for agent in agents:
        agent.update(harness="opencode-native", skills=[], description=None)

    def catalog(route: Route) -> None:
        route.fulfill(json={"data": agents})

    def sessions(route: Route) -> None:
        if route.request.method == "POST":
            # Capture creation without starting a native CLI or a real LLM turn.
            route.fulfill(status=503, json={"detail": "Session creation intercepted by test"})
        else:
            route.fulfill(json={"data": [], "has_more": False})

    page.route(re.compile(r"/v1/agents(?:\?.*)?$"), catalog)
    page.route(re.compile(r"/v1/sessions(?:\?.*)?$"), sessions)
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={
                "hosts": [
                    {
                        "host_id": "host_picker",
                        "name": "e2e-host",
                        "owner": "e2e",
                        "status": "online",
                        "configured_harnesses": {"opencode-native": True},
                    }
                ]
            }
        ),
    )
    page.add_init_script(
        "localStorage.setItem('omnigent:recent-workspaces', "
        + json.dumps(json.dumps({"host_picker": ["/work/repo"]}))
        + ");"
    )
    page.goto(f"{live_server}/")
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    expect(picker).to_be_visible(timeout=30_000)
    picker.click()

    launcher = page.get_by_test_id("new-chat-landing-agent-ag_opencode")
    expect(launcher).to_be_visible()
    expect(launcher).to_contain_text("OpenCode")
    custom = page.get_by_test_id("new-chat-landing-custom-agents")
    if width < 768:
        custom.click()
    else:
        custom.hover()

    reviewer = page.get_by_test_id("new-chat-landing-agent-ag_reviewer")
    summarizer = page.get_by_test_id("new-chat-landing-agent-ag_summarizer")
    expect(reviewer).to_have_accessible_name("code-reviewer")
    expect(summarizer).to_have_accessible_name("thread-summarizer")
    reviewer.click()
    expect(page.get_by_role("menu")).to_have_count(0)
    expect(picker).to_have_text("code-reviewer")
    page.get_by_test_id("new-chat-landing-input").fill("Review this change")
    with page.expect_request(
        lambda request: (
            request.method == "POST"
            and re.search(r"/v1/sessions(?:\?.*)?$", request.url) is not None
        )
    ) as created:
        page.get_by_test_id("new-chat-landing-submit").click()
    body = created.value.post_data_json
    assert body["agent_id"] == "ag_reviewer"
    assert body["labels"]["omnigent.ui"] == "terminal"
    assert body["labels"]["omnigent.wrapper"] == "opencode-native-ui"
