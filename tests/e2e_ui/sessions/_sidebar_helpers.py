"""Shared session and sidebar setup for browser journeys through the real server."""

import httpx
from playwright.sync_api import Page, Route


def wait_for_sidebar_collapse(page: Page) -> None:
    """Wait for the closed sidebar to release its layout width before measuring."""
    page.wait_for_function(
        "() => [...document.querySelectorAll('.conversations-sidebar')]"
        ".every((sidebar) => sidebar.getBoundingClientRect().width === 0)"
    )
    page.evaluate(
        "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
    )


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


def open_session_filters(page: Page) -> None:
    """Open the sidebar funnel's Display submenu, which lists the session filters."""
    page.get_by_test_id("session-filter").click()
    page.get_by_test_id("session-display-menu").click()


def choose_session_filter(page: Page, value: str) -> None:
    """Pick the ``all``/``mine``/``shared``/``archived`` filter from the sidebar funnel."""
    open_session_filters(page)
    page.get_by_test_id(f"session-filter-{value}").click()
