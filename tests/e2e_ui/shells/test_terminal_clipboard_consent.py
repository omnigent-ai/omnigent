"""E2E: terminal clipboard consent is noticeable, persistent, and revocable.

Most cases isolate the UI with terminal/WebSocket mocks. The native-helper
regression uses a real managed shell and transport. Browser clipboard writes
are recorded in-page without touching the OS clipboard.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, WebSocketRoute, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle, open_right_rail

_TERMINAL_ID = "terminal_clipboard_e2e"
_TERMINAL_LABEL = "bash · clipboard-e2e"
_REMEMBER_LABEL = "Remember my choice"
_INITIAL_CLIPBOARD = "clipboard before terminal copy"
_CLIPBOARD_STUB = """
window.__terminalClipboard = {
  text: "clipboard before terminal copy",
  writes: [],
  rejectWrites: false,
};
Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: {
    writeText: async (text) => {
      if (window.__terminalClipboard.rejectWrites) {
        throw new DOMException("A user gesture is required", "NotAllowedError");
      }
      window.__terminalClipboard.text = text;
      window.__terminalClipboard.writes.push(text);
    },
    readText: async () => window.__terminalClipboard.text,
  },
});
const originalExecCommand = document.execCommand.bind(document);
document.execCommand = (command, ...args) => {
  if (command.toLowerCase() === "copy") return false;
  return originalExecCommand(command, ...args);
};
"""


def _create_session(live_server: str) -> str:
    response = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _build_hello_world_bundle(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["session_id"]


def _terminal_id(session_id: str) -> str:
    return f"{_TERMINAL_ID}_{session_id}"


def _clipboard_frame(text: str) -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return json.dumps(
        {
            "type": "clipboard-write",
            "encoding": "base64",
            "data": encoded,
        },
        separators=(",", ":"),
    )


def _expect_clipboard(page: Page, expected: str) -> None:
    page.wait_for_function(
        "expected => window.__terminalClipboard.text === expected",
        arg=expected,
    )


@dataclass
class _ClipboardBrowser:
    page: Page
    base_url: str
    session_ids: tuple[str, str]
    sockets: dict[str, WebSocketRoute]
    inputs: list[bytes]
    resizes: dict[str, list[tuple[int, int]]]
    active_session: int = 0

    @property
    def terminal(self) -> Locator:
        return self.page.locator('[data-testid="terminal-view"]:visible').first

    @property
    def consent(self) -> Locator:
        return self.page.get_by_test_id("terminal-clipboard-consent")

    @property
    def textarea(self) -> Locator:
        return self.terminal.locator("textarea.xterm-helper-textarea")

    def open(self, session: int = 0) -> None:
        self.active_session = session
        self.page.goto(f"{self.base_url}/c/{self.session_ids[session]}")
        self.reveal()

    def reload(self) -> None:
        self.page.reload()
        self.reveal()

    def reveal(self) -> None:
        if self.page.viewport_size and self.page.viewport_size["width"] < 768:
            self.page.get_by_role("button", name="Conversation actions").click()
            self.page.get_by_role("menuitem", name=re.compile(r"^Shells\b")).click()
            drawer = self.page.get_by_test_id("shells-panel-drawer")
            drawer.get_by_role("button", name=re.compile("clipboard-e2e")).click()
        else:
            open_right_rail(self.page)
            rail = self.page.get_by_role("complementary", name="Workspace")
            rail.get_by_text(_TERMINAL_LABEL, exact=True).click()
        expect(self.terminal).to_have_attribute("data-state", "connected", timeout=20_000)
        assert _terminal_id(self.session_ids[self.active_session]) in self.sockets

    def request_copy(self, text: str) -> None:
        self.textarea.focus()
        self.page.keyboard.type("a")  # Satisfy the recent-input gate.
        self.sockets[_terminal_id(self.session_ids[self.active_session])].send(
            _clipboard_frame(text)
        )

    def copy_selection(self, text: str) -> str:
        """Select real xterm output; simulate clipboard commit without touching the OS."""
        terminal_id = _terminal_id(self.session_ids[self.active_session])
        self.sockets[terminal_id].send(b"\x1b[2J\x1b[H" + text.encode() + b"\r\n")
        self.page.wait_for_timeout(200)  # Let xterm's buffered parser render the line.
        screen = self.terminal.locator(".xterm-screen")
        screen.click(trial=True)
        bounds = screen.bounding_box()
        assert bounds is not None
        cols, rows = self.resizes[terminal_id][-1]
        cell_width = bounds["width"] / cols
        y = bounds["y"] + bounds["height"] / rows / 2
        self.page.mouse.move(bounds["x"] + cell_width / 4, y)
        self.page.mouse.down()
        self.page.mouse.move(bounds["x"] + (len(text) + 0.25) * cell_width, y, steps=5)
        self.page.mouse.up()
        return self.textarea.evaluate(
            """element => {
                const data = new DataTransfer();
                const event = new ClipboardEvent("copy", {
                    clipboardData: data, bubbles: true, cancelable: true,
                });
                element.dispatchEvent(event);
                const text = data.getData("text/plain");
                if (data.types.includes("text/plain")) {
                    window.__terminalClipboard.text = text;
                    window.__terminalClipboard.writes.push(text);
                }
                return text;
            }"""
        )

    def expect_no_copy(self) -> None:
        # Let the injected WebSocket frame reach the browser before checking.
        self.page.wait_for_timeout(200)
        assert self.page.evaluate("window.__terminalClipboard.writes") == []


@pytest.fixture
def clipboard_browser(page: Page, live_server: str) -> Iterator[_ClipboardBrowser]:
    """Unbound sessions and mocked terminals require no runner or tmux process."""
    session_ids = (_create_session(live_server), _create_session(live_server))
    sockets: dict[str, WebSocketRoute] = {}
    inputs: list[bytes] = []
    resizes: dict[str, list[tuple[int, int]]] = {}
    page.add_init_script(_CLIPBOARD_STUB)

    terminal_list = re.compile(
        rf"/v1/sessions/({'|'.join(map(re.escape, session_ids))})/resources/terminals(?:\?|$)"
    )

    def _serve_terminal(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        match = terminal_list.search(route.request.url)
        assert match is not None
        terminal_id = _terminal_id(match.group(1))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": terminal_id,
                            "object": "terminal",
                            "name": "bash",
                            "metadata": {
                                "terminal_name": "bash",
                                "session_key": "clipboard-e2e",
                                "running": True,
                            },
                        }
                    ],
                    "first_id": terminal_id,
                    "last_id": terminal_id,
                    "has_more": False,
                }
            ),
        )

    def _attach(ws: WebSocketRoute) -> None:
        terminal_id = ws.url.split("/resources/terminals/", 1)[1].split("/", 1)[0]
        sockets[terminal_id] = ws
        resizes.setdefault(terminal_id, [])

        def _record_input(message: str | bytes) -> None:
            if isinstance(message, bytes):
                inputs.append(message)
            else:
                control = json.loads(message)
                if control.get("type") == "resize":
                    resizes[terminal_id].append((control["cols"], control["rows"]))

        ws.on_message(_record_input)

    page.route(terminal_list, _serve_terminal)
    page.route_web_socket(
        re.compile(rf"/resources/terminals/{_TERMINAL_ID}_[^/]+/attach"),
        _attach,
    )
    try:
        yield _ClipboardBrowser(page, live_server, session_ids, sockets, inputs, resizes)
    finally:
        for session_id in session_ids:
            httpx.delete(
                f"{live_server}/v1/sessions/{session_id}", timeout=10.0
            ).raise_for_status()


@pytest.mark.parametrize(
    ("width", "height"),
    [(1280, 844), (390, 844), (390, 480), (320, 400)],
    ids=["desktop", "mobile", "short-mobile", "narrow-short-mobile"],
)
def test_terminal_clipboard_popup_floats_bottom_right_without_resizing_or_stealing_focus(
    clipboard_browser: _ClipboardBrowser, width: int, height: int
) -> None:
    ui = clipboard_browser
    ui.page.set_viewport_size({"width": width, "height": height})
    ui.open()
    screen = ui.terminal.locator(".xterm-screen")
    ui.page.evaluate("document.fonts.ready")
    screen.click(trial=True)  # Wait for the shell drawer's layout to settle.
    original_screen_bounds = screen.bounding_box()
    assert original_screen_bounds is not None
    terminal_id = _terminal_id(ui.session_ids[0])
    original_resizes = list(ui.resizes[terminal_id])
    assert original_resizes

    ui.request_copy("pending selection")

    expect(ui.consent).to_be_visible()
    expect(ui.consent).to_have_attribute("role", "region")
    expect(ui.consent).to_contain_text("Allow copying from terminals?")
    expect(ui.consent).to_contain_text("Your selection hasn’t been copied yet.")
    expect(ui.consent).to_contain_text(
        "Allowing copying also lets terminal programs silently replace your "
        "clipboard with text or commands you didn’t intend to paste."
    )
    expect(ui.consent.get_by_role("checkbox", name=_REMEMBER_LABEL)).to_be_checked()
    expect(ui.textarea).to_be_focused()
    # Trial actions wait for the notification's entry animation without focusing it.
    ui.consent.get_by_role("button", name="Allow copying", exact=True).click(trial=True)

    bounds = ui.consent.bounding_box()
    screen_bounds = screen.bounding_box()
    assert bounds is not None and screen_bounds is not None
    assert 0 <= bounds["x"] < bounds["x"] + bounds["width"] <= width + 1
    assert 0 <= bounds["y"] < bounds["y"] + bounds["height"] <= height
    assert 0 <= width - bounds["x"] - bounds["width"] <= 32
    assert 0 <= height - bounds["y"] - bounds["height"] <= 32
    if width >= 768:
        assert 440 <= bounds["width"] <= 520
    else:
        assert width - 40 <= bounds["width"] <= width
    assert screen_bounds == pytest.approx(original_screen_bounds, abs=1)
    assert screen_bounds["height"] >= 80

    remember_bounds = ui.consent.get_by_role("checkbox", name=_REMEMBER_LABEL).bounding_box()
    assert remember_bounds is not None
    expect(ui.consent).not_to_contain_text("On this server, in this browser or app.")
    expect(ui.consent).to_contain_text("Change this in Settings → General.")
    for paragraph in ui.consent.locator("p").all():
        expect(paragraph).to_have_css("text-align", "left")
        text_left = paragraph.evaluate(
            """element => {
                const range = document.createRange();
                range.selectNodeContents(element);
                return range.getBoundingClientRect().left;
            }"""
        )
        assert text_left == pytest.approx(remember_bounds["x"], abs=1)

    for name in ("Allow copying", "Copy once", "Block", "Dismiss clipboard request"):
        action = ui.consent.get_by_role("button", name=name, exact=True)
        expect(action).to_be_in_viewport(ratio=1)
        action_bounds = action.bounding_box()
        assert action_bounds is not None
        assert bounds["y"] <= action_bounds["y"]
        assert action_bounds["y"] + action_bounds["height"] <= bounds["y"] + bounds["height"]
        action.click(trial=True, timeout=5_000)

    ui.page.keyboard.type("still typing")
    expect(ui.textarea).to_be_focused()
    expect(ui.consent).to_be_visible()
    assert b"still typing" in b"".join(ui.inputs)
    ui.expect_no_copy()
    _expect_clipboard(ui.page, _INITIAL_CLIPBOARD)
    assert ui.resizes[terminal_id] == original_resizes

    if screenshot_dir := os.environ.get("E2E_SCREENSHOT_DIR"):
        directory = Path(screenshot_dir)
        directory.mkdir(parents=True, exist_ok=True)
        ui.page.screenshot(path=str(directory / f"terminal-clipboard-{width}x{height}.png"))

    if width == 320:
        remember = ui.consent.get_by_role("checkbox", name=_REMEMBER_LABEL)
        remember.scroll_into_view_if_needed()
        expect(remember).to_be_in_viewport(ratio=1)
        remember.uncheck()
        expect(
            ui.consent.get_by_role("button", name="Allow for this session", exact=True)
        ).to_be_in_viewport(ratio=1)
        remember.check()
        expect(remember).to_be_checked()
        expect(
            ui.consent.get_by_role("button", name="Allow copying", exact=True)
        ).to_be_in_viewport(ratio=1)

    ui.consent.get_by_role("button", name="Allow copying", exact=True).click()
    _expect_clipboard(ui.page, "pending selection")
    expect(ui.consent).to_have_count(0)
    expect(ui.textarea).to_be_focused()
    assert screen.bounding_box() == pytest.approx(original_screen_bounds, abs=1)
    assert ui.resizes[terminal_id] == original_resizes

    ui.request_copy("next selection")
    _expect_clipboard(ui.page, "next selection")
    expect(ui.consent).to_have_count(0)


def test_terminal_clipboard_remembered_grant_survives_reload_and_new_session(
    clipboard_browser: _ClipboardBrowser,
) -> None:
    ui = clipboard_browser
    ui.open()
    ui.request_copy("first selection")
    ui.consent.get_by_role("button", name="Allow copying", exact=True).click()
    _expect_clipboard(ui.page, "first selection")
    expect(ui.consent).to_have_count(0)

    ui.request_copy("second selection")
    _expect_clipboard(ui.page, "second selection")
    expect(ui.consent).to_have_count(0)

    ui.reload()
    ui.request_copy("selection after reload")
    _expect_clipboard(ui.page, "selection after reload")
    expect(ui.consent).to_have_count(0)

    ui.open(session=1)
    ui.request_copy("different session and terminal")
    _expect_clipboard(ui.page, "different session and terminal")
    expect(ui.consent).to_have_count(0)


def test_terminal_clipboard_pending_copy_stays_clickable_after_disconnect(
    clipboard_browser: _ClipboardBrowser,
) -> None:
    ui = clipboard_browser
    ui.open()
    ui.request_copy("selection before disconnect")
    expect(ui.consent).to_be_visible()

    ui.sockets[_terminal_id(ui.session_ids[0])].close(code=1000, reason="Terminal exited")
    expect(ui.terminal).to_have_attribute("data-state", "closed")
    ui.consent.get_by_role("button", name="Copy once", exact=True).click(timeout=5_000)
    _expect_clipboard(ui.page, "selection before disconnect")
    expect(ui.consent).to_have_count(0)


def test_terminal_clipboard_prompt_and_session_grant(
    clipboard_browser: _ClipboardBrowser,
) -> None:
    ui = clipboard_browser
    ui.open()
    ui.request_copy("first selection")
    ui.consent.get_by_role("checkbox", name=_REMEMBER_LABEL).uncheck()
    ui.consent.get_by_role("button", name="Allow for this session", exact=True).click()
    _expect_clipboard(ui.page, "first selection")

    ui.request_copy("second selection")
    _expect_clipboard(ui.page, "second selection")
    expect(ui.consent).to_have_count(0)

    ui.reload()
    ui.request_copy("selection after reload")
    expect(ui.consent).to_be_visible()
    ui.expect_no_copy()

    ui.open(session=1)
    ui.request_copy("different session and terminal")
    expect(ui.consent).to_be_visible()
    ui.expect_no_copy()


@pytest.mark.parametrize("remember", [True, False], ids=["remember", "session-only"])
def test_terminal_clipboard_copy_once_never_remembers_permission(
    clipboard_browser: _ClipboardBrowser, remember: bool
) -> None:
    ui = clipboard_browser
    ui.open()
    ui.request_copy("one-time selection")
    ui.consent.get_by_role("checkbox", name=_REMEMBER_LABEL).set_checked(remember)
    ui.consent.get_by_role("button", name="Copy once", exact=True).click()
    _expect_clipboard(ui.page, "one-time selection")
    expect(ui.consent).to_have_count(0)

    ui.request_copy("another selection")
    expect(ui.consent).to_be_visible()
    _expect_clipboard(ui.page, "one-time selection")
    assert ui.page.evaluate("window.__terminalClipboard.writes") == ["one-time selection"]

    ui.open(session=1)
    ui.request_copy("different session and terminal")
    expect(ui.consent).to_be_visible()
    ui.expect_no_copy()


@pytest.mark.parametrize("remember", [True, False], ids=["remember", "session-only"])
def test_terminal_clipboard_block_honors_remember_choice(
    clipboard_browser: _ClipboardBrowser, remember: bool
) -> None:
    ui = clipboard_browser
    ui.open()
    ui.request_copy("blocked selection")
    ui.consent.get_by_role("checkbox", name=_REMEMBER_LABEL).set_checked(remember)
    ui.consent.get_by_role("button", name="Block", exact=True).click()

    ui.request_copy("still blocked selection")
    ui.expect_no_copy()
    expect(ui.consent).to_have_count(0)

    ui.reload()
    ui.request_copy("selection after reload")
    ui.expect_no_copy()
    expect(ui.consent).to_have_count(0 if remember else 1)

    ui.open(session=1)
    ui.request_copy("different session and terminal")
    ui.expect_no_copy()
    expect(ui.consent).to_have_count(0 if remember else 1)


def test_terminal_clipboard_browser_retry_preserves_remembered_permission(
    clipboard_browser: _ClipboardBrowser,
) -> None:
    ui = clipboard_browser
    ui.open()
    ui.request_copy("initial selection")
    ui.consent.get_by_role("button", name="Allow copying", exact=True).click()
    _expect_clipboard(ui.page, "initial selection")
    expect(ui.consent).to_have_count(0)

    ui.page.evaluate("window.__terminalClipboard.rejectWrites = true")
    ui.request_copy("selection needing a click")
    expect(ui.consent).to_contain_text("Copy needs a click")
    expect(ui.consent.get_by_role("checkbox")).to_have_count(0)
    _expect_clipboard(ui.page, "initial selection")

    ui.page.evaluate("window.__terminalClipboard.rejectWrites = false")
    ui.consent.get_by_role("button", name="Copy now", exact=True).click()
    _expect_clipboard(ui.page, "selection needing a click")
    expect(ui.consent).to_have_count(0)

    ui.page.evaluate("window.__terminalClipboard.rejectWrites = true")
    ui.request_copy("dismissed selection")
    expect(ui.consent).to_contain_text("Copy needs a click")
    ui.consent.get_by_role("button", name="Dismiss", exact=True).click()
    expect(ui.consent).to_have_count(0)

    ui.page.evaluate("window.__terminalClipboard.rejectWrites = false")
    ui.request_copy("selection after dismissal")
    expect(ui.consent).to_contain_text("Copy needs a click")
    expect(ui.consent.get_by_role("checkbox")).to_have_count(0)
    _expect_clipboard(ui.page, "selection needing a click")
    ui.consent.get_by_role("button", name="Copy now", exact=True).click()
    _expect_clipboard(ui.page, "selection after dismissal")
    expect(ui.consent).to_have_count(0)

    ui.request_copy("automatic selection after retry")
    _expect_clipboard(ui.page, "automatic selection after retry")
    expect(ui.consent).to_have_count(0)

    ui.reload()
    ui.request_copy("automatic selection after reload")
    _expect_clipboard(ui.page, "automatic selection after reload")
    expect(ui.consent).to_have_count(0)


@pytest.mark.parametrize("finish", ["copy-now", "new-selection"])
def test_terminal_clipboard_pending_selection_survives_a_cross_tab_grant(
    clipboard_browser: _ClipboardBrowser, finish: str
) -> None:
    ui = clipboard_browser
    ui.open()
    ui.request_copy("selection waiting for permission")
    expect(ui.consent).to_be_visible()

    settings = ui.page.context.new_page()
    try:
        settings.goto(f"{ui.base_url}/settings/general")
        preference = settings.get_by_test_id("terminal-clipboard-preference-select")
        expect(preference).to_contain_text("Ask before copying")
        preference.click()
        settings.get_by_role("option", name="Allow copying", exact=True).click()
        expect(preference).to_contain_text("Allow copying")

        ui.page.bring_to_front()
        expect(ui.consent).to_contain_text("Finish copying terminal text")
        expect(ui.consent).to_contain_text("The requested text hasn’t been copied yet.")
        expect(ui.consent.get_by_role("checkbox")).to_have_count(0)
        ui.expect_no_copy()

        if finish == "copy-now":
            ui.consent.get_by_role("button", name="Copy now", exact=True).click()
            _expect_clipboard(ui.page, "selection waiting for permission")
        else:
            ui.request_copy("newer selection")
            _expect_clipboard(ui.page, "newer selection")
        expect(ui.consent).to_have_count(0)

        ui.request_copy("automatic selection after shared grant")
        _expect_clipboard(ui.page, "automatic selection after shared grant")
        expect(ui.consent).to_have_count(0)
    finally:
        settings.close()


@pytest.mark.parametrize("source", ["program", "selection"])
def test_terminal_clipboard_settings_revokes_a_mounted_terminals_permission(
    clipboard_browser: _ClipboardBrowser,
    source: str,
) -> None:
    ui = clipboard_browser
    ui.open()
    request_copy = ui.request_copy if source == "program" else ui.copy_selection
    request_copy("initial selection")
    ui.consent.get_by_role("button", name="Allow copying", exact=True).click()
    _expect_clipboard(ui.page, "initial selection")

    settings = ui.page.context.new_page()
    try:
        settings.goto(f"{ui.base_url}/settings/general")
        preference = settings.get_by_test_id("terminal-clipboard-preference-select")
        expect(preference).to_contain_text("Allow copying")

        for option in ("Ask before copying", "Block copying", "Allow copying"):
            settings.bring_to_front()
            preference.click()
            settings.get_by_role("option", name=option, exact=True).click()
            expect(preference).to_contain_text(option)

            if option == "Ask before copying" and (
                screenshot_dir := os.environ.get("E2E_SCREENSHOT_DIR")
            ):
                directory = Path(screenshot_dir)
                directory.mkdir(parents=True, exist_ok=True)
                settings.screenshot(
                    path=str(directory / "terminal-clipboard-settings-revoked.png"),
                    full_page=True,
                )

            ui.page.bring_to_front()
            request_copy(f"selection with {option}")
            if option == "Allow copying":
                _expect_clipboard(ui.page, f"selection with {option}")
                expect(ui.consent).to_have_count(0)
            else:
                ui.page.wait_for_timeout(200)
                _expect_clipboard(ui.page, "initial selection")
                expect(ui.consent).to_have_count(1 if option == "Ask before copying" else 0)
                assert ui.page.evaluate("window.__terminalClipboard.writes") == [
                    "initial selection"
                ]
    finally:
        settings.close()

    ui.reload()
    request_copy("automatic selection after settings change")
    _expect_clipboard(ui.page, "automatic selection after settings change")
    expect(ui.consent).to_have_count(0)


def test_terminal_clipboard_browser_selection_asks_and_copy_once_does_not_remember(
    clipboard_browser: _ClipboardBrowser,
) -> None:
    ui = clipboard_browser
    ui.open()

    assert ui.copy_selection("first browser selection") == ""
    expect(ui.consent).to_be_visible()
    ui.expect_no_copy()
    ui.consent.get_by_role("button", name="Copy once", exact=True).click()
    _expect_clipboard(ui.page, "first browser selection")
    expect(ui.consent).to_have_count(0)

    assert ui.copy_selection("second browser selection") == ""
    expect(ui.consent).to_be_visible()
    _expect_clipboard(ui.page, "first browser selection")
    ui.consent.get_by_role("button", name="Allow copying", exact=True).click()
    _expect_clipboard(ui.page, "second browser selection")

    ui.reload()
    assert ui.copy_selection("selection after reload") == "selection after reload"
    _expect_clipboard(ui.page, "selection after reload")
    expect(ui.consent).to_have_count(0)
    ui.open(session=1)
    assert ui.copy_selection("selection from another session") == "selection from another session"
    _expect_clipboard(ui.page, "selection from another session")
    expect(ui.consent).to_have_count(0)


@pytest.mark.parametrize("remember", [True, False], ids=["remember", "session-only"])
def test_terminal_clipboard_settings_blocks_selection_copying_after_returning_to_session(
    clipboard_browser: _ClipboardBrowser,
    remember: bool,
) -> None:
    ui = clipboard_browser
    ui.open()
    assert ui.copy_selection("initial browser selection") == ""
    ui.consent.get_by_role("checkbox", name=_REMEMBER_LABEL).set_checked(remember)
    allow = "Allow copying" if remember else "Allow for this session"
    ui.consent.get_by_role("button", name=allow, exact=True).click()
    _expect_clipboard(ui.page, "initial browser selection")
    assert ui.copy_selection("allowed browser selection") == "allowed browser selection"
    _expect_clipboard(ui.page, "allowed browser selection")

    ui.page.get_by_test_id("settings-button").click()
    preference = ui.page.get_by_test_id("terminal-clipboard-preference-select")
    expect(preference).to_contain_text("Allow copying" if remember else "Ask before copying")
    preference.click()
    ui.page.get_by_role("option", name="Block copying", exact=True).click()
    expect(preference).to_contain_text("Block copying")
    ui.page.get_by_role("link", name="Back", exact=True).click()
    expect(ui.terminal).to_have_attribute("data-state", "connected", timeout=20_000)

    assert ui.copy_selection("blocked browser selection") == ""
    expect(ui.page.get_by_text("Copying from this terminal is blocked.")).to_be_visible()
    _expect_clipboard(ui.page, "allowed browser selection")
    expect(ui.consent).to_have_count(0)
    if remember and (screenshot_dir := os.environ.get("E2E_SCREENSHOT_DIR")):
        directory = Path(screenshot_dir)
        directory.mkdir(parents=True, exist_ok=True)
        ui.page.screenshot(path=str(directory / "terminal-clipboard-selection-blocked.png"))
    ui.request_copy("blocked program selection")
    ui.page.wait_for_timeout(200)
    _expect_clipboard(ui.page, "allowed browser selection")

    ui.reload()
    assert ui.copy_selection("blocked selection after reload") == ""
    ui.expect_no_copy()
    expect(ui.consent).to_have_count(0)
    ui.open(session=1)
    assert ui.copy_selection("blocked selection from another session") == ""
    ui.expect_no_copy()
    expect(ui.consent).to_have_count(0)


def test_native_clipboard_helper_obeys_live_settings_in_existing_terminal(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Exercise the real shell → native helper → tmux → browser consent path."""
    base_url, session_id = terminal_session
    page.add_init_script(_CLIPBOARD_STUB)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()
    terminal = rail.get_by_test_id("terminal-view").last
    expect(terminal).to_have_attribute("data-state", "connected", timeout=60_000)
    textarea = terminal.locator("textarea.xterm-helper-textarea")
    consent = page.get_by_test_id("terminal-clipboard-consent")

    # A regressed launch must fail the test without touching the machine's clipboard.
    native_bin = tmp_path / "native-bin"
    native_bin.mkdir()
    bypass_marker = tmp_path / "bypassed-consent"
    native_copy = native_bin / "pbcopy"
    native_copy.write_text(f"#!/bin/sh\ncat > {shlex.quote(str(bypass_marker))}\n")
    native_copy.chmod(0o700)

    def run_in_terminal(command: str, marker: str) -> None:
        done = tmp_path / marker
        textarea.focus()
        page.keyboard.insert_text(f"{command}; printf done > {shlex.quote(str(done))}")
        page.keyboard.press("Enter")
        deadline = time.monotonic() + 10
        while not done.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert done.exists(), "real terminal command did not finish"
        assert not bypass_marker.exists(), "native clipboard command bypassed consent"

    fallback = shlex.quote(str(native_bin))
    run_in_terminal(
        'case "${PATH%%:*}" in */clipboard-bin) '
        f'export PATH="${{PATH%%:*}}":{fallback}:"${{PATH#*:}}";; '
        f'*) export PATH={fallback}:"$PATH";; esac',
        "path-ready",
    )

    def native_copy_text(text: str, marker: str) -> None:
        run_in_terminal(f"printf %s {shlex.quote(text)} | pbcopy", marker)

    native_copy_text("native copy waiting for consent λ", "ask-done")
    expect(consent).to_be_visible()
    _expect_clipboard(page, _INITIAL_CLIPBOARD)
    consent.get_by_role("button", name="Allow copying", exact=True).click()
    _expect_clipboard(page, "native copy waiting for consent λ")
    native_copy_text("native copy already allowed", "allow-done")
    _expect_clipboard(page, "native copy already allowed")

    settings = page.context.new_page()
    try:
        settings.goto(f"{base_url}/settings/general")
        preference = settings.get_by_test_id("terminal-clipboard-preference-select")
        for option in ("Block copying", "Ask before copying", "Allow copying"):
            settings.bring_to_front()
            preference.click()
            settings.get_by_role("option", name=option, exact=True).click()
            expect(preference).to_contain_text(option)
            page.bring_to_front()
            native_copy_text(f"native copy with {option}", f"{option.split()[0]}-done")
            if option == "Allow copying":
                _expect_clipboard(page, f"native copy with {option}")
                expect(consent).to_have_count(0)
            else:
                if option == "Ask before copying":
                    expect(consent).to_be_visible()
                else:
                    expect(
                        page.get_by_text("Copying from this terminal is blocked.")
                    ).to_be_visible()
                    expect(consent).to_have_count(0)
                _expect_clipboard(page, "native copy already allowed")
    finally:
        settings.close()
    expect(terminal).to_have_attribute("data-state", "connected")
