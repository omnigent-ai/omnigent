"""E2E: terminal clipboard writes require consent, whatever their source.

Two sources reach the same gate:

- a validated tmux ``clipboard-write`` control frame (a copy-mode selection),
- raw pane OSC 52, which control mode forwards verbatim -- how a TUI running
  in the pane (pi's copy action, vim's ``"+`` register) asks to copy.

The terminal resource and attach WebSocket are browser-route mocks. This keeps
these tests tmux-free while exercising the built SPA's real TerminalView,
TerminalSession, xterm OSC parsing and input bookkeeping, WebSocket parsing,
Sonner consent UI, and browser Clipboard API.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, WebSocketRoute, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle, open_right_rail

_TERMINAL_ID = "terminal_clipboard_e2e"
_TERMINAL_LABEL = "bash · clipboard-e2e"


@pytest.fixture
def clipboard_ui_session(live_server: str) -> Iterator[tuple[str, str]]:
    """Create an unbound session; no runner or tmux terminal is launched."""
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
    session_id = response.json()["session_id"]
    try:
        yield live_server, session_id
    finally:
        httpx.delete(
            f"{live_server}/v1/sessions/{session_id}",
            timeout=10.0,
        ).raise_for_status()


def _clipboard_frame(text: str) -> str:
    """Build the validated server->browser ``clipboard-write`` text frame."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return json.dumps(
        {
            "type": "clipboard-write",
            "encoding": "base64",
            "data": encoded,
        },
        separators=(",", ":"),
    )


