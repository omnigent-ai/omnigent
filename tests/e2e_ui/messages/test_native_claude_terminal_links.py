"""UI journeys: opening links that real Claude Code prints in its Terminal view.

Fullscreen Claude Code tracks the mouse, so plain clicks go to it and ⌘/Ctrl-click
opens links. Under tmux control mode it only goes fullscreen when
``CLAUDE_CODE_NO_FLICKER=1`` reaches it, so that journey is opt-in.
"""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import (
    reset_mock_llm,
    set_fallback_mock_llm,
)

from ..shells.test_terminal_session_links import (
    expect_terminal_link_hovered,
    record_terminal_mouse_reports,
)
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _TERMINAL_VIEW,
    _open_terminal_view,
    _pane_text,
    _tmux_advert,
    _type_into_tui,
    _wait_terminal_connected,
)

_LINK_HOST = "https://docs.example.com"
_REPLY_TIMEOUT_MS = 90_000


def _tmux_format(base_url: str, session_id: str, format_string: str) -> str:
    """Expand a tmux format for the session's pane, e.g. ``"#{pane_width}"``."""
    advert = _tmux_advert(base_url, session_id)
    assert advert is not None, "the Claude terminal was never advertised"
    return subprocess.run(
        [
            "tmux",
            "-S",
            advert["socket_path"],
            "display-message",
            "-p",
            "-t",
            advert["tmux_target"],
            format_string,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _serve_link_destination(page: Page) -> list[str]:
    """Answer every request to the link host with a page naming its URL."""
    opened: list[str] = []

    def _fulfill(route: Route) -> None:
        opened.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="text/html",
            body=f"<h1>Opened from the terminal</h1><p>{route.request.url}</p>",
        )

    page.context.route(f"{_LINK_HOST}/**", _fulfill)
    return opened


def _ask_for_long_link(
    page: Page, base_url: str, session_id: str, mock_llm_server_url: str
) -> tuple[str, list[str]]:
    """Have Claude Code print a URL that wraps, and return it with the pane rows."""
    nonce = uuid.uuid4().hex[:8]
    # Longer than the pane is wide, so the link always wraps onto a second row.
    width = int(_tmux_format(base_url, session_id, "#{pane_width}"))
    filler = "-".join(["command-click"] * (width // 14 + 1))
    url = f"{_LINK_HOST}/guide/terminal-links/{filler}?ref={nonce}"
    reply = f"See {url} for details."
    reset_mock_llm(mock_llm_server_url)
    for key in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, key, reply)
    _type_into_tui(page, f"Where are the terminal link docs? {nonce}")
    for _ in range(_REPLY_TIMEOUT_MS // 500):
        pane = _pane_text(base_url, session_id)
        if "See https://" in pane and "details." in pane.split("See https://", 1)[1]:
            break
        page.wait_for_timeout(500)
    else:
        raise AssertionError(f"Claude Code never printed the link; pane:\n{pane}")
    # Let the renderer settle before measuring cells.
    page.wait_for_timeout(1_000)
    return url, _pane_text(base_url, session_id).split("\n")


def _link_cells(rows: list[str], url: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return a ``(row, column)`` inside the link on its first and its wrapped row."""
    first_row = next((index for index, row in enumerate(rows) if "See https://" in row), None)
    pane = "\n".join(rows)
    assert first_row is not None and first_row + 1 < len(rows), f"link not on screen:\n{pane}"
    assert url not in rows[first_row], f"the link did not wrap: {rows[first_row]!r}"
    first_column = rows[first_row].index("https://") + len("https://") + 2
    continuation = rows[first_row + 1]
    second_column = len(continuation) - len(continuation.lstrip()) + 1
    return (first_row, first_column), (first_row + 1, second_column)


def _cell_point(
    page: Page, base_url: str, session_id: str, cell: tuple[int, int]
) -> tuple[float, float]:
    """Translate a pane ``(row, column)`` to the center of that xterm cell."""
    height, width = (
        int(part)
        for part in _tmux_format(base_url, session_id, "#{pane_height} #{pane_width}").split()
    )
    box = page.locator(_TERMINAL_VIEW).last.locator(".xterm-screen").first.bounding_box()
    assert box is not None
    row, column = cell
    return (
        box["x"] + (column + 0.5) * box["width"] / width,
        box["y"] + (row + 0.5) * box["height"] / height,
    )


def _hover(page: Page, point: tuple[float, float]) -> None:
    page.mouse.move(point[0] - 4, point[1])
    page.mouse.move(*point, steps=4)
    expect_terminal_link_hovered(page)


def _command_click_opens(page: Page, point: tuple[float, float], url: str) -> None:
    """⌘/Ctrl-click *point* and check the new tab shows *url*, then return to the session."""
    _hover(page, point)
    with page.context.expect_page(timeout=15_000) as popup_info:
        page.keyboard.down("ControlOrMeta")
        page.mouse.click(*point)
        page.keyboard.up("ControlOrMeta")
    popup = popup_info.value
    popup.wait_for_load_state()
    assert popup.url == url
    expect(popup.get_by_text(url)).to_be_visible()
    popup.wait_for_timeout(1_500)
    page.bring_to_front()


def _open_claude_terminal(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    # Claude Code drops keystrokes typed before its prompt has rendered.
    for _ in range(_REPLY_TIMEOUT_MS // 500):
        if "❯" in _pane_text(base_url, session_id):
            break
        page.wait_for_timeout(500)
    else:
        raise AssertionError("Claude Code never showed its prompt")
    page.wait_for_timeout(1_000)


@pytest.mark.timeout(300)
def test_command_click_opens_a_wrapped_claude_link_from_either_row(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    page: Page = request.getfixturevalue("page")
    opened = _serve_link_destination(page)
    _open_claude_terminal(page, base_url, session_id)

    url, rows = _ask_for_long_link(page, base_url, session_id, mock_llm_server_url)
    first_cell, wrapped_cell = _link_cells(rows, url)

    _command_click_opens(page, _cell_point(page, base_url, session_id, first_cell), url)
    _command_click_opens(page, _cell_point(page, base_url, session_id, wrapped_cell), url)
    assert opened == [url, url]


@pytest.mark.skipif(
    os.environ.get("CLAUDE_CODE_NO_FLICKER") != "1",
    reason="Claude Code's fullscreen renderer needs CLAUDE_CODE_NO_FLICKER=1 in the runner env",
)
@pytest.mark.timeout(300)
def test_fullscreen_claude_keeps_plain_clicks_and_command_click_opens(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    page: Page = request.getfixturevalue("page")
    opened = _serve_link_destination(page)
    reports = record_terminal_mouse_reports(page)
    _open_claude_terminal(page, base_url, session_id)

    url, rows = _ask_for_long_link(page, base_url, session_id, mock_llm_server_url)
    flags = _tmux_format(base_url, session_id, "#{alternate_on} #{mouse_sgr_flag}")
    assert flags == "1 1", f"Claude Code is not tracking the mouse on the alt screen: {flags}"
    first_cell, wrapped_cell = _link_cells(rows, url)
    point = _cell_point(page, base_url, session_id, first_cell)

    _hover(page, point)
    reports.clear()
    page.mouse.click(*point)
    page.wait_for_timeout(2_000)  # custom-lint: disable=sleep-then-assert -- no tab may open
    assert opened == []
    assert len(page.context.pages) == 1
    assert [(code & 3, phase) for code, phase in reports] == [(0, "M"), (0, "m")], reports

    reports.clear()
    _command_click_opens(page, point, url)
    _command_click_opens(page, _cell_point(page, base_url, session_id, wrapped_cell), url)
    page.wait_for_timeout(1_000)  # custom-lint: disable=sleep-then-assert -- no extra tab or press
    assert opened == [url, url]
    assert reports == []
