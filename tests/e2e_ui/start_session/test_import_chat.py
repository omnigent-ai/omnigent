"""E2E coverage for importing a native chat from the new-session page."""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect

_HOST_ID = "host_import_e2e"
_CLAUDE_SESSION_ID = "claude-import-e2e"


def test_import_recent_claude_chat(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A recent Claude chat can be selected and imported from the landing page."""
    base_url, imported_session_id = seeded_session

    def handle_hosts(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": "This machine",
                            "owner": "e2e",
                            "status": "online",
                        }
                    ]
                }
            ),
        )

    def handle_recent(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "sessions": [
                        {
                            "session_id": _CLAUDE_SESSION_ID,
                            "title": "Improve native chat importing",
                            "workspace": "/work/omnigent",
                            "item_count": 12,
                        }
                    ]
                }
            ),
        )

    def handle_load(route: Route) -> None:
        assert route.request.post_data_json == {
            "source": "claude",
            "session_id": _CLAUDE_SESSION_ID,
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"source": "claude", "items": []}),
        )

    def handle_import(route: Route) -> None:
        body = route.request.post_data_json
        assert body["host_id"] == _HOST_ID
        assert body["source"] == "claude"
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"session_id": imported_session_id}),
        )

    page.route("**/v1/hosts", handle_hosts)
    page.route(f"**/v1/hosts/{_HOST_ID}/chat-imports?*", handle_recent)
    page.route(f"**/v1/hosts/{_HOST_ID}/chat-imports/load", handle_load)
    page.route("**/v1/imports", handle_import)

    page.goto(f"{base_url}/")
    page.get_by_test_id("new-chat-import-chat").click()

    dialog = page.get_by_test_id("import-chat-dialog")
    expect(dialog).to_be_visible()
    expect(page.get_by_test_id("import-chat-host")).to_contain_text("This machine")
    expect(dialog.get_by_text("Improve native chat importing")).to_be_visible()
    expect(dialog.get_by_text("/work/omnigent · 12 items")).to_be_visible()

    dialog.get_by_text("Improve native chat importing").click()
    expect(page.get_by_test_id("import-chat-session-id")).to_have_value(_CLAUDE_SESSION_ID)
    dialog.get_by_role("button", name="Import chat", exact=True).click()

    expect(page).to_have_url(f"{base_url}/c/{imported_session_id}")
