"""E2E: the composer switches a real claude-native session's permission mode.

One session is launched with ``--permission-mode dontAsk``, so Claude Code renders
its ``don't ask on`` footer and the chip reads "Don't ask". Choosing a switchable
mode must move both the chip and that footer. Another starts in Manual and steps
through modes with Shift+Tab from the composer. The footer is read through the
product's read-only terminal-attach WebSocket (rendered with ``pyte``), since the
runner's tmux socket is not reachable from the test.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterator

import httpx
import pyte
import pytest
import websockets
from playwright.sync_api import Page, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.conftest import native_claude_mock_session_context

_TARGET_MODE = "acceptEdits"
_TARGET_LABEL = "Accept edits"
_DONT_ASK_FOOTER = "don't ask on"
_TARGET_FOOTER = "accept edits on"
# Claude CLI boot + first footer paint.
_FOOTER_TIMEOUT_S = 120.0
# Allow for prompt readiness and multi-step footer cycling on the runner.
_SWITCH_TIMEOUT_MS = 20_000
# Wider than any real pane: a read-only attach ignores the resize, and a
# narrower screen would wrap the footer line.
_PANE_COLS, _PANE_ROWS = 250, 60


@pytest.fixture
def native_claude_dont_ask_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    claude_managed_settings: None,
) -> Iterator[tuple[str, str]]:
    """A real Claude CLI launched into ``dontAsk`` against the mock provider."""
    with native_claude_mock_session_context(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        terminal_launch_args=["--permission-mode", "dontAsk"],
    ) as session:
        yield session


def _terminal_id(base_url: str, session_id: str) -> str | None:
    """Return the session's Claude terminal resource id once the runner has launched it."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    resp.raise_for_status()
    for resource in resp.json().get("data", []):
        if resource.get("type") == "terminal":
            return str(resource["id"])
    return None


def _render_terminal(
    base_url: str, session_id: str, terminal_id: str, settle_s: float = 3.0
) -> str:
    """Attach read-only to the terminal and return the screen text it renders."""
    ws_url = (
        base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        + f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?read_only=true"
    )
    screen = pyte.Screen(_PANE_COLS, _PANE_ROWS)
    stream = pyte.ByteStream(screen)

    async def _collect() -> None:
        async with websockets.connect(ws_url, max_size=None, open_timeout=20) as ws:
            await ws.send(json.dumps({"type": "resize", "cols": _PANE_COLS, "rows": _PANE_ROWS}))
            deadline = time.monotonic() + settle_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    break
                if isinstance(frame, bytes):
                    stream.feed(frame)

    run_in_fresh_loop(_collect())
    return "\n".join(screen.display)


def _wait_for_footer(base_url: str, session_id: str, footer: str, timeout_s: float) -> str:
    """Poll the rendered terminal until its mode footer contains *footer*; return the screen."""
    deadline = time.monotonic() + timeout_s
    screen = ""
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            terminal_id = _terminal_id(base_url, session_id)
            if terminal_id is not None:
                screen = _render_terminal(base_url, session_id, terminal_id)
                if footer in screen.lower():
                    return screen
        except (httpx.HTTPError, websockets.WebSocketException, OSError) as exc:
            # The terminal is still booting; keep polling until the deadline.
            last_error = exc
        time.sleep(1.0)
    raise AssertionError(
        f"terminal never rendered {footer!r}; last error: {last_error!r}; last screen:\n{screen}"
    )


