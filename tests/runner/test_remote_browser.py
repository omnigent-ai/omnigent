"""Tests for the runner's remote browser bridge."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest

from omnigent.runner.remote_browser import (
    WS_CLOSE_REMOTE_BROWSER_NOT_CONFIGURED,
    WS_CLOSE_REMOTE_BROWSER_PAGE_GONE,
    WS_CLOSE_REMOTE_BROWSER_UNREACHABLE,
    CdpConnection,
    RemoteBrowserError,
    RemoteBrowserSession,
    bridge_remote_browser_to_websocket,
    browser_websocket_url,
    clamp_viewport,
    configured_cdp_url,
    encode_frame,
    key_event_params,
    mouse_event_params,
    navigable_url,
    resolve_browser_websocket_url,
    validate_cdp_url,
)

_PAGE = {"targetId": "page-1", "type": "page", "title": "Home", "url": "https://example.com/"}
_HISTORY = {
    "currentIndex": 1,
    "entries": [
        {"id": 7, "url": "https://example.com/start", "title": "Start"},
        {"id": 8, "url": "https://example.com/", "title": "Home"},
    ],
}


class _FakeChrome:
    """A DevTools transport that answers commands and lets a test emit events."""

    def __init__(self, targets: list[dict[str, Any]] | None = None) -> None:
        self.targets = [_PAGE] if targets is None else targets
        self.commands: list[dict[str, Any]] = []
        self.refused: set[str] = set()
        self._outbox: asyncio.Queue[str] = asyncio.Queue()

    def _result(self, method: str) -> dict[str, Any]:
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == "Target.createTarget":
            return {"targetId": "created-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "main-frame"}}}
        if method == "Page.getNavigationHistory":
            return _HISTORY
        return {}

    async def send(self, message: str) -> None:
        command = json.loads(message)
        self.commands.append(command)
        reply: dict[str, Any] = {"id": command["id"]}
        if command["method"] in self.refused:
            reply["error"] = {"message": "Not attached to an active page"}
        else:
            reply["result"] = self._result(command["method"])
        self._outbox.put_nowait(json.dumps(reply))

    async def recv(self) -> str:
        return await self._outbox.get()

    async def close(self) -> None:
        return None

    def emit(self, method: str, params: dict[str, Any], *, in_page: bool = True) -> None:
        event: dict[str, Any] = {"method": method, "params": params}
        if in_page:
            event["sessionId"] = "session-1"
        self._outbox.put_nowait(json.dumps(event))

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [c.get("params", {}) for c in self.commands if c["method"] == method]


class _FakeViewer:
    """A viewer WebSocket that records what the bridge sends."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.messages: list[dict[str, Any]] = []
        self.frames: list[bytes] = []
        self.closed: tuple[int, str | None] | None = None

    async def receive(self) -> dict[str, Any]:
        return await self._inbox.get()

    async def send_text(self, data: str) -> None:
        self.messages.append(json.loads(data))

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    def say(self, **message: Any) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

    def disconnect(self) -> None:
        self._inbox.put_nowait({"type": "websocket.disconnect"})

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [message for message in self.messages if message["type"] == kind]


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _start(
    chrome: _FakeChrome, *, read_only: bool = False
) -> tuple[_FakeViewer, asyncio.Task[None]]:
    viewer = _FakeViewer()
    session = RemoteBrowserSession(viewer, CdpConnection(chrome), read_only=read_only)
    task = asyncio.create_task(session.run())
    await _settle()
    return viewer, task


async def _finish(viewer: _FakeViewer, task: asyncio.Task[None]) -> None:
    viewer.disconnect()
    await asyncio.wait_for(task, timeout=2)


def _screencast_frame(jpeg: bytes = b"jpeg") -> dict[str, Any]:
    return {
        "data": base64.b64encode(jpeg).decode(),
        "sessionId": 3,
        "metadata": {"deviceWidth": 1280, "deviceHeight": 800},
    }


def _decode_frame(frame: bytes) -> tuple[dict[str, Any], bytes]:
    header_length = int.from_bytes(frame[:4], "big")
    return json.loads(frame[4 : 4 + header_length]), frame[4 + header_length :]


