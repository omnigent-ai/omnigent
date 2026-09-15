"""Browser terminal inventory and attach mocks shared by layout tests."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, WebSocketRoute

from tests.e2e_ui.conftest import fetch_with_retry


def mock_terminal_attach(
    page: Page, session_ids: list[str], *, terminal_name: str = "codex"
) -> dict[str, list[WebSocketRoute]]:
    """Expose online terminal resources and record their mocked attach sockets."""
    terminal_id = f"terminal_{terminal_name}_main"
    sockets: dict[str, list[WebSocketRoute]] = {session_id: [] for session_id in session_ids}

    def session_snapshot(route: Route) -> None:
        response = fetch_with_retry(route)
        payload = response.json()
        rows = payload.get("data", [payload])
        for row in rows:
            if row.get("id") in session_ids:
                row.update(runner_id="runner_scrollbar_mock", runner_online=True)
        route.fulfill(response=response, json=payload)

    def terminal_inventory(route: Route) -> None:
        route.fulfill(
            json={
                "object": "list",
                "data": [
                    {
                        "id": terminal_id,
                        "object": "terminal",
                        "name": terminal_name,
                        "metadata": {
                            "terminal_name": terminal_name,
                            "session_key": "main",
                            "running": True,
                        },
                    }
                ],
                "has_more": False,
            }
        )

    def attach(ws: WebSocketRoute) -> None:
        session_id = urlparse(ws.url).path.split("/")[3]
        sockets[session_id].append(ws)
        ws.on_message(lambda _message: None)

    ids = "|".join(re.escape(session_id) for session_id in session_ids)
    page.route(re.compile(rf"/v1/sessions(?:/(?:{ids}))?(?:\?|$)"), session_snapshot)
    page.route(
        re.compile(rf"/v1/sessions/(?:{ids})/resources/terminals(?:\?|$)"),
        terminal_inventory,
    )
    page.route(
        "**/health?session_ids=*",
        lambda route: route.fulfill(
            json={
                "sessions": {
                    session_id: {"runner_online": True, "host_online": None}
                    for session_id in session_ids
                }
            }
        ),
    )
    page.route_web_socket(
        re.compile(rf"/v1/sessions/(?:{ids})/resources/terminals/{terminal_id}/attach"),
        attach,
    )
    # The fake online PTYs must not be contradicted by the unbound server rows.
    page.route_web_socket("**/v1/sessions/updates*", lambda ws: ws.on_message(lambda _msg: None))
    return sockets
