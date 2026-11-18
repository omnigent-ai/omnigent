"""HTTP/2 over one connected socket, as a pyqwest transport.

The host reaches its workspace helper over one end of a socket pair, which
pyqwest's own transport can't use: it only connects to addresses. This speaks
HTTP/2 with prior knowledge on that socket with ``h2``, so the generated
Connect client (gRPC protocol) runs over it unchanged. Calls share the one
connection as concurrent streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlparse

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
from pyqwest import Headers, HTTPVersion, Request, Response, Transport

_logger = logging.getLogger(__name__)

_READ_BYTES = 64 * 1024


class HelperConnectionLost(ConnectionError):
    """The connection to the helper closed, or broke, while calls were open."""


@dataclass
class _Stream:
    """One call's response, as the connection's reader receives it."""

    headers: asyncio.Future[list[tuple[str, str]]]
    # DATA payloads with their flow-controlled length; None ends the body.
    body: asyncio.Queue[tuple[bytes, int] | BaseException | None] = field(
        default_factory=asyncio.Queue
    )
    trailers: Headers = field(default_factory=Headers)
    ended: bool = False


class _ResponseBody(AsyncIterator[bytes]):
    """A response's DATA frames; closing it early resets the stream."""

    def __init__(self, transport: SocketHTTP2Transport, stream_id: int, stream: _Stream) -> None:
        self._transport = transport
        self._stream_id = stream_id
        self._stream = stream

    def __aiter__(self) -> _ResponseBody:
        return self

    async def __anext__(self) -> bytes:
        if self._stream.ended:
            raise StopAsyncIteration
        item = await self._stream.body.get()
        if item is None:
            self._stream.ended = True
            self._transport._discard(self._stream_id)
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            self._stream.ended = True
            self._transport._discard(self._stream_id)
            raise item
        data, flow_length = item
        # Acknowledge as the caller consumes, so a slow reader slows the helper.
        self._transport._acknowledge(self._stream_id, flow_length)
        return data

    async def aclose(self) -> None:
        if not self._stream.ended:
            self._stream.ended = True
            self._transport._reset(self._stream_id)