def test_configured_cdp_url_ignores_blank_values() -> None:
    assert configured_cdp_url({}) is None
    assert configured_cdp_url({"OMNIGENT_REMOTE_BROWSER_CDP_URL": "  "}) is None
    assert (
        configured_cdp_url({"OMNIGENT_REMOTE_BROWSER_CDP_URL": " http://127.0.0.1:9222 "})
        == "http://127.0.0.1:9222"
    )


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:9222", "http://localhost:9222/", "ws://[::1]:9222/devtools/browser/abc"],
)
def test_validate_cdp_url_accepts_loopback(url: str) -> None:
    assert validate_cdp_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.10:9222",
        "http://browser.example.com:9222",
        "https://127.0.0.1:9222",
        "http://user:secret@127.0.0.1:9222",
        "file:///tmp/socket",
    ],
)
def test_validate_cdp_url_rejects_everything_else(url: str) -> None:
    with pytest.raises(RemoteBrowserError) as rejected:
        validate_cdp_url(url)
    assert rejected.value.close_code == WS_CLOSE_REMOTE_BROWSER_NOT_CONFIGURED


def test_browser_websocket_url_keeps_the_configured_address() -> None:
    assert (
        browser_websocket_url(
            "http://127.0.0.1:40123", "ws://127.0.0.1:9222/devtools/browser/abc-123"
        )
        == "ws://127.0.0.1:40123/devtools/browser/abc-123"
    )


async def test_resolve_browser_websocket_url_asks_chrome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:40123/json/version"
        return httpx.Response(
            200, json={"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await resolve_browser_websocket_url(
            "http://127.0.0.1:40123", http_client=client
        )

    assert resolved == "ws://127.0.0.1:40123/devtools/browser/abc"


async def test_resolve_browser_websocket_url_reports_an_unreachable_chrome() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        with pytest.raises(RemoteBrowserError) as unreachable:
            await resolve_browser_websocket_url("http://127.0.0.1:40123", http_client=client)

    assert unreachable.value.close_code == WS_CLOSE_REMOTE_BROWSER_UNREACHABLE


def test_encode_frame_round_trips() -> None:
    header, jpeg = _decode_frame(encode_frame(5, 1280, 800, b"\xff\xd8image"))
    assert header == {"seq": 5, "width": 1280, "height": 800}
    assert jpeg == b"\xff\xd8image"


def test_mouse_event_params_translates_each_event() -> None:
    assert mouse_event_params(
        {"event": "down", "x": 10, "y": 20.5, "button": "left", "buttons": 1, "click_count": 2}
    ) == {
        "type": "mousePressed",
        "x": 10.0,
        "y": 20.5,
        "button": "left",
        "buttons": 1,
        "modifiers": 0,
        "clickCount": 2,
    }
    wheel = mouse_event_params({"event": "wheel", "x": 1, "y": 2, "delta_y": 120, "modifiers": 8})
    assert wheel is not None
    assert (wheel["type"], wheel["deltaX"], wheel["deltaY"], wheel["modifiers"]) == (
        "mouseWheel",
        0.0,
        120.0,
        8,
    )


@pytest.mark.parametrize(
    "message",
    [
        {"event": "drag", "x": 1, "y": 2},
        {"event": "move", "x": "1", "y": 2},
        {"event": "move", "x": float("nan"), "y": 2},
        {"event": "move", "x": 1e9, "y": 2},
        {"event": "down", "x": 1, "y": 2, "button": "fourth"},
    ],
)
def test_mouse_event_params_rejects_malformed_messages(message: dict[str, Any]) -> None:
    assert mouse_event_params(message) is None


def test_key_event_params_only_inserts_text_on_a_press_that_carries_it() -> None:
    typed = key_event_params(
        {"event": "down", "key": "a", "code": "KeyA", "text": "a", "key_code": 65}
    )
    assert typed == {
        "type": "keyDown",
        "key": "a",
        "code": "KeyA",
        "windowsVirtualKeyCode": 65,
        "modifiers": 0,
        "text": "a",
    }
    arrow = key_event_params({"event": "down", "key": "ArrowDown", "code": "ArrowDown"})
    assert arrow is not None and arrow["type"] == "rawKeyDown" and "text" not in arrow
    released = key_event_params({"event": "up", "key": "a", "text": "a"})
    assert released is not None and released["type"] == "keyUp" and "text" not in released
    assert key_event_params({"event": "press", "key": "a"}) is None


def test_clamp_viewport_bounds_the_size() -> None:
    assert clamp_viewport(1280.4, 800) == (1280, 800)
    assert clamp_viewport(10, 99999) == (200, 2160)
    assert clamp_viewport("wide", 800) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("http://localhost:5173/", "http://localhost:5173/"),
        ("file:///etc/hosts", None),
        ("javascript:alert(1)", None),
        ("chrome://settings", None),
        ("example.com", None),
        (42, None),
    ],
)
def test_navigable_url_allows_only_web_addresses(value: object, expected: str | None) -> None:
    assert navigable_url(value) == expected


