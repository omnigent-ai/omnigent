"""Browser account controls, including connection errors and disconnect."""

from __future__ import annotations

from playwright.sync_api import Page, Route, expect


def test_managed_mcp_account_controls(page: Page, live_server: str) -> None:
    connected = False
    fail_test = False

    def info(route: Route) -> None:
        response = route.fetch()
        body = response.json()
        body["enabled_connections"] = ["mcp"]
        route.fulfill(response=response, json=body)

    def catalog(route: Route) -> None:
        route.fulfill(
            json={
                "data": [
                    {
                        "id": "tracker",
                        "title": "Demo work tracker",
                        "description": "A fictional account",
                        "auth": "bearer",
                        "connected": connected,
                        "tools": ["read_ticket"],
                        "connect_provider": "bearer",
                    }
                ]
            }
        )

    def connection(route: Route) -> None:
        nonlocal connected
        if route.request.method == "PUT":
            assert route.request.post_data_json == {"token": "demo-token"}
            connected = True
        elif route.request.method == "DELETE":
            connected = False
        route.fulfill(json={"connected": connected})

    def test_connection(route: Route) -> None:
        if fail_test:
            route.fulfill(status=502, json={"detail": "MCP connection failed"})
        else:
            route.fulfill(json={"tools": ["read_ticket"]})

    page.route("**/v1/info", info)
    page.route("**/v1/mcp-registry/services", catalog)
    page.route("**/v1/mcp-registry/services/tracker/connection", connection)
    page.route("**/v1/mcp-registry/services/tracker/test", test_connection)
    page.goto(f"{live_server}/settings/integrations")
    expect(page.get_by_text("Not connected", exact=True)).to_be_visible()
    page.get_by_role("button", name="Connect", exact=True).click()
    token = page.get_by_label("Token for Demo work tracker")
    token.fill("demo-token")
    page.get_by_role("button", name="Save token").click()
    expect(page.get_by_text("Connected", exact=True)).to_be_visible()
    expect(token).to_have_count(0)
    page.get_by_role("button", name="Test connection").click()
    expect(page.get_by_role("status")).to_contain_text("Connection works. Tools: read_ticket")
    fail_test = True
    page.get_by_role("button", name="Test connection").click()
    expect(page.get_by_role("alert")).to_contain_text("MCP connection failed")
    page.get_by_role("button", name="Disconnect", exact=True).click()
    expect(page.get_by_text("Not connected", exact=True)).to_be_visible()
