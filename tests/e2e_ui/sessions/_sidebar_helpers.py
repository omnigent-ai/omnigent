"""Shared sidebar setup for browser journeys through the real server."""

from playwright.sync_api import Page, Route


def enable_grouping_ui(page: Page, enabled: bool = True) -> None:
    """Advertise the ``sidebar_grouping_ui`` release flag on the live server's info."""

    def serve_info(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {**info.get("features", {}), "sidebar_grouping_ui": enabled}
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", serve_info)