async def test_session_attaches_to_the_first_page_and_reports_its_state() -> None:
    chrome = _FakeChrome(
        targets=[{"targetId": "tools", "type": "page", "url": "devtools://devtools/x"}, _PAGE]
    )
    viewer, task = await _start(chrome)

    assert viewer.of_type("ready") == [{"type": "ready", "read_only": False}]
    assert chrome.sent("Target.attachToTarget") == [{"targetId": "page-1", "flatten": True}]
    assert chrome.sent("Page.startScreencast")[0]["format"] == "jpeg"
    state = viewer.of_type("state")[-1]
    assert (state["url"], state["title"]) == ("https://example.com/", "Home")
    assert (state["can_go_back"], state["can_go_forward"]) == (True, False)
    assert state["tabs"] == [{"id": "page-1", "title": "Home", "url": "https://example.com/"}]
    await _finish(viewer, task)


async def test_session_opens_a_blank_page_when_chrome_has_none() -> None:
    chrome = _FakeChrome(targets=[])
    viewer, task = await _start(chrome)

    assert chrome.sent("Target.createTarget") == [{"url": "about:blank"}]
    assert chrome.sent("Target.attachToTarget") == [{"targetId": "created-1", "flatten": True}]
    await _finish(viewer, task)


async def test_frames_wait_for_acknowledgement_and_the_newest_replaces_the_backlog() -> None:
    chrome = _FakeChrome()
    viewer, task = await _start(chrome)

    for jpeg in (b"one", b"two", b"three", b"four"):
        chrome.emit("Page.screencastFrame", _screencast_frame(jpeg))
    await _settle()

    # Chrome is acknowledged for every frame; the viewer holds two unacknowledged.
    assert len(chrome.sent("Page.screencastFrameAck")) == 4
    assert [_decode_frame(frame)[1] for frame in viewer.frames] == [b"one", b"two"]

    viewer.say(type="frame_ack", seq=1)
    await _settle()
    header, jpeg = _decode_frame(viewer.frames[-1])
    assert (header["seq"], header["width"], header["height"], jpeg) == (4, 1280, 800, b"four")
    assert len(viewer.frames) == 3
    await _finish(viewer, task)


async def test_viewer_input_and_navigation_reach_chrome() -> None:
    chrome = _FakeChrome()
    viewer, task = await _start(chrome)

    viewer.say(type="resize", width=900, height=600)
    viewer.say(type="mouse", event="move", x=5, y=6)
    viewer.say(type="key", event="down", key="Enter", code="Enter", key_code=13)
    viewer.say(type="text", text="pasted")
    viewer.say(type="navigate", url="https://example.org/docs")
    viewer.say(type="back")
    viewer.say(type="reload")
    await _settle()

    assert chrome.sent("Emulation.setDeviceMetricsOverride") == [
        {"width": 900, "height": 600, "deviceScaleFactor": 0, "mobile": False}
    ]
    assert chrome.sent("Input.dispatchMouseEvent")[0]["type"] == "mouseMoved"
    assert chrome.sent("Input.dispatchKeyEvent")[0]["type"] == "rawKeyDown"
    assert chrome.sent("Input.insertText") == [{"text": "pasted"}]
    assert chrome.sent("Page.navigate") == [{"url": "https://example.org/docs"}]
    assert chrome.sent("Page.navigateToHistoryEntry") == [{"entryId": 7}]
    assert len(chrome.sent("Page.reload")) == 1
    await _finish(viewer, task)


async def test_navigation_to_a_non_web_address_is_refused_with_a_notice() -> None:
    chrome = _FakeChrome()
    viewer, task = await _start(chrome)

    viewer.say(type="navigate", url="file:///etc/hosts")
    await _settle()

    assert chrome.sent("Page.navigate") == []
    assert viewer.of_type("notice") == [
        {"type": "notice", "message": "Only http and https addresses can be opened."}
    ]
    await _finish(viewer, task)


