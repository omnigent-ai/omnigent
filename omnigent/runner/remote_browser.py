"""Live view of a Chrome reachable from the runner over the DevTools protocol.

The runner's operator points ``OMNIGENT_REMOTE_BROWSER_CDP_URL`` at a Chrome
started with ``--remote-debugging-port`` on the runner's own machine. A viewer
attaches over a WebSocket and gets that Chrome's page as a stream of JPEG
frames, and sends back pointer, keyboard and navigation messages.

Wire protocol, viewer side:

- Binary, runner to viewer: one frame, see :func:`encode_frame`.
- Text JSON, runner to viewer: ``ready``, ``state``, ``notice``.
- Text JSON, viewer to runner: ``frame_ack``, ``resize``, ``mouse``, ``key``,
  ``text``, ``navigate``, ``back``, ``forward``, ``reload``, ``select_tab``.

A read-only viewer gets frames and state; everything it sends except
``frame_ack`` is dropped.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Query, WebSocket

_logger = logging.getLogger(__name__)

REMOTE_BROWSER_CDP_URL_ENV: Final[str] = "OMNIGENT_REMOTE_BROWSER_CDP_URL"

# Application close codes; the web pane retries everything except "not configured".
WS_CLOSE_REMOTE_BROWSER_NOT_CONFIGURED: Final[int] = 4404
WS_CLOSE_REMOTE_BROWSER_PAGE_GONE: Final[int] = 4405
WS_CLOSE_REMOTE_BROWSER_UNREACHABLE: Final[int] = 4502

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
_DISCOVERY_TIMEOUT_S: Final[float] = 5.0
_COMMAND_TIMEOUT_S: Final[float] = 10.0
# Chrome sends screencast frames base64-encoded inside one JSON message.
_CDP_MAX_MESSAGE_BYTES: Final[int] = 64 * 1024 * 1024

_JPEG_QUALITY: Final[int] = 70
# Frames a viewer may hold unacknowledged before newer ones replace the waiting one.
_MAX_FRAMES_IN_FLIGHT: Final[int] = 2
_FRAME_ACK_TIMEOUT_S: Final[float] = 2.0

_MIN_VIEWPORT: Final[int] = 200
_MAX_VIEWPORT_WIDTH: Final[int] = 3840
_MAX_VIEWPORT_HEIGHT: Final[int] = 2160
_MAX_COORDINATE: Final[float] = 100_000.0
_MAX_KEY_FIELD_CHARS: Final[int] = 64
_MAX_INSERT_TEXT_CHARS: Final[int] = 64 * 1024
_MAX_URL_CHARS: Final[int] = 8192

_MOUSE_EVENT_TYPES: Final[Mapping[str, str]] = {
    "move": "mouseMoved",
    "down": "mousePressed",
    "up": "mouseReleased",
    "wheel": "mouseWheel",
}
_MOUSE_BUTTONS: Final[frozenset[str]] = frozenset({"none", "left", "middle", "right"})
# Pages a viewer never follows: Chrome's own UI and extension pages.
_HIDDEN_TAB_URL_PREFIXES: Final[tuple[str, ...]] = ("devtools://", "chrome-extension://")
_NAVIGABLE_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


class RemoteBrowserError(Exception):
    """A remote browser problem that should close the viewer with *close_code*."""

    def __init__(self, message: str, *, close_code: int) -> None:
        super().__init__(message)
        self.close_code = close_code


class CdpCommandError(Exception):
    """Chrome refused one command; the connection itself is still usable."""


class ViewerSocket(Protocol):
    """The part of a FastAPI ``WebSocket`` the bridge uses."""

    async def receive(self) -> Mapping[str, Any]: ...

    async def send_text(self, data: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class CdpTransport(Protocol):
    """A connected DevTools WebSocket."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


