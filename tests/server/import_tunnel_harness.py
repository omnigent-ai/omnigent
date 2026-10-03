"""In-process host <-> server harness for the local-session import tests.

Joins the real pieces an import crosses (the imports router, the host-tunnel
receive loop, ``HostRegistry`` and the host daemon's ``HostProcess`` import
handler) over in-memory queues instead of a WebSocket. Only persistence
(:class:`FakeConversationStore`) and the transcript readers are doubles.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import threading
import time
import types
from collections.abc import Callable, Collection, Mapping
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    HOST_CAPABILITIES,
    HostHelloFrame,
    HostImportLocalByIdFrame,
    HostImportLocalCancelFrame,
    HostImportLocalFrame,
    decode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes import host_tunnel
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.models import LocalSessionImport
from omnigent.stores.conversation_store import ConversationAlreadyExistsError
from omnigent.stores.host_store import Host

HOST_ID = "0123456789abcdef0123456789abcdef"


def message_item(text: str, response_id: str = "r1") -> NewConversationItem:
    """One user message item, the shape every fake transcript reader produces."""
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def local_session(session_id: str, *, items: int = 1, text: str = "hi") -> LocalSessionImport:
    """A normalized local transcript as ``load_local_session`` returns it."""
    return LocalSessionImport(
        source="claude",
        external_session_id=session_id,
        workspace="/repo",
        items=tuple(message_item(f"{text} {i}", f"r{i}") for i in range(items)),
    )


class FakeConversationStore:
    """In-memory conversation store with hooks to inject storage failures."""

    def __init__(self) -> None:
        self.conversations: dict[str, Any] = {}
        self.items: dict[str, list[NewConversationItem]] = {}
        self.external: dict[str, str] = {}
        self.deleted: list[str] = []
        # Called before an append / external-id write lands; raise to fail it.
        self.on_append: Callable[[str, list[NewConversationItem]], None] | None = None
        self.on_set_external: Callable[[str, str], None] | None = None

    def find_conversation_by_external_session_id(self, external_session_id: str) -> Any:
        conversation_id = self.external.get(external_session_id)
        return self.conversations.get(conversation_id) if conversation_id else None

    def create_conversation(self, **kwargs: Any) -> Any:
        conversation_id = kwargs["conversation_id"]
        if conversation_id in self.conversations:
            raise ConversationAlreadyExistsError(conversation_id)
        kwargs.setdefault("parent_conversation_id", None)
        conversation = types.SimpleNamespace(
            id=conversation_id,
            created_at=int(time.time()),
            external_session_id=None,
            **kwargs,
        )
        self.conversations[conversation_id] = conversation
        return conversation

    def get_conversation(self, conversation_id: str) -> Any:
        return self.conversations.get(conversation_id)

    def list_items(self, conversation_id: str, limit: int = 100, **_kwargs: Any) -> Any:
        return types.SimpleNamespace(data=list(self.items.get(conversation_id, []))[:limit])

    def set_external_session_id(self, conversation_id: str, external_session_id: str) -> None:
        if self.on_set_external is not None:
            self.on_set_external(conversation_id, external_session_id)
        self.external[external_session_id] = conversation_id
        self.conversations[conversation_id].external_session_id = external_session_id

    def append(self, conversation_id: str, items: list[NewConversationItem]) -> list[Any]:
        if self.on_append is not None:
            self.on_append(conversation_id, items)
        self.items.setdefault(conversation_id, []).extend(items)
        return []

    def set_labels(self, conversation_id: str, labels: dict[str, str]) -> None:
        self.conversations[conversation_id].labels = labels

    async def delete_conversation(self, conversation_id: str) -> bool:
        self.deleted.append(conversation_id)
        self.conversations.pop(conversation_id, None)
        self.items.pop(conversation_id, None)
        for external, mapped in list(self.external.items()):
            if mapped == conversation_id:
                del self.external[external]
        return True


class FakePermissionStore:
    """Grant rows only: who owns which conversation (no admins, no sharing)."""

    def __init__(self) -> None:
        self.grants: dict[tuple[str, str], int] = {}

    def is_admin(self, _user_id: str) -> bool:
        return False

    def check_access(self, user_id: str | None, conversation_id: str, required_level: int) -> bool:
        return self.grants.get((user_id or "", conversation_id), 0) >= required_level

    def has_any_grants(self, conversation_id: str) -> bool:
        return any(cid == conversation_id for _user, cid in self.grants)

    def ensure_user(self, _user_id: str) -> None:
        return None

    def grant(self, user_id: str, conversation_id: str, level: int) -> None:
        self.grants[(user_id, conversation_id)] = level


def fail_append_for(store: FakeConversationStore, external_id: str, exc: BaseException) -> None:
    """Make appending to ``external_id``'s import conversation raise ``exc``."""
    target = imports_module._import_conversation_id("claude", external_id)

    def on_append(conversation_id: str, _items: list[NewConversationItem]) -> None:
        if conversation_id == target:
            raise exc

    store.on_append = on_append