class SocketHTTP2Transport(Transport):
    """A pyqwest transport speaking HTTP/2 on an already connected socket.

    :param sock: A connected stream socket, e.g. one end of
        :func:`socket.socketpair`. The transport owns it from here on.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._connection = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        )
        self._streams: dict[int, _Stream] = {}
        self._window_open = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._lost: HelperConnectionLost | None = None

    async def start(self) -> None:
        """Open the HTTP/2 connection and start reading from it."""
        reader, self._writer = await asyncio.open_unix_connection(sock=self._sock)
        self._connection.initiate_connection()
        self._flush()
        self._reader_task = asyncio.create_task(self._read(reader), name="workspace-helper-h2")

    @property
    def lost(self) -> bool:
        """Whether the connection closed or broke; no further call can succeed."""
        return self._lost is not None

    async def wait_closed(self) -> None:
        """Wait until the connection closes or breaks."""
        if self._reader_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self._reader_task)

    async def execute(self, request: Request) -> Response:
        if self._lost is not None:
            raise self._lost
        url = urlparse(request.url)
        stream_id = self._connection.get_next_available_stream_id()
        stream = _Stream(headers=asyncio.get_running_loop().create_future())
        self._streams[stream_id] = stream
        headers = [
            (":method", request.method),
            (":scheme", url.scheme or "http"),
            (":authority", url.netloc or "workspace-helper"),
            (":path", url.path or "/"),
            *((name.lower(), value) for name, value in request.headers.items()),
        ]
        try:
            self._connection.send_headers(stream_id, headers)
            self._flush()
            await self._send_body(stream_id, request.content)
            response_headers = await stream.headers
        except BaseException:
            self._reset(stream_id)
            raise
        status = int(next(value for name, value in response_headers if name == ":status"))
        return Response(
            status=status,
            http_version=HTTPVersion.HTTP2,
            headers=Headers((name, value) for name, value in response_headers if name[0] != ":"),
            content=_ResponseBody(self, stream_id, stream),
            trailers=stream.trailers,
        )

    async def close(self) -> None:
        """Close the connection; open calls fail with :class:`HelperConnectionLost`."""
        self.abort()
        if self._reader_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        if self._writer is not None:
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

    def abort(self) -> None:
        """Close the connection at once, without waiting; safe to repeat."""
        self._fail_all(HelperConnectionLost("The workspace helper connection closed"))
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._writer is not None:
            self._writer.close()
        else:
            self._sock.close()

    async def _send_body(
        self, stream_id: int, content: bytes | AsyncIterator[bytes] | None
    ) -> None:
        if content is None or isinstance(content, bytes):
            await self._send_data(stream_id, content or b"")
        else:
            async for chunk in content:
                await self._send_data(stream_id, chunk, end=False)
            await self._send_data(stream_id, b"")
        # The last call above ended the stream.

    async def _send_data(self, stream_id: int, data: bytes, *, end: bool = True) -> None:
        view = memoryview(data)
        while view:
            if self._lost is not None:
                raise self._lost
            window = min(
                self._connection.local_flow_control_window(stream_id),
                self._connection.max_outbound_frame_size,
            )
            if window <= 0:
                self._window_open.clear()
                await self._window_open.wait()
                continue
            self._connection.send_data(stream_id, bytes(view[:window]))
            view = view[window:]
            self._flush()
        if end:
            self._connection.end_stream(stream_id)
            self._flush()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        error: HelperConnectionLost = HelperConnectionLost(
            "The workspace helper closed the connection"
        )
        try:
            while data := await reader.read(_READ_BYTES):
                for event in self._connection.receive_data(data):
                    self._handle(event)
                self._flush()
        except (OSError, h2.exceptions.ProtocolError) as exc:
            error = HelperConnectionLost(f"The workspace helper connection broke: {exc}")
        finally:
            self._fail_all(error)

    def _handle(self, event: h2.events.Event) -> None:
        match event:
            case h2.events.ResponseReceived(stream_id=stream_id, headers=headers):
                stream = self._streams.get(stream_id)
                if stream is not None and not stream.headers.done():
                    # The configuration decodes headers to str.
                    stream.headers.set_result(cast("list[tuple[str, str]]", list(headers or [])))
            case h2.events.DataReceived(
                stream_id=stream_id, data=data, flow_controlled_length=length
            ):
                stream = self._streams.get(stream_id)
                if stream is None:
                    self._connection.acknowledge_received_data(length or 0, stream_id)
                else:
                    stream.body.put_nowait((data or b"", length or 0))
            case h2.events.TrailersReceived(stream_id=stream_id, headers=headers):
                stream = self._streams.get(stream_id)
                if stream is not None:
                    for name, value in cast("list[tuple[str, str]]", headers or []):
                        stream.trailers.add(name, value)
            case h2.events.StreamEnded(stream_id=stream_id):
                stream = self._streams.get(stream_id)
                if stream is not None:
                    stream.body.put_nowait(None)
            case h2.events.StreamReset(stream_id=stream_id, error_code=code):
                stream = self._streams.get(stream_id)
                if stream is not None:
                    self._discard(stream_id)
                    self._fail(stream, HelperConnectionLost(f"The helper reset the call ({code})"))
            case h2.events.WindowUpdated() | h2.events.RemoteSettingsChanged():
                self._window_open.set()
            case h2.events.ConnectionTerminated():
                self._fail_all(HelperConnectionLost("The workspace helper ended the connection"))

    def _fail(self, stream: _Stream, error: BaseException) -> None:
        if not stream.headers.done():
            stream.headers.set_exception(error)
        stream.body.put_nowait(error)

    def _fail_all(self, error: HelperConnectionLost) -> None:
        if self._lost is None:
            self._lost = error
        streams, self._streams = self._streams, {}
        for stream in streams.values():
            self._fail(stream, error)
        # Senders waiting for window space wake up and see the loss.
        self._window_open.set()

    def _discard(self, stream_id: int) -> None:
        """Forget a stream, returning the flow-control credit of DATA nobody will read."""
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            return
        unread = 0
        while not stream.body.empty():
            item = stream.body.get_nowait()
            if isinstance(item, tuple):
                unread += item[1]
        # The credit is the connection's too, so dropping it would starve every other call.
        self._acknowledge(stream_id, unread)

    def _acknowledge(self, stream_id: int, length: int) -> None:
        if self._lost is not None or not length:
            return
        with contextlib.suppress(h2.exceptions.StreamClosedError):
            self._connection.acknowledge_received_data(length, stream_id)
        self._flush()

    def _reset(self, stream_id: int) -> None:
        self._discard(stream_id)
        if self._lost is not None:
            return
        with contextlib.suppress(h2.exceptions.StreamClosedError, h2.exceptions.ProtocolError):
            self._connection.reset_stream(stream_id, h2.errors.ErrorCodes.CANCEL)
        self._flush()

    def _flush(self) -> None:
        data = self._connection.data_to_send()
        if not data or self._writer is None or self._lost is not None:
            return
        try:
            self._writer.write(data)
        except OSError as exc:
            self._fail_all(HelperConnectionLost(f"The workspace helper connection broke: {exc}"))