def configured_cdp_url(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the operator's DevTools endpoint, or ``None`` when unset or blank."""
    value = (environ if environ is not None else os.environ).get(REMOTE_BROWSER_CDP_URL_ENV, "")
    return value.strip() or None


def validate_cdp_url(raw_url: str) -> str:
    """
    Check that *raw_url* names a DevTools endpoint on this machine.

    The DevTools protocol has no authentication, so only loopback is accepted.

    :param raw_url: ``http://127.0.0.1:9222`` or a ``ws://`` browser endpoint.
    :returns: The URL, trimmed.
    :raises RemoteBrowserError: If the scheme or host is not allowed.
    """
    url = raw_url.strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "ws"} or parts.username or parts.password:
        raise RemoteBrowserError(
            "remote browser endpoint must be an http:// or ws:// URL",
            close_code=WS_CLOSE_REMOTE_BROWSER_NOT_CONFIGURED,
        )
    if (parts.hostname or "").lower() not in _LOOPBACK_HOSTS:
        raise RemoteBrowserError(
            "remote browser endpoint must be on localhost",
            close_code=WS_CLOSE_REMOTE_BROWSER_NOT_CONFIGURED,
        )
    return url


def browser_websocket_url(cdp_url: str, advertised_url: str) -> str:
    """
    Combine the path Chrome advertises with the address the runner reaches it at.

    Chrome reports its own listening address, which differs from the
    configured one when the port is forwarded.
    """
    configured = urlsplit(cdp_url)
    advertised = urlsplit(advertised_url)
    return urlunsplit(("ws", configured.netloc, advertised.path, advertised.query, ""))


async def resolve_browser_websocket_url(
    cdp_url: str, *, http_client: httpx.AsyncClient | None = None
) -> str:
    """
    Resolve the configured endpoint to Chrome's browser-level WebSocket URL.

    :param cdp_url: A URL accepted by :func:`validate_cdp_url`.
    :param http_client: Client for the discovery request; tests inject one.
    :returns: A ``ws://`` URL on the configured host and port.
    :raises RemoteBrowserError: If Chrome does not answer the discovery request.
    """
    if urlsplit(cdp_url).scheme == "ws":
        return cdp_url
    discovery_url = cdp_url.rstrip("/") + "/json/version"
    try:
        if http_client is None:
            # trust_env=False: a proxy must never see a loopback DevTools request.
            async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_S, trust_env=False) as client:
                response = await client.get(discovery_url)
        else:
            response = await http_client.get(discovery_url)
        response.raise_for_status()
        advertised_url = response.json()["webSocketDebuggerUrl"]
        if not isinstance(advertised_url, str):
            raise TypeError("webSocketDebuggerUrl is not a string")
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise RemoteBrowserError(
            "remote browser is not reachable",
            close_code=WS_CLOSE_REMOTE_BROWSER_UNREACHABLE,
        ) from exc
    return browser_websocket_url(cdp_url, advertised_url)


def encode_frame(sequence: int, width: int, height: int, jpeg: bytes) -> bytes:
    """
    Pack one frame: a 4-byte big-endian header length, a JSON header, the JPEG.

    :param sequence: The number the viewer echoes in its ``frame_ack``.
    :param width: Page width in CSS pixels, which pointer coordinates use.
    :param height: Page height in CSS pixels.
    :param jpeg: The encoded image.
    """
    header = json.dumps(
        {"seq": sequence, "width": width, "height": height}, separators=(",", ":")
    ).encode()
    return len(header).to_bytes(4, "big") + header + jpeg


def _number(value: object, *, limit: float = _MAX_COORDINATE) -> float | None:
    """Return *value* as a finite number within ``±limit``, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value != value or abs(value) > limit:
        return None
    return float(value)


def _small_int(value: object, *, maximum: int) -> int:
    """Return *value* as an integer in ``0..maximum``, or 0 when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= maximum else 0


def _short_text(value: object) -> str:
    """Return *value* when it is a short string, else an empty string."""
    return value if isinstance(value, str) and len(value) <= _MAX_KEY_FIELD_CHARS else ""