def host_record(*, name: str = "laptop", age_s: int = 0, status: str = "online") -> Host:
    """A host row whose last heartbeat was ``age_s`` seconds ago."""
    now = int(time.time())
    return Host(
        host_id=HOST_ID,
        name=name,
        user_id="local",
        status=status,
        created_at=now - 3600,
        updated_at=now - age_s,
    )


def make_hello(name: str = "laptop", capabilities: list[str] | None = None) -> HostHelloFrame:
    """A minimal hello frame for registering a host connection."""
    return HostHelloFrame(
        version="0", frame_protocol_version=1, name=name, capabilities=list(capabilities or [])
    )


def register_host(registry: HostRegistry, name: str = "laptop") -> HostConnection:
    """Register :data:`HOST_ID` with no socket behind it."""
    return registry.register(
        HOST_ID, ws=cast(Any, None), hello=make_hello(name), owner=None, workspace_id=0
    )


class _ServerSideWs:
    """What ``host_tunnel._receive_loop`` reads: frames the host sent."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[str | None] = asyncio.Queue()

    async def receive(self) -> dict[str, Any]:
        text = await self.inbound.get()
        if text is None:
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "websocket.receive", "text": text}


class _HostSideWs:
    """What the host daemon sends on: delivers straight to the server side."""

    def __init__(self, server_ws: _ServerSideWs) -> None:
        self._server_ws = server_ws
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)
        await self._server_ws.inbound.put(text)


class RecordingWs:
    """A host-side socket double that records every frame the host sends."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    def frames(self) -> list[Any]:
        """Every frame sent so far, decoded."""
        return [decode_host_frame(text) for text in self.sent]

    def as_ws(self) -> Any:
        """Typed as the websocket the host handlers expect."""
        return cast(Any, self)


def make_host(name: str = "laptop") -> HostProcess:
    """A host daemon with a test identity (never connected)."""
    return HostProcess(
        identity=HostIdentity(host_id=HOST_ID, name=name),
        server_url="http://localhost:8000",
    )


class TunnelPair:
    """A registered host connection whose far end is a real ``HostProcess``.

    ``legacy_host=True`` simulates a host build that predates import
    heartbeats and skip lists: it advertises no capabilities, ignores the
    request's ``progress`` flag and skip list, and drops the cancel frame.
    """

    def __init__(self, *, host_name: str = "laptop", legacy_host: bool = False) -> None:
        self.registry = HostRegistry()
        self.server_ws = _ServerSideWs()
        self.host_ws = _HostSideWs(self.server_ws)
        self.conn = self.registry.register(
            HOST_ID,
            ws=cast(Any, self.server_ws),
            hello=make_hello(host_name, [] if legacy_host else HOST_CAPABILITIES),
            owner=None,
            workspace_id=0,
        )
        self.host = make_host(host_name)
        self.legacy_host = legacy_host
        # Every server -> host frame, decoded, in send order.
        self.to_host: list[Any] = []
        self._tasks: list[asyncio.Task[Any]] = []

    async def __aenter__(self) -> TunnelPair:
        self._tasks = [
            asyncio.create_task(
                host_tunnel._receive_loop(
                    cast(Any, self.server_ws),
                    self.conn,
                    HOST_ID,
                    cast(Any, None),
                    self.registry,
                    None,
                    None,
                    None,
                )
            ),
            asyncio.create_task(self._pump_to_host()),
        ]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        for task in self._tasks:
            task.cancel()
        await self.host._quiesce_frame_tasks()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _pump_to_host(self) -> None:
        while True:
            text = await self.conn.outbound_queue.get()
            if text is None:
                return
            with contextlib.suppress(ValueError):
                self.to_host.append(decode_host_frame(text))
            if self.legacy_host:
                self._dispatch_legacy(text)
            else:
                self.host._start_frame_task(cast(Any, self.host_ws), text)

    def _dispatch_legacy(self, text: str) -> None:
        frame = decode_host_frame(text)
        if not isinstance(frame, (HostImportLocalFrame, HostImportLocalByIdFrame)):
            return  # an older host drops every frame kind it doesn't know
        legacy = dataclasses.replace(frame, progress=False)
        if isinstance(legacy, HostImportLocalFrame):
            legacy = dataclasses.replace(legacy, skip_external_session_ids=[])
        task = asyncio.create_task(self.host._handle_import_local(cast(Any, self.host_ws), legacy))
        self.host._frame_tasks.add(cast(Any, task))
        task.add_done_callback(self.host._frame_tasks.discard)

    def cancel_frames(self) -> list[HostImportLocalCancelFrame]:
        """Every cancel frame the server sent the host."""
        return [f for f in self.to_host if isinstance(f, HostImportLocalCancelFrame)]

    def host_frames(self) -> list[Any]:
        """Every host -> server frame the host sent, decoded."""
        return [decode_host_frame(text) for text in self.host_ws.sent]