@pytest.mark.nightly
@pytest.mark.timeout(360)
def test_permission_picker_switches_out_of_dont_ask(
    request: pytest.FixtureRequest,
    native_claude_dont_ask_session: tuple[str, str],
) -> None:
    """Picking Accept edits on a don't-ask session applies it in Claude's terminal.

    The real TUI must paint the don't-ask footer before the page is requested, so
    a recording starts at the user's first action.
    """
    base_url, session_id = native_claude_dont_ask_session
    _wait_for_footer(base_url, session_id, _DONT_ASK_FOOTER, timeout_s=_FOOTER_TIMEOUT_S)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    chip = page.get_by_test_id("composer-permission-chip")
    expect(chip).to_be_visible(timeout=60_000)
    expect(chip).to_have_text("Don't ask")

    chip.click()
    expect(page.get_by_test_id("composer-permission-menu")).to_be_visible()
    with page.expect_response(
        lambda r: r.request.method == "PATCH" and r.url.endswith(f"/v1/sessions/{session_id}"),
        timeout=_SWITCH_TIMEOUT_MS,
    ) as switched:
        page.get_by_test_id(f"composer-permission-option-{_TARGET_MODE}").click()
    response = switched.value

    # The composer echoes the server's rejection, which names this session.
    error_row = page.get_by_text(
        f"Could not switch to {_TARGET_MODE} mode for session '{session_id}'", exact=False
    )
    if response.status != 200:
        rejection = response.text()
        # Let the composer render the rejection so a recording ends on it, but
        # report the server's reason even if the row never appears.
        try:
            expect(error_row.first).to_be_visible(timeout=10_000)
        finally:
            pytest.fail(f"mode switch rejected: {rejection}")
    expect(chip).to_have_text(_TARGET_LABEL)
    expect(error_row).to_have_count(0)
    _wait_for_footer(base_url, session_id, _TARGET_FOOTER, timeout_s=10.0)


_COMPOSER_PLACEHOLDER = "Send a message…"
# Shift+Tab from Manual walks the chip's menu order.
_HOTKEY_STEPS = [
    ("auto", "Auto", "auto mode on"),
    ("acceptEdits", "Accept edits", "accept edits on"),
    ("plan", "Plan", "plan mode on"),
]


@pytest.fixture
def native_claude_auto_capable_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    claude_managed_settings: None,
) -> Iterator[tuple[str, str]]:
    """A real Claude CLI in Manual, on a model whose cycle includes Auto."""
    with native_claude_mock_session_context(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        terminal_launch_args=["--model", "claude-opus-4-8", "--permission-mode", "default"],
    ) as session:
        yield session


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_shift_tab_cycles_permission_mode_from_composer(
    request: pytest.FixtureRequest,
    native_claude_auto_capable_session: tuple[str, str],
) -> None:
    """Shift+Tab in the composer steps the chip and Claude's own footer together.

    The draft and focus stay in the composer, and each press is one PATCH
    applied through the same path as the picker.
    """
    base_url, session_id = native_claude_auto_capable_session
    _wait_for_footer(base_url, session_id, "manual mode on", timeout_s=_FOOTER_TIMEOUT_S)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    chip = page.get_by_test_id("composer-permission-chip")
    expect(chip).to_be_visible(timeout=60_000)
    expect(chip).to_have_text("Manual")
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    composer.click()
    composer.press_sequentially("draft survives the mode switch")
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        # Show the starting mode in the recording before the first press.
        page.wait_for_timeout(1_500)

    for mode, label, footer in _HOTKEY_STEPS:
        with page.expect_response(
            lambda r: r.request.method == "PATCH" and r.url.endswith(f"/v1/sessions/{session_id}"),
            timeout=_SWITCH_TIMEOUT_MS,
        ) as switched:
            page.keyboard.press("Shift+Tab")
        response = switched.value
        if response.status != 200:
            pytest.fail(f"Shift+Tab switch to {mode} rejected: {response.text()}")
        assert json.loads(response.request.post_data or "{}") == {"permission_mode": mode}
        expect(chip).to_have_text(label)
        expect(composer).to_be_focused()
        expect(composer).to_have_value("draft survives the mode switch")
        _wait_for_footer(base_url, session_id, footer, timeout_s=15.0)

    # End on Claude's own pane so the footer the chip promised is on screen.
    page.get_by_test_id("view-mode-terminal").click()
    expect(page.get_by_test_id("terminal-view").last).to_be_visible(timeout=30_000)
    _wait_for_footer(base_url, session_id, "plan mode on", timeout_s=15.0)
