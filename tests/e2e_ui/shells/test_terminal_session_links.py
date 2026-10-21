"""E2E: terminal links open the right destination.

The embedded xterm makes URLs in TUI / shell output clickable via its
``TerminalLinkProvider``. Same-origin Omnigent session URLs are app navigation, not
external content: clicking one should update the current SPA route instead of
opening a duplicate browser tab/window for the same chat.

A URL longer than the pane is shown across two rows. When the printing program
broke the line at the pane width itself (a CLI that word-wraps its output to the
terminal size), the user sees the same two rows as a terminal soft wrap and a
click must open the complete URL, not the first-row fragment.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import open_right_rail

_WRAPPED_URL_HOST = "wrapped-link.example.com"
# Longer than a rail shell pane at the default viewport, so it needs a second
# row; the distinctive tail makes a cut destination unambiguous.
_WRAPPED_URL = (
    f"https://{_WRAPPED_URL_HOST}/explore/connections/seg-path/seg-path/pagerduty-mcp?o=1#end"
)


def _open_new_shell(page: Page) -> None:
    """Open a user shell as a rail tab via the tab strip's "+" → Shell menu."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def _run_at_terminal_origin(page: Page, command: str) -> None:
    """Run *command* in the active xterm after clearing it and homing the cursor."""
    terminal_view = page.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type(f"printf '\\033[2J\\033[H'; {command}")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)


def _print_url_at_terminal_origin(page: Page, url: str) -> None:
    """Print *url* at row 1/column 1 of the active xterm."""
    _run_at_terminal_origin(page, f"printf '%s\\n' '{url}'")


def _print_url_folded_at_pane_width(page: Page, url: str) -> None:
    """Print *url* at row 1/column 1 with the program breaking the line at the pane width."""
    _run_at_terminal_origin(page, f"printf '%s\\n' '{url}' | fold -w \"$(tput cols)\"")


def _terminal_row_point(page: Page, row: int = 1, rows: int = 1) -> tuple[float, float]:
    """Return a point near the start of 1-based *row* in the active xterm screen of *rows* rows."""
    terminal_view = page.get_by_test_id("terminal-view").last
    screen = terminal_view.locator(".xterm-screen").first
    expect(screen).to_be_visible(timeout=20_000)
    box = screen.bounding_box()
    assert box is not None, "xterm screen should have a clickable bounding box"
    return box["x"] + 16, box["y"] + (row - 1) * box["height"] / rows + 10


def _click_first_terminal_row(page: Page) -> None:
    """Click near the start of row 1 in the active xterm screen."""
    page.mouse.click(*_terminal_row_point(page))


def _record_pane_sizes(page: Page) -> list[list[tuple[int, int]]]:
    """Collect the (cols, rows) of each resize sent on each attach socket, newest socket last."""
    panes: list[list[tuple[int, int]]] = []

    def _on_ws(ws: object) -> None:
        if "/attach" not in ws.url:  # type: ignore[attr-defined]
            return
        sizes: list[tuple[int, int]] = []
        panes.append(sizes)

        def _on_frame(payload: str | bytes) -> None:
            if isinstance(payload, str) and payload.startswith("{"):
                message = json.loads(payload)
                if message.get("type") == "resize":
                    sizes.append((int(message["cols"]), int(message["rows"])))

        ws.on("framesent", _on_frame)  # type: ignore[attr-defined]

    page.on("websocket", _on_ws)
    return panes


def _echo_destination(route: Route) -> None:
    """Serve a page showing the URL the terminal link actually opened."""
    route.fulfill(
        status=200,
        content_type="text/html",
        body=(
            "<html><body style='font: 24px monospace; padding: 32px'>"
            "<h1>Destination opened from the terminal</h1>"
            f"<p style='word-break: break-all'>{route.request.url}</p></body></html>"
        ),
    )


def test_same_origin_terminal_session_link_navigates_in_app(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Clicking a terminal-printed session URL does not open a duplicate tab.

    The query string makes the destination visibly different from the current
    URL while still targeting the same session. That proves the click hit the
    xterm link: a missed click leaves the URL unchanged, while the old behavior
    opens a popup/new tab and also leaves the current URL unchanged.
    """
    base_url, session_id = terminal_session
    target_path = f"/c/{session_id}?terminal-link-e2e=1"
    target_url = f"{base_url}{target_path}"

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    _print_url_at_terminal_origin(page, target_url)

    try:
        with page.expect_popup(timeout=1_000):
            _click_first_terminal_row(page)
    except PlaywrightTimeoutError:
        pass
    else:
        raise AssertionError("same-origin terminal session link opened a popup")

    expect(page).to_have_url(f"{base_url}{target_path}")


def test_program_broken_two_row_terminal_url_opens_full_destination(
    request: pytest.FixtureRequest, terminal_session: tuple[str, str]
) -> None:
    """Clicking either row of a URL the program broke at the pane width opens the whole URL.

    A width-aware CLI ends the first row with its own line break, so the pane
    holds two hard rows that look exactly like a terminal soft wrap.
    """
    base_url, session_id = terminal_session
    page: Page = request.getfixturevalue("page")
    page.context.route(f"https://{_WRAPPED_URL_HOST}/**", _echo_destination)
    panes = _record_pane_sizes(page)

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    _print_url_folded_at_pane_width(page, _WRAPPED_URL)
    # The shell opened last, so its attach socket is the newest one.
    pane_cols, pane_rows = panes[-1][-1] if panes and panes[-1] else (0, 0)
    assert pane_cols and pane_rows and pane_cols < len(_WRAPPED_URL) <= 2 * pane_cols, (
        f"the URL must span exactly two rows of the pane: pane size {pane_cols}x{pane_rows}"
    )

    for row in (1, 2):
        # Hover first so the detected link's underline is visible before the click.
        point = _terminal_row_point(page, row, pane_rows)
        page.mouse.move(*point)
        page.wait_for_timeout(1_000)
        with page.expect_popup(timeout=5_000) as popup_info:
            page.mouse.click(*point)
        popup = popup_info.value
        expect(
            popup.get_by_role("heading", name="Destination opened from the terminal")
        ).to_be_visible()

        assert popup.url == _WRAPPED_URL, (
            f"clicking row {row} of the two-row URL opened {popup.url!r} "
            f"instead of the complete URL {_WRAPPED_URL!r} (pane is {pane_cols} columns wide)"
        )