def _osc52_write(text: str) -> bytes:
    """Build the raw pane bytes a TUI emits to copy: ``ESC ] 52 ; c ; <b64> BEL``."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{encoded}\x07".encode("ascii")


def _expect_clipboard(page: Page, expected: str) -> None:
    """Poll because navigator.clipboard.writeText resolves asynchronously."""
    deadline = time.monotonic() + 5
    actual = ""
    while time.monotonic() < deadline:
        actual = page.evaluate("() => navigator.clipboard.readText()")
        if actual == expected:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"clipboard was {actual!r}, expected {expected!r}")


def _mount_mock_terminal(
    page: Page, base_url: str, session_id: str
) -> tuple[Locator, list[WebSocketRoute]]:
    """Mock the terminal resource and attach socket, then open the shell tab.

    :returns: The connected ``terminal-view`` locator and the captured attach
        sockets, whose last entry is the live one to inject server frames on.
    """
    sockets: list[WebSocketRoute] = []

    terminal_list = re.compile(
        rf"/v1/sessions/{re.escape(session_id)}/resources/terminals(?:\?|$)"
    )

    def _serve_terminal(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": _TERMINAL_ID,
                            "object": "terminal",
                            "name": "bash",
                            "metadata": {
                                "terminal_name": "bash",
                                "session_key": "clipboard-e2e",
                                "running": True,
                            },
                        }
                    ],
                    "first_id": _TERMINAL_ID,
                    "last_id": _TERMINAL_ID,
                    "has_more": False,
                }
            ),
        )

    def _attach(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        # Swallow resize/input frames; the tests inject server frames below.
        ws.on_message(lambda _message: None)

    page.route(terminal_list, _serve_terminal)
    page.route_web_socket(
        re.compile(rf"/resources/terminals/{re.escape(_TERMINAL_ID)}/attach"),
        _attach,
    )
    page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=base_url,
    )

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    rail = page.get_by_role("complementary", name="Workspace")
    shell_tab = rail.get_by_text(_TERMINAL_LABEL, exact=True)
    expect(shell_tab).to_be_visible(timeout=30_000)
    shell_tab.click()

    terminal = rail.get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=20_000)
    assert sockets, "mock terminal attach WebSocket did not connect"
    return terminal, sockets


def test_terminal_clipboard_prompt_and_session_grant(
    page: Page,
    clipboard_ui_session: tuple[str, str],
) -> None:
    """The first copy asks; a session grant makes the next copy automatic."""
    base_url, session_id = clipboard_ui_session
    terminal, sockets = _mount_mock_terminal(page, base_url, session_id)

    textarea = terminal.locator("textarea.xterm-helper-textarea")
    consent = page.get_by_test_id("terminal-clipboard-consent")

    first = f"terminal-first-{uuid.uuid4().hex}"
    textarea.focus()
    page.keyboard.type("a")  # Satisfy TerminalSession's recent-input gate.
    sockets[-1].send(_clipboard_frame(first))

    expect(consent).to_be_visible()
    expect(consent).to_contain_text("Allow this terminal to copy to your clipboard?")
    expect(consent.get_by_role("button", name="Allow for this session")).to_be_visible()
    expect(consent.get_by_role("button", name="Copy once")).to_be_visible()
    expect(consent.get_by_role("button", name="Block")).to_be_visible()

    consent.get_by_role("button", name="Allow for this session").click()
    _expect_clipboard(page, first)
    expect(consent).to_have_count(0)

    # A second frame in the same mounted TerminalView copies automatically.
    second = f"terminal-second-{uuid.uuid4().hex}"
    textarea.focus()
    page.keyboard.type("b")
    sockets[-1].send(_clipboard_frame(second))

    _expect_clipboard(page, second)
    expect(consent).to_have_count(0)


def test_pane_osc52_copy_prompts_then_reaches_clipboard(
    page: Page,
    clipboard_ui_session: tuple[str, str],
) -> None:
    """A TUI's in-pane copy reaches the clipboard, behind the same prompt.

    Control mode forwards raw pane output, so a TUI's copy arrives as an OSC 52
    sequence with no tmux buffer behind it -- the shape ``set-clipboard
    external`` keeps out of tmux, but not off this socket. It must land on the
    clipboard the way a tmux selection does, through the consent prompt, and
    never by writing straight to it: pane output is agent-controlled, so an
    unprompted write would let injected content replace what the user copies.
    """
    base_url, session_id = clipboard_ui_session
    terminal, sockets = _mount_mock_terminal(page, base_url, session_id)

    textarea = terminal.locator("textarea.xterm-helper-textarea")
    consent = page.get_by_test_id("terminal-clipboard-consent")

    copied = f"osc52-copy-{uuid.uuid4().hex}"
    textarea.focus()
    page.keyboard.type("a")  # Satisfy TerminalSession's recent-input gate.
    sockets[-1].send(_osc52_write(copied))

    expect(consent).to_be_visible()
    expect(consent).to_contain_text("Allow this terminal to copy to your clipboard?")

    consent.get_by_role("button", name="Copy once").click()
    _expect_clipboard(page, copied)
    expect(consent).to_have_count(0)


def test_pane_osc52_read_request_is_never_answered(
    page: Page,
    clipboard_ui_session: tuple[str, str],
) -> None:
    """``OSC 52 ; c ; ?`` asks to read the clipboard; it must produce nothing.

    Answering would hand agent-controlled pane output whatever the user last
    copied. The sequence is consumed with no prompt and no reply.
    """
    base_url, session_id = clipboard_ui_session
    terminal, sockets = _mount_mock_terminal(page, base_url, session_id)

    sent: list[str | bytes] = []

    def _record(message: str | bytes) -> None:
        # A plain function, not ``sent.append``: Playwright tags handlers with
        # an attribute, which a builtin method rejects.
        sent.append(message)

    sockets[-1].on_message(_record)

    textarea = terminal.locator("textarea.xterm-helper-textarea")
    consent = page.get_by_test_id("terminal-clipboard-consent")

    secret = f"never-disclosed-{uuid.uuid4().hex}"
    page.evaluate("(text) => navigator.clipboard.writeText(text)", secret)

    textarea.focus()
    page.keyboard.type("a")  # Satisfy the recent-input gate; still no answer.
    sent.clear()
    sockets[-1].send(b"\x1b]52;c;?\x07")

    # No prompt, and nothing containing the clipboard goes back to the pane.
    expect(consent).to_have_count(0)
    page.wait_for_timeout(500)
    for message in sent:
        payload = message if isinstance(message, str) else message.decode("utf-8", "replace")
        assert secret not in payload, f"clipboard leaked to the pane: {payload!r}"
