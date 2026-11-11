"""Claude subscription connection controls with intercepted connection responses.

The browser saves and disconnects a pasted setup token without reaching a real
credential store. Backend tests cover storing, vending and validating the token.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

_TOKEN = "sk-ant-oat01-browser-example"


def test_claude_subscription_connection_controls(page: Page, live_server: str) -> None:
    def info(route: Route) -> None:
        response = route.fetch()
        body = response.json()
        body["enabled_connections"] = ["claude"]
        route.fulfill(response=response, json=body)

    state: dict[str, object] = {"connected": False, "saved": [], "disconnects": 0}

    def status_body() -> dict[str, object]:
        connected = bool(state["connected"])
        return {
            "enabled": True,
            "connected": connected,
            "connected_at": 1_700_000_000 if connected else None,
        }

    def connection(route: Route) -> None:
        path = route.request.url.split("?", 1)[0]
        if path.endswith("/token"):
            assert route.request.method == "PUT"
            token = json.loads(route.request.post_data or "{}")["token"]
            if not token.startswith("sk-ant-oat"):
                route.fulfill(status=422, json={"detail": "Paste the token from setup-token."})
                return
            state["saved"].append(token)  # type: ignore[union-attr]
            state["connected"] = True
        elif path.endswith("/disconnect"):
            assert route.request.method == "POST"
            state.update(connected=False, disconnects=int(state["disconnects"]) + 1)  # type: ignore[arg-type]
            route.fulfill(json={"disconnected": True})
            return
        route.fulfill(json=status_body())

    page.route("**/v1/info", info)
    page.route(re.compile(r"/v1/connections/claude/(status|token|disconnect)(?:\?|$)"), connection)
    page.goto(f"{live_server}/settings/integrations")

    token_input = page.get_by_label("Claude setup token")
    token_input.fill("sk-ant-api03-not-a-setup-token")
    page.get_by_role("button", name="Connect Claude", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Paste the token from setup-token.")

    token_input.fill(_TOKEN)
    page.get_by_role("button", name="Connect Claude", exact=True).click()
    expect(page.get_by_text(re.compile(r"Subscription connected\."))).to_be_visible()
    expect(page.get_by_label("Claude setup token")).to_have_count(0)
    assert state["saved"] == [_TOKEN]

    page.get_by_role("button", name="Disconnect", exact=True).click()
    expect(page.get_by_label("Claude setup token")).to_be_visible()
    assert state["disconnects"] == 1
