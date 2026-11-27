"""Turn on the ``quick_pick_palette`` flag in a page and run its ⌘K commands."""

from __future__ import annotations

import json
import time

from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry


def enable_quick_pick_palette(page: Page, *also: str) -> None:
    """Report ``quick_pick_palette``, and the flags in ``also``, on from ``/v1/info``.

    The server's other flags are kept. One route answers for every flag, since a later
    ``/v1/info`` route replaces an earlier one.
    """

    def info(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        enabled = dict.fromkeys(("quick_pick_palette", *also), True)
        body["features"] = {**(body.get("features") or {}), **enabled}
        route.fulfill(response=response, body=json.dumps(body))

    page.route("**/v1/info", info)


# How long the app may take to apply the server's flags after it first renders.
_FLAGS_ARRIVE_S = 30.0


def open_quick_pick_palette(page: Page) -> Locator:
    """Open ⌘K's QuickPick palette and return its focused field.

    A page renders with the default flags when ``/v1/info`` answers slowly, as under a loaded
    run, so ⌘K can open the flag-off palette, which the late answer then closes. Both are named
    "Command palette", so wait for QuickPick itself, dismissing the other and pressing again.
    """
    palette_input = page.get_by_role("combobox", name="Command palette")
    dialog = page.get_by_role("dialog")
    deadline = time.monotonic() + _FLAGS_ARRIVE_S
    while True:
        page.keyboard.press("ControlOrMeta+k")
        try:
            expect(dialog.locator('[data-slot="quick-pick"]')).to_have_count(1, timeout=3_000)
            break
        except AssertionError:
            if time.monotonic() > deadline:
                raise
            if dialog.count():
                page.keyboard.press("Escape")
            expect(dialog).to_have_count(0, timeout=5_000)
    expect(palette_input).to_be_focused(timeout=10_000)
    return palette_input


def run_palette_command(page: Page, query: str, name: str, *, closes: bool = False) -> None:
    """Open ⌘K, filter to ``query`` and run the first option, which must be ``name``.

    Pass ``closes=True`` for commands that finish in the palette; leave it off for
    ones that open a follow-up picker, such as Select theme….
    """
    palette_input = open_quick_pick_palette(page)
    palette_input.fill(query)
    first = page.get_by_role("dialog").get_by_role("option").first
    expect(first).to_contain_text(name)
    page.keyboard.press("Enter")
    if closes:
        expect(palette_input).to_have_count(0)