def serve_local_sessions(
    monkeypatch: pytest.MonkeyPatch,
    sessions: Mapping[str, LocalSessionImport | BaseException],
    *,
    load_delay_s: float = 0.0,
    held: Collection[str] = (),
    release: threading.Event | None = None,
) -> None:
    """Make the host's transcript readers serve ``sessions`` (newest first).

    Reading any id in ``held`` blocks until ``release`` is set (callers set it
    in a ``finally``; the wait gives up after 10 s so a failed test can't hang).
    """
    order = list(sessions)

    def _across(*, limit: int) -> list[tuple[str, str]]:
        return [("claude", sid) for sid in order[:limit]]

    def _recent(_source: str, *, limit: int) -> tuple[str, ...]:
        return tuple(order[:limit])

    def _load(_source: str, session_id: str) -> LocalSessionImport:
        if load_delay_s:
            time.sleep(load_delay_s)
        if session_id in held and release is not None:
            release.wait(timeout=10)
        value = sessions[session_id]
        if isinstance(value, BaseException):
            raise value
        return value

    local = "omnigent.session_import.local"
    monkeypatch.setattr(f"{local}.list_recent_sessions_across_harnesses", _across)
    monkeypatch.setattr(f"{local}.list_recent_local_session_ids", _recent)
    monkeypatch.setattr(f"{local}.load_local_session", _load)


class JumpingClock:
    """A ``time`` module stand-in for the imports route whose monotonic clock can jump.

    Jumping past the stream deadline expires it at the next check, so deadline
    tests end on an event (e.g. the first persisted session), not wall time.
    """

    def __init__(self) -> None:
        self.offset = 0.0
        self.time = time.time

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def expire_deadline(self) -> None:
        """Move the clock past any stream deadline."""
        self.offset += 10 * imports_module._LOCAL_IMPORT_STREAM_DEADLINE_S


def expire_deadline_after_first_append(
    monkeypatch: pytest.MonkeyPatch, store: FakeConversationStore
) -> JumpingClock:
    """Make the stream deadline pass once the first session's items are written."""
    clock = JumpingClock()
    monkeypatch.setattr(imports_module, "time", clock)
    previous = store.on_append

    def on_append(conversation_id: str, items: list[NewConversationItem]) -> None:
        if previous is not None:
            previous(conversation_id, items)
        if not clock.offset:
            clock.expire_deadline()

    store.on_append = on_append
    return clock


def imports_app(
    conversation_store: FakeConversationStore,
    *,
    host_registry: HostRegistry | None = None,
    host: Host | None = None,
    permission_store: FakePermissionStore | None = None,
    user_id: str | None = None,
) -> FastAPI:
    """Mount the imports router with the production error-body shape.

    ``user_id`` turns auth on: every request is made as that user.
    """
    auth_provider = (
        types.SimpleNamespace(get_user_id=lambda _request: user_id)
        if user_id is not None
        else None
    )
    router = imports_module.create_imports_router(
        cast(Any, conversation_store),
        cast(Any, types.SimpleNamespace(get=lambda _agent_id: object())),
        auth_provider=cast(Any, auth_provider),
        permission_store=cast(Any, permission_store),
        host_registry=host_registry,
        host_store=cast(Any, types.SimpleNamespace(get_host=lambda _host_id: host)),
    )
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        # Mirrors omnigent.server.app's handler: details only add fields.
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {**exc.details, "code": exc.code, "message": exc.message}},
        )

    return app


def client(app: FastAPI, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    """An in-process HTTP client for ``app``."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def local_import_body(**overrides: Any) -> dict[str, Any]:
    """A ``/v1/imports/local`` request for :data:`HOST_ID`."""
    return {"host_id": HOST_ID, "source": "all", "limit": 10, **overrides}


def cli_import_body(external_session_id: str = "ext-1", **overrides: Any) -> dict[str, Any]:
    """A ``/v1/imports`` request carrying one user message."""
    return {
        "source": "claude",
        "external_session_id": external_session_id,
        "workspace": "/repo",
        "items": [
            {
                "type": "message",
                "response_id": "r1",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            }
        ],
        **overrides,
    }


def ndjson(response: httpx.Response) -> list[dict[str, Any]]:
    """Decode an NDJSON response body."""
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


async def post_stream(app: FastAPI, **body: Any) -> list[dict[str, Any]]:
    """POST ``/v1/imports/local/stream`` and return its events."""
    async with client(app) as http:
        response = await http.post("/v1/imports/local/stream", json=local_import_body(**body))
    assert response.status_code == 200, response.text
    return ndjson(response)


async def stream_through_tunnel(
    monkeypatch: pytest.MonkeyPatch,
    store: FakeConversationStore,
    sessions: Mapping[str, LocalSessionImport | BaseException],
    **body: Any,
) -> list[dict[str, Any]]:
    """Stream-import ``sessions`` from a real host over the in-memory tunnel."""
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, sessions)
    async with pair:
        return await post_stream(app, **body)


def error_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The single ``error`` event, asserting the stream still ended with ``done``."""
    (error,) = [e for e in events if e["event"] == "error"]
    assert events[-1]["event"] == "done"
    return error


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it holds or ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)