def mouse_event_params(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """
    Translate a viewer ``mouse`` message to ``Input.dispatchMouseEvent`` params.

    :returns: The params, or ``None`` when the message is malformed.
    """
    event_type = _MOUSE_EVENT_TYPES.get(str(message.get("event")))
    x = _number(message.get("x"))
    y = _number(message.get("y"))
    button = message.get("button", "none")
    if event_type is None or x is None or y is None or button not in _MOUSE_BUTTONS:
        return None
    params: dict[str, Any] = {
        "type": event_type,
        "x": x,
        "y": y,
        "button": button,
        "buttons": _small_int(message.get("buttons"), maximum=31),
        "modifiers": _small_int(message.get("modifiers"), maximum=15),
    }
    if event_type in {"mousePressed", "mouseReleased"}:
        params["clickCount"] = _small_int(message.get("click_count"), maximum=3) or 1
    if event_type == "mouseWheel":
        params["deltaX"] = _number(message.get("delta_x")) or 0.0
        params["deltaY"] = _number(message.get("delta_y")) or 0.0
    return params


def key_event_params(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """
    Translate a viewer ``key`` message to ``Input.dispatchKeyEvent`` params.

    :returns: The params, or ``None`` when the message is malformed.
    """
    event = message.get("event")
    if event not in {"down", "up"}:
        return None
    text = _short_text(message.get("text"))
    if event == "up":
        event_type = "keyUp"
    else:
        # Only a key press that carries text inserts a character.
        event_type = "keyDown" if text else "rawKeyDown"
    params: dict[str, Any] = {
        "type": event_type,
        "key": _short_text(message.get("key")),
        "code": _short_text(message.get("code")),
        "windowsVirtualKeyCode": _small_int(message.get("key_code"), maximum=255),
        "modifiers": _small_int(message.get("modifiers"), maximum=15),
    }
    if event_type == "keyDown":
        params["text"] = text
    return params


def clamp_viewport(width: object, height: object) -> tuple[int, int] | None:
    """Return a viewport size within Chrome-friendly bounds, or ``None`` if malformed."""
    w = _number(width)
    h = _number(height)
    if w is None or h is None:
        return None
    return (
        max(_MIN_VIEWPORT, min(_MAX_VIEWPORT_WIDTH, round(w))),
        max(_MIN_VIEWPORT, min(_MAX_VIEWPORT_HEIGHT, round(h))),
    )


def navigable_url(value: object) -> str | None:
    """Return *value* when a viewer may navigate to it: an http(s) URL."""
    if not isinstance(value, str) or len(value) > _MAX_URL_CHARS:
        return None
    parts = urlsplit(value.strip())
    if parts.scheme not in _NAVIGABLE_SCHEMES or not parts.netloc:
        return None
    return value.strip()


def is_viewable_tab(target: Mapping[str, Any]) -> bool:
    """Whether a DevTools target is a page a viewer can follow."""
    url = str(target.get("url", ""))
    return target.get("type") == "page" and not url.startswith(_HIDDEN_TAB_URL_PREFIXES)


class CdpConnection:
    """One DevTools connection: numbered commands, and events on a queue."""

    def __init__(self, transport: CdpTransport) -> None:
        self._transport = transport
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                message = json.loads(await self._transport.recv())
                message_id = message.get("id")
                if message_id is None:
                    self.events.put_nowait(message)
                    continue
                future = self._pending.pop(message_id, None)
                if future is None or future.done():
                    continue
                if "error" in message:
                    future.set_exception(CdpCommandError(str(message["error"].get("message"))))
                else:
                    future.set_result(message.get("result", {}))
        except Exception:  # noqa: BLE001 - any transport failure ends the connection
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("DevTools connection closed"))
            self._pending.clear()
            # Wake the event consumer so it notices the connection is gone.
            self.events.put_nowait(None)

    async def _write(
        self,
        message_id: int,
        method: str,
        params: Mapping[str, Any] | None,
        session_id: str | None,
    ) -> None:
        message: dict[str, Any] = {"id": message_id, "method": method}
        if params:
            message["params"] = dict(params)
        if session_id:
            message["sessionId"] = session_id
        await self._transport.send(json.dumps(message, separators=(",", ":")))

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def command(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a command and wait for its result."""
        message_id = self._allocate_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        # Registered before writing so a fast reply always finds its future.
        self._pending[message_id] = future
        try:
            await self._write(message_id, method, params, session_id)
            return await asyncio.wait_for(future, timeout=_COMMAND_TIMEOUT_S)
        finally:
            self._pending.pop(message_id, None)

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        """Send a command without waiting; its reply is discarded."""
        await self._write(self._allocate_id(), method, params, session_id)

    async def close(self) -> None:
        """Close the transport and stop reading."""
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        with contextlib.suppress(Exception):
            await self._transport.close()


@dataclass
class _ViewerFrames:
    """Paces frames to one viewer so a slow link sees the current page, not a backlog."""

    in_flight: dict[int, float] = field(default_factory=dict)
    waiting: bytes | None = None
    waiting_sequence: int = 0
    sequence: int = 0

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def can_send(self, now: float) -> bool:
        # An ack that never arrives must not stall the stream.
        self.in_flight = {
            seq: sent for seq, sent in self.in_flight.items() if now - sent < _FRAME_ACK_TIMEOUT_S
        }
        return len(self.in_flight) < _MAX_FRAMES_IN_FLIGHT


class RemoteBrowserSession:
    """Bridges one viewer WebSocket to one page of the remote Chrome."""

    def __init__(self, viewer: ViewerSocket, cdp: CdpConnection, *, read_only: bool) -> None:
        self._viewer = viewer
        self._cdp = cdp
        self._read_only = read_only
        self._frames = _ViewerFrames()
        self._send_lock = asyncio.Lock()
        self._tabs: dict[str, dict[str, Any]] = {}
        self._target_id: str | None = None
        self._session_id: str | None = None
        self._main_frame_id: str | None = None
        self._loading = False
        self._viewport: tuple[int, int] | None = None

    async def run(self) -> None:
        """Serve the viewer until either side goes away."""
        await self._cdp.command("Target.setDiscoverTargets", {"discover": True})
        targets = (await self._cdp.command("Target.getTargets")).get("targetInfos", [])
        for target in targets:
            self._note_tab(target)
        first = next(iter(self._tabs), None)
        if first is None:
            created = await self._cdp.command("Target.createTarget", {"url": "about:blank"})
            first = str(created["targetId"])
        await self._send_json({"type": "ready", "read_only": self._read_only})
        await self._attach(first)

        events = asyncio.create_task(self._pump_events())
        inputs = asyncio.create_task(self._pump_viewer())
        try:
            done, _pending = await asyncio.wait(
                {events, inputs}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            for task in (events, inputs):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    def _note_tab(self, target: Mapping[str, Any]) -> None:
        target_id = str(target.get("targetId", ""))
        if not target_id:
            return
        if is_viewable_tab(target):
            self._tabs[target_id] = {
                "id": target_id,
                "title": str(target.get("title", "")),
                "url": str(target.get("url", "")),
            }
        else:
            self._tabs.pop(target_id, None)

    async def _attach(self, target_id: str) -> None:
        if self._session_id is not None:
            with contextlib.suppress(Exception):
                await self._cdp.command("Target.detachFromTarget", {"sessionId": self._session_id})
        attached = await self._cdp.command(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self._target_id = target_id
        self._session_id = str(attached["sessionId"])
        self._loading = False
        await self._page("Page.enable")
        frame_tree = await self._page("Page.getFrameTree")
        self._main_frame_id = frame_tree.get("frameTree", {}).get("frame", {}).get("id")
        # Chrome only paints, and so only screencasts, the tab in front.
        with contextlib.suppress(Exception):
            await self._cdp.command("Target.activateTarget", {"targetId": target_id})
        if self._viewport is not None:
            await self._apply_viewport()
        await self._page(
            "Page.startScreencast",
            {"format": "jpeg", "quality": _JPEG_QUALITY, "everyNthFrame": 1},
        )
        await self._send_state()

    async def _page(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self._cdp.command(method, params, session_id=self._session_id)

    async def _page_notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._cdp.notify(method, params, session_id=self._session_id)

    async def _apply_viewport(self) -> None:
        if self._viewport is None:
            return
        width, height = self._viewport
        await self._page(
            "Emulation.setDeviceMetricsOverride",
            # deviceScaleFactor 0 keeps Chrome's own scale.
            {"width": width, "height": height, "deviceScaleFactor": 0, "mobile": False},
        )

    async def _send_json(self, payload: Mapping[str, Any]) -> None:
        async with self._send_lock:
            await self._viewer.send_text(json.dumps(payload, separators=(",", ":")))

    async def _send_state(self) -> None:
        try:
            history = await self._page("Page.getNavigationHistory")
        except CdpCommandError:
            # The page is between documents; the navigation's next event sends state.
            return
        entries = history.get("entries", [])
        index = history.get("currentIndex", 0)
        current = entries[index] if 0 <= index < len(entries) else {}
        await self._send_json(
            {
                "type": "state",
                "url": current.get("url", ""),
                "title": current.get("title", ""),
                "can_go_back": index > 0,
                "can_go_forward": index < len(entries) - 1,
                "loading": self._loading,
                "tab_id": self._target_id,
                "tabs": list(self._tabs.values()),
            }
        )

    async def _pump_events(self) -> None:
        while True:
            event = await self._cdp.events.get()
            if event is None:
                raise RemoteBrowserError(
                    "remote browser disconnected",
                    close_code=WS_CLOSE_REMOTE_BROWSER_UNREACHABLE,
                )
            try:
                await self._handle_event(event)
            except CdpCommandError as exc:
                _logger.debug("remote browser event %s failed: %s", event.get("method"), exc)

    async def _handle_event(self, event: Mapping[str, Any]) -> None:
        method = event.get("method")
        params = event.get("params", {})
        if method in {"Target.targetCreated", "Target.targetInfoChanged"}:
            self._note_tab(params.get("targetInfo", {}))
            await self._send_state()
            return
        if method == "Target.targetDestroyed":
            self._tabs.pop(str(params.get("targetId", "")), None)
            if params.get("targetId") == self._target_id:
                raise RemoteBrowserError(
                    "the page was closed", close_code=WS_CLOSE_REMOTE_BROWSER_PAGE_GONE
                )
            await self._send_state()
            return
        if event.get("sessionId") != self._session_id:
            return
        if method == "Page.screencastFrame":
            await self._handle_frame(params)
        elif method in {"Page.frameStartedLoading", "Page.frameStoppedLoading"}:
            if params.get("frameId") == self._main_frame_id:
                self._loading = method == "Page.frameStartedLoading"
                await self._send_state()
        elif method == "Page.frameNavigated":
            if params.get("frame", {}).get("id") == self._main_frame_id:
                # A new renderer process starts without the emulated size.
                await self._apply_viewport()
                await self._send_state()
        elif method == "Page.navigatedWithinDocument":
            await self._send_state()
        elif method == "Page.javascriptDialogOpening":
            await self._handle_dialog(params)

    async def _handle_frame(self, params: Mapping[str, Any]) -> None:
        # Acknowledge at once; the viewer, not Chrome, is what gets paced.
        await self._page_notify("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})
        metadata = params.get("metadata", {})
        sequence = self._frames.next_sequence()
        frame = encode_frame(
            sequence,
            round(metadata.get("deviceWidth", 0)),
            round(metadata.get("deviceHeight", 0)),
            base64.b64decode(params.get("data", "")),
        )
        if self._frames.can_send(time.monotonic()):
            await self._send_frame(sequence, frame)
        else:
            self._frames.waiting = frame
            self._frames.waiting_sequence = sequence

    async def _send_frame(self, sequence: int, frame: bytes) -> None:
        self._frames.in_flight[sequence] = time.monotonic()
        async with self._send_lock:
            await self._viewer.send_bytes(frame)

    async def _handle_dialog(self, params: Mapping[str, Any]) -> None:
        # A screencast cannot show a dialog, and an open one blocks the page.
        kind = params.get("type")
        accept = kind in {"alert", "beforeunload"}
        await self._page("Page.handleJavaScriptDialog", {"accept": accept})
        await self._send_json(
            {
                "type": "notice",
                "message": (
                    f"The page opened a dialog ({kind}), which was "
                    f"{'accepted' if accept else 'dismissed'}."
                ),
            }
        )

    async def _pump_viewer(self) -> None:
        while True:
            message = await self._viewer.receive()
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if isinstance(payload, dict):
                await self._handle_viewer_message(payload)

    async def _handle_viewer_message(self, payload: Mapping[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "frame_ack":
            self._frames.in_flight.pop(_small_int(payload.get("seq"), maximum=2**53), None)
            if self._frames.waiting is not None and self._frames.can_send(time.monotonic()):
                frame, sequence = self._frames.waiting, self._frames.waiting_sequence
                self._frames.waiting = None
                await self._send_frame(sequence, frame)
            return
        if self._read_only:
            return
        try:
            await self._handle_control(kind, payload)
        except CdpCommandError:
            # Chrome refuses commands while a page is being replaced; the viewer can retry.
            await self._send_json(
                {"type": "notice", "message": "The browser didn't accept that. Try again."}
            )

    async def _handle_control(self, kind: object, payload: Mapping[str, Any]) -> None:
        if kind == "mouse":
            params = mouse_event_params(payload)
            if params is not None:
                await self._page_notify("Input.dispatchMouseEvent", params)
        elif kind == "key":
            params = key_event_params(payload)
            if params is not None:
                await self._page_notify("Input.dispatchKeyEvent", params)
        elif kind == "text":
            text = payload.get("text")
            if isinstance(text, str) and 0 < len(text) <= _MAX_INSERT_TEXT_CHARS:
                await self._page_notify("Input.insertText", {"text": text})
        elif kind == "resize":
            viewport = clamp_viewport(payload.get("width"), payload.get("height"))
            if viewport is not None and viewport != self._viewport:
                self._viewport = viewport
                await self._apply_viewport()
        elif kind == "navigate":
            url = navigable_url(payload.get("url"))
            if url is None:
                await self._send_json(
                    {"type": "notice", "message": "Only http and https addresses can be opened."}
                )
            else:
                await self._page("Page.navigate", {"url": url})
        elif kind in {"back", "forward"}:
            await self._step_history(-1 if kind == "back" else 1)
        elif kind == "reload":
            await self._page("Page.reload")
        elif kind == "select_tab":
            tab_id = payload.get("id")
            if isinstance(tab_id, str) and tab_id in self._tabs and tab_id != self._target_id:
                await self._attach(tab_id)

    async def _step_history(self, offset: int) -> None:
        history = await self._page("Page.getNavigationHistory")
        entries = history.get("entries", [])
        index = history.get("currentIndex", 0) + offset
        if 0 <= index < len(entries):
            await self._page("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})


async def _connect_cdp(websocket_url: str) -> CdpConnection:
    import websockets.asyncio.client

    try:
        transport = await websockets.asyncio.client.connect(
            websocket_url,
            max_size=_CDP_MAX_MESSAGE_BYTES,
            # Chrome answers no pings on this socket; the event stream shows liveness.
            ping_interval=None,
            open_timeout=_DISCOVERY_TIMEOUT_S,
        )
    except Exception as exc:
        raise RemoteBrowserError(
            "remote browser is not reachable",
            close_code=WS_CLOSE_REMOTE_BROWSER_UNREACHABLE,
        ) from exc
    return CdpConnection(transport)


async def bridge_remote_browser_to_websocket(
    websocket: ViewerSocket,
    *,
    read_only: bool,
    cdp_url: str | None = None,
) -> None:
    """
    Stream the configured Chrome to an accepted viewer WebSocket until it closes.

    :param websocket: The viewer's WebSocket; the caller has accepted it.
    :param read_only: Drop the viewer's input and navigation.
    :param cdp_url: The DevTools endpoint; defaults to the runner's environment.
    """
    endpoint = cdp_url if cdp_url is not None else configured_cdp_url()
    cdp: CdpConnection | None = None
    try:
        if endpoint is None:
            raise RemoteBrowserError(
                "no remote browser is configured on this host",
                close_code=WS_CLOSE_REMOTE_BROWSER_NOT_CONFIGURED,
            )
        websocket_url = await resolve_browser_websocket_url(validate_cdp_url(endpoint))
        cdp = await _connect_cdp(websocket_url)
        await RemoteBrowserSession(websocket, cdp, read_only=read_only).run()
    except RemoteBrowserError as exc:
        with contextlib.suppress(Exception):
            await websocket.close(code=exc.close_code, reason=str(exc))
    except Exception:
        _logger.exception("remote browser bridge failed")
        with contextlib.suppress(Exception):
            await websocket.close(
                code=WS_CLOSE_REMOTE_BROWSER_UNREACHABLE, reason="remote browser bridge failed"
            )
    finally:
        if cdp is not None:
            await cdp.close()


def register_remote_browser_routes(app: FastAPI) -> None:
    """Add the remote browser attach route to the runner app."""

    @app.websocket("/v1/sessions/{session_id}/resources/remote_browser/attach")
    async def remote_browser_attach_ws(
        websocket: WebSocket,
        session_id: str,
        read_only: bool = Query(default=False),
    ) -> None:
        # The server authorized the viewer for this session before relaying it here.
        _logger.debug("remote browser attach for session %s", session_id)
        await websocket.accept()
        await bridge_remote_browser_to_websocket(websocket, read_only=read_only)