async def test_a_command_chrome_refuses_is_reported_without_ending_the_session() -> None:
    chrome = _FakeChrome()
    viewer, task = await _start(chrome)

    chrome.refused = {"Page.navigate", "Page.getNavigationHistory"}
    viewer.say(type="navigate", url="https://example.org/")
    chrome.emit("Page.frameNavigated", {"frame": {"id": "main-frame"}})
    await _settle()

    assert not task.done()
    assert viewer.of_type("notice") == [
        {"type": "notice", "message": "The browser didn't accept that. Try again."}
    ]
    chrome.refused = set()
    viewer.say(type="reload")
    await _settle()
    assert len(chrome.sent("Page.reload")) == 1
    await _finish(viewer, task)


async def test_the_page_size_is_applied_again_after_a_navigation() -> None:
    chrome = _FakeChrome()
    viewer, task = await _start(chrome)

    viewer.say(type="resize", width=900, height=600)
    await _settle()
    chrome.emit("Page.frameNavigated", {"frame": {"id": "main-frame"}})
    await _settle()

    assert len(chrome.sent("Emulation.setDeviceMetricsOverride")) == 2
    await _finish(viewer, task)


async def test_read_only_viewer_gets_frames_but_cannot_drive() -> None:
    chrome = _FakeChrome()
    viewer, task = await _start(chrome, read_only=True)

    viewer.say(type="mouse", event="down", x=5, y=6, button="left")
    viewer.say(type="navigate", url="https://example.org/")
    viewer.say(type="resize", width=900, height=600)
    chrome.emit("Page.screencastFrame", _screencast_frame())
    await _settle()

    assert viewer.of_type("ready") == [{"type": "ready", "read_only": True}]
    assert len(viewer.frames) == 1
    for method in (
        "Input.dispatchMouseEvent",
        "Page.navigate",
        "Emulation.setDeviceMetricsOverride",
    ):
        assert chrome.sent(method) == []
    await _finish(viewer, task)


async def test_selecting_another_tab_moves_the_stream_to_it() -> None:
    other = {"targetId": "page-2", "type": "page", "title": "Docs", "url": "https://example.org/"}
    chrome = _FakeChrome(targets=[_PAGE, other])
    viewer, task = await _start(chrome)

    viewer.say(type="select_tab", id="page-2")
    viewer.say(type="select_tab", id="unknown")
    await _settle()

    assert [call["targetId"] for call in chrome.sent("Target.attachToTarget")] == [
        "page-1",
        "page-2",
    ]
    assert chrome.sent("Target.activateTarget")[-1] == {"targetId": "page-2"}
    assert viewer.of_type("state")[-1]["tab_id"] == "page-2"
    await _finish(viewer, task)


async def test_confirm_dialogs_are_dismissed_and_reported() -> None:
    chrome = _FakeChrome()
    viewer, task = await _start(chrome)

    chrome.emit("Page.javascriptDialogOpening", {"type": "confirm", "message": "Delete?"})
    chrome.emit("Page.javascriptDialogOpening", {"type": "alert", "message": "Saved"})
    await _settle()

    assert chrome.sent("Page.handleJavaScriptDialog") == [{"accept": False}, {"accept": True}]
    assert [notice["message"] for notice in viewer.of_type("notice")] == [
        "The page opened a dialog (confirm), which was dismissed.",
        "The page opened a dialog (alert), which was accepted.",
    ]
    await _finish(viewer, task)


async def test_closing_the_watched_page_ends_the_session() -> None:
    chrome = _FakeChrome()
    _viewer, task = await _start(chrome)

    chrome.emit("Target.targetDestroyed", {"targetId": "page-1"}, in_page=False)

    with pytest.raises(RemoteBrowserError) as gone:
        await asyncio.wait_for(task, timeout=2)
    assert gone.value.close_code == WS_CLOSE_REMOTE_BROWSER_PAGE_GONE


async def test_bridge_closes_the_viewer_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_REMOTE_BROWSER_CDP_URL", raising=False)
    viewer = _FakeViewer()

    await bridge_remote_browser_to_websocket(viewer, read_only=False)

    assert viewer.closed == (
        WS_CLOSE_REMOTE_BROWSER_NOT_CONFIGURED,
        "no remote browser is configured on this host",
    )
