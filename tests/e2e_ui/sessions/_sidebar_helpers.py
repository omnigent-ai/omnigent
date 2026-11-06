"""Shared session and sidebar setup for browser journeys through the real server."""

import httpx
from playwright.sync_api import Page, Route


def patch_session(base_url: str, session_id: str, **fields: object) -> None:
    """PATCH session fields such as ``title`` or ``archived`` over the API."""
    response = httpx.patch(f"{base_url}/v1/sessions/{session_id}", json=fields, timeout=10.0)
    response.raise_for_status()


def session_archived(base_url: str, session_id: str) -> bool:
    """Whether the server reports the session archived, e.g. inside ``poll_until``."""
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json()["archived"] is True


def enable_grouping_ui(page: Page, enabled: bool = True) -> None:
    """Advertise the ``sidebar_grouping_ui`` release flag on the live server's info."""

    def serve_info(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {**info.get("features", {}), "sidebar_grouping_ui": enabled}
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", serve_info)
