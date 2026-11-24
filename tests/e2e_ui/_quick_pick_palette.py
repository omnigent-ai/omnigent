"""Turn on the ``quick_pick_palette`` flag in a page and run its ⌘K commands."""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry


def enable_quick_pick_palette(page: Page) -> None:
    """Report ``quick_pick_palette`` on from ``/v1/info``, keeping the server's other flags."""

    def info(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        body["features"] = {**(body.get("features") or {}), "quick_pick_palette": True}
        route.fulfill(response=response, body=json.dumps(body))

    page.route("**/v1/info", info)


def run_palette_command(page: Page, query: str, name: str, *, closes: bool = False) -> None:
    """Open ⌘K, filter to ``query`` and run the first option, which must be ``name``.

    Pass ``closes=True`` for commands that finish in the palette; leave it off for
    ones that open a follow-up picker, such as Select theme….
    """
    page.keyboard.press("ControlOrMeta+k")
    palette_input = page.get_by_role("combobox", name="Command palette")
    expect(palette_input).to_be_focused(timeout=10_000)
    palette_input.fill(query)
    first = page.get_by_role("dialog").get_by_role("option").first
    expect(first).to_contain_text(name)
    page.keyboard.press("Enter")
    if closes:
        expect(palette_input).to_have_count(0)
