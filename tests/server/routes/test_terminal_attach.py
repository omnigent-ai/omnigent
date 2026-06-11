"""Tests for the server's resource-addressed terminal-attach WS route.

The route at
``/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach``
proxies WS frames to the runner's matching endpoint when a runner
WS factory is configured (``set_runner_ws_factory``) and falls back
to running ``tmux attach`` against the server-side
:class:`TerminalRegistry` otherwise.

Two branches: proxy-to-runner-WS and local fallback. The local
fallback resolves the opaque terminal resource id back to a
``(terminal_name, session_key)`` pair via the registry and bridges
the PTY in-process.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.entities import Conversation, SessionPermission
from omnigent.inner.terminal import TerminalInstance
from omnigent.runtime import (
    _globals,
    set_runner_client,
    set_runner_router,
    set_runner_ws_factory,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    UnifiedAuthProvider,
)
from omnigent.server.routes.terminal_attach import create_terminal_attach_router
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import make_test_terminal_instance


class _StubPermissionStore:
    """Minimal in-memory permission store for terminal attach auth tests."""

    def __init__(self) -> None:
        self._grants: dict[tuple[str, str], SessionPermission] = {}
        self._admins: set[str] = set()

    def get(self, user_id: str, conversation_id: str) -> SessionPermission | None:
        return self._grants.get((user_id, conversation_id))

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins

    def add_grant(self, user_id: str, conversation_id: str, level: int) -> None:
        self._grants[(user_id, conversation_id)] = SessionPermission(
            user_id=user_id,
            conversation_id=conversation_id,
            level=level,
        )

    def check_access(self, user_id: str | None, conversation_id: str, required_level: int) -> bool:
        if user_id is None:
            return False
        grant = self.get(user_id, conversation_id)
        if grant is not None and grant.level >= required_level:
            return True
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        if public_grant is not None and public_grant.level >= required_level:
            return True
        return False

    def get_permission_level(self, user_id: str | None, conversation_id: str) -> int | None:
        if user_id is None:
            return None
        if self.is_admin(user_id):
            return LEVEL_OWNER
        grant = self.get(user_id, conversation_id)
        if grant is not None:
            return grant.level
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        if public_grant is not None:
            return public_grant.level
        return None


class _StubConversationStore:
    """Minimal in-memory conversation store for terminal attach auth tests."""

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    def add(self, conversation_id: str) -> None:
        self._conversations[conversation_id] = Conversation(
            id=conversation_id,
            created_at=0,
            updated_at=0,
            root_conversation_id=conversation_id,
        )


def _make_running_instance(name: str, session_key: str, tmp_path: Path) -> TerminalInstance:
    """
    Construct a :class:`TerminalInstance` with ``running=True``.

    The route only reads ``.running`` and the dict keys, so a stub
    instance is sufficient — no real tmux involvement.

    :param name: Terminal name, e.g. ``"bash"``.
    :param session_key: Session key, e.g. ``"s1"``.
    :param tmp_path: Pytest tmpdir for placeholder paths.
    :returns: A running :class:`TerminalInstance`.
    """
    return make_test_terminal_instance(name, session_key, tmp_path, running=True)


def _seed_registry(
    registry: TerminalRegistry,
    conversation_id: str,
    instances: list[TerminalInstance],
) -> None:
    """Insert *instances* into *registry* under *conversation_id*.

    Bypasses :meth:`TerminalRegistry.launch` (real tmux); mutates
    the private map directly because test infrastructure needs a
    deterministic state without exercising the launch path.

    :param registry: The registry to mutate.
    :param conversation_id: Owning conversation id.
    :param instances: Instances to register.
    """
    slot = registry._by_conversation.setdefault(conversation_id, {})
    for instance in instances:
        slot[(instance.name, instance.session_key)] = instance


@pytest.fixture
def server_registry(tmp_path: Path) -> Iterator[TerminalRegistry]:
    """
    Install a fresh :class:`TerminalRegistry` as the server's
    runtime singleton for the duration of the test.

    The route reads via :func:`omnigent.runtime.get_terminal_registry`,
    which dereferences the module-level singleton. Tests install
    their own registry and restore the prior value at teardown so
    they're isolated from each other and from any leftover state
    from earlier integration tests.

    :param tmp_path: Pytest tmpdir, unused here but kept so the
        fixture can yield a registry-typed value for downstream
        fixtures to seed.
    :yields: The installed :class:`TerminalRegistry`.
    """
    del tmp_path  # placeholder for shape consistency
    prior = _globals._terminal_registry
    registry = TerminalRegistry()
    _globals._terminal_registry = registry
    yield registry
    _globals._terminal_registry = prior


@pytest.fixture
def runner_client_reset() -> Iterator[None]:
    """
    Ensure runner globals are cleared at test start and restored at
    teardown.

    Without this, a prior test that called
    :func:`set_runner_client` or :func:`set_runner_ws_factory` would
    leak its client/factory into the fallback-path tests and silently
    change their behavior.
    """
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    prior_factory = _globals._runner_ws_factory
    set_runner_client(None)
    set_runner_router(None)
    set_runner_ws_factory(None)
    yield
    set_runner_client(prior_client)
    set_runner_router(prior_router)
    set_runner_ws_factory(prior_factory)


@pytest.fixture
def app(server_registry: TerminalRegistry, runner_client_reset: None) -> FastAPI:
    """A minimal FastAPI app that mounts the terminal-attach router.

    Avoids ``create_app`` to keep the test focused on the route -
    we don't need stores, lifespan, or any other router for these
    assertions.

    :param server_registry: The runtime registry fixture.
    :param runner_client_reset: Runner-client reset fixture.
    :returns: A FastAPI app with the terminals router mounted under
        ``/v1``.
    """
    del server_registry, runner_client_reset
    app = FastAPI()
    app.include_router(create_terminal_attach_router(), prefix="/v1")
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """httpx client routing through the test app via ASGI.

    :param app: The FastAPI app fixture.
    :yields: An async client targeted at the in-process app.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── WS attach: proxy to runner WS ─────────────────────────


class _FakeRunnerWSConn:
    """
    Stand-in for a connected :mod:`websockets` client connection.

    Captures sends from the proxy side; lets the test script the
    runner's outgoing frames and the eventual close. Used in lieu of
    binding a real local server so the test stays in-process and
    deterministic.

    To avoid teardown races (the proxy tears down as soon as either
    direction finishes), recv waits for ``wait_close_after`` browser
    frames to land before raising ``ConnectionClosed``. The default
    of 0 preserves the existing close-immediately behaviour for
    tests that drive teardown from the runner side.
    """

    def __init__(
        self,
        *,
        outgoing: list[bytes | str] | None = None,
        close_code: int | None = None,
        close_reason: str = "",
        wait_close_after: int = 0,
    ) -> None:
        """
        :param outgoing: Frames the runner emits before closing.
        :param close_code: WS close code the runner sends; ``None``
            defaults to a clean 1000.
        :param close_reason: WS close reason string.
        :param wait_close_after: Number of browser-to-runner frames
            to wait for before allowing the close to fire. Prevents
            teardown races in tests that drive the browser side.
        """
        self.outgoing: list[bytes | str] = list(outgoing or [])
        self.received: list[bytes | str] = []
        self._close_code = close_code
        self._close_reason = close_reason
        self._wait_close_after = wait_close_after
        self._recv_progress = asyncio.Event()

    async def send(self, data: bytes | str) -> None:
        """Record an inbound frame; signal the close gate if reached.

        :param data: The frame the proxy is forwarding.
        """
        self.received.append(data)
        if len(self.received) >= self._wait_close_after:
            self._recv_progress.set()

    async def recv(self) -> bytes | str:
        """Pop the next outgoing frame or raise ``ConnectionClosed``.

        :returns: The next runner-side frame to send to the browser.
        :raises ConnectionClosedOK: When no outgoing frames remain
            (and any optional close gate has been reached).
        """
        # Drain queued outgoing frames first.
        if self.outgoing:
            return self.outgoing.pop(0)
        # Optionally wait for browser-side frames to land before
        # closing. Without this, a recv() that returns immediately
        # tears the proxy down before the browser-to-runner direction
        # has a chance to push frames through.
        if self._wait_close_after > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._recv_progress.wait(), timeout=5.0)
        from websockets.exceptions import ConnectionClosedOK
        from websockets.frames import Close

        close = Close(self._close_code or 1000, self._close_reason)
        raise ConnectionClosedOK(close, None, None)


class _FakeRunnerWSFactory:
    """
    Callable matching the ``set_runner_ws_factory`` shape.

    Records the path it was called with and returns an async context
    manager yielding a :class:`_FakeRunnerWSConn`.
    """

    def __init__(self, conn: _FakeRunnerWSConn) -> None:
        """
        :param conn: The fake connection to yield to the proxy.
        """
        self._conn = conn
        self.calls: list[str] = []

    def __call__(self, path: str):
        """Return a context manager yielding the stored fake conn.

        :param path: The runner-side path the proxy constructed.
        :returns: An async context manager.
        """
        self.calls.append(path)

        class _CM:
            def __init__(self_inner, conn: _FakeRunnerWSConn) -> None:
                self_inner._conn = conn

            async def __aenter__(self_inner) -> _FakeRunnerWSConn:
                return self_inner._conn

            async def __aexit__(self_inner, exc_type, exc, tb) -> None:
                return None

        return _CM(self._conn)


async def test_attach_terminal_rejects_unauthorized_user_before_runner_proxy() -> None:
    """A user without session access cannot attach to another user's terminal."""
    conv_store = _StubConversationStore()
    conv_store.add("conv_alice")
    perm_store = _StubPermissionStore()
    perm_store.add_grant("alice@example.com", "conv_alice", LEVEL_EDIT)
    app = FastAPI()
    app.include_router(
        create_terminal_attach_router(
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=perm_store,  # type: ignore[arg-type]
            conversation_store=conv_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    factory = _FakeRunnerWSFactory(_FakeRunnerWSConn())
    set_runner_ws_factory(factory)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach",
            headers={"X-Forwarded-Email": "bob@example.com"},
        ):
            pass

    assert exc_info.value.code == 1008
    assert factory.calls == []


async def test_attach_terminal_rejects_missing_identity_before_runner_proxy() -> None:
    """Multi-user WebSocket attach requires an authenticated identity."""
    conv_store = _StubConversationStore()
    conv_store.add("conv_alice")
    perm_store = _StubPermissionStore()
    perm_store.add_grant("alice@example.com", "conv_alice", LEVEL_EDIT)
    app = FastAPI()
    app.include_router(
        create_terminal_attach_router(
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=perm_store,  # type: ignore[arg-type]
            conversation_store=conv_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    factory = _FakeRunnerWSFactory(_FakeRunnerWSConn())
    set_runner_ws_factory(factory)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach",
        ):
            pass

    assert exc_info.value.code == 1008
    assert factory.calls == []


async def test_attach_terminal_allows_owner_for_interactive_proxy() -> None:
    """The session owner may open an interactive (write) terminal attach."""
    conv_store = _StubConversationStore()
    conv_store.add("conv_alice")
    perm_store = _StubPermissionStore()
    perm_store.add_grant("alice@example.com", "conv_alice", LEVEL_OWNER)
    app = FastAPI()
    app.include_router(
        create_terminal_attach_router(
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=perm_store,  # type: ignore[arg-type]
            conversation_store=conv_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    conn = _FakeRunnerWSConn(wait_close_after=1)
    factory = _FakeRunnerWSFactory(conn)
    set_runner_ws_factory(factory)

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach",
        headers={"X-Forwarded-Email": "alice@example.com"},
    ) as ws:
        ws.send_bytes(b"whoami\n")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()

    assert factory.calls == [
        "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach?read_only=false"
    ]
    assert b"whoami\n" in conn.received


async def test_attach_terminal_edit_grant_denied_write_allowed_read_only() -> None:
    """A non-owner edit collaborator cannot type but may observe.

    A terminal is a single shared PTY whose keystrokes carry no
    per-user identity, so input is acted on (and, for the agent's TUI,
    persisted into history) as the owner. Holding *edit* on someone
    else's session is therefore not enough to drive their terminal: an
    interactive attach by Bob (edit on Alice's session) is refused
    before the runner proxy is dialed, while a read-only attach is
    allowed so Bob can still watch.
    """
    conv_store = _StubConversationStore()
    conv_store.add("conv_alice")
    perm_store = _StubPermissionStore()
    perm_store.add_grant("alice@example.com", "conv_alice", LEVEL_OWNER)
    perm_store.add_grant("bob@example.com", "conv_alice", LEVEL_EDIT)
    app = FastAPI()
    app.include_router(
        create_terminal_attach_router(
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=perm_store,  # type: ignore[arg-type]
            conversation_store=conv_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )

    interactive_factory = _FakeRunnerWSFactory(_FakeRunnerWSConn())
    set_runner_ws_factory(interactive_factory)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach",
            headers={"X-Forwarded-Email": "bob@example.com"},
        ):
            pass
    assert exc_info.value.code == 1008
    assert interactive_factory.calls == []

    readonly_conn = _FakeRunnerWSConn(outgoing=[b"output"])
    readonly_factory = _FakeRunnerWSFactory(readonly_conn)
    set_runner_ws_factory(readonly_factory)
    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach?read_only=true",
        headers={"X-Forwarded-Email": "bob@example.com"},
    ) as ws:
        assert ws.receive_bytes() == b"output"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    assert readonly_factory.calls == [
        "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach?read_only=true"
    ]


async def test_attach_terminal_read_grant_only_allows_read_only_proxy() -> None:
    """Read-level users may observe read-only terminals but cannot type."""
    conv_store = _StubConversationStore()
    conv_store.add("conv_alice")
    perm_store = _StubPermissionStore()
    perm_store.add_grant("viewer@example.com", "conv_alice", LEVEL_READ)
    app = FastAPI()
    app.include_router(
        create_terminal_attach_router(
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=perm_store,  # type: ignore[arg-type]
            conversation_store=conv_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )

    readonly_conn = _FakeRunnerWSConn(outgoing=[b"output"])
    readonly_factory = _FakeRunnerWSFactory(readonly_conn)
    set_runner_ws_factory(readonly_factory)
    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach?read_only=true",
        headers={"X-Forwarded-Email": "viewer@example.com"},
    ) as ws:
        assert ws.receive_bytes() == b"output"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()

    assert readonly_factory.calls == [
        "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach?read_only=true"
    ]

    interactive_factory = _FakeRunnerWSFactory(_FakeRunnerWSConn())
    set_runner_ws_factory(interactive_factory)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_alice/resources/terminals/terminal_bash_s1/attach",
            headers={"X-Forwarded-Email": "viewer@example.com"},
        ):
            pass

    assert exc_info.value.code == 1008
    assert interactive_factory.calls == []


async def test_attach_terminal_proxies_to_runner_ws_factory(app: FastAPI) -> None:
    """
    When a runner WS factory is set, the server proxies frames
    instead of running tmux locally.

    The proxy must forward the resource-addressed path (with the
    same ``terminal_id`` segment) to the runner and ferry binary
    frames from the runner back to the browser unmodified.

    :param app: The FastAPI app fixture.
    """
    pty_bytes = b"\x1b[31mhello\x1b[0m"
    conn = _FakeRunnerWSConn(outgoing=[pty_bytes])
    factory = _FakeRunnerWSFactory(conn)
    set_runner_ws_factory(factory)

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_ws/resources/terminals/terminal_bash_s1/attach?read_only=true"
    ) as ws:
        # The runner emits one binary frame, then closes cleanly.
        msg = ws.receive_bytes()
        assert msg == pty_bytes
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()

    assert factory.calls == [
        "/v1/sessions/conv_ws/resources/terminals/terminal_bash_s1/attach?read_only=true"
    ]


async def test_attach_terminal_proxy_forwards_browser_bytes_to_runner(
    app: FastAPI,
) -> None:
    """
    Binary frames the browser sends must reach the runner verbatim.

    Forwarding bytes is what makes keystrokes work; if the proxy
    accidentally text-encodes or buffers binary frames, xterm.js's
    ``onData`` stops functioning. This pins the byte-for-byte
    pass-through.

    :param app: The FastAPI app fixture.
    """
    # No outgoing frames from the runner; we send one frame from
    # the browser, then let the connection close once it arrives.
    conn = _FakeRunnerWSConn(wait_close_after=1)
    factory = _FakeRunnerWSFactory(conn)
    set_runner_ws_factory(factory)

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_ws/resources/terminals/terminal_bash_s1/attach"
    ) as ws:
        ws.send_bytes(b"ls\n")
        # Trigger the close path so the test exits.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()

    # The runner-side fake should have observed exactly the bytes
    # the browser sent. If text is observed, the proxy is corrupting
    # the binary direction.
    assert b"ls\n" in conn.received, (
        f"Expected runner to receive b'ls\\n', got {conn.received!r}. "
        f"If text appears, the binary-direction translation is wrong."
    )


async def test_attach_terminal_runner_close_propagates_close_code(
    app: FastAPI,
) -> None:
    """
    A runner-side WS close with code 4404 (terminal not found) is
    mirrored on the browser side so the UI can show "no such
    terminal" instead of a generic disconnect.

    :param app: The FastAPI app fixture.
    """
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    class _ImmediateCloseConn(_FakeRunnerWSConn):
        async def recv(self) -> bytes | str:
            close = Close(4404, "terminal not found or not running")
            raise ConnectionClosedError(close, None, None)

    factory = _FakeRunnerWSFactory(_ImmediateCloseConn())
    set_runner_ws_factory(factory)

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_ws/resources/terminals/terminal_bash_missing/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


# ── WS attach: local fallback when no ws factory ─────────


async def test_attach_terminal_local_fallback_missing_closes_4404(
    app: FastAPI,
) -> None:
    """
    Without a runner WS factory, the route falls back to the local
    registry. A missing terminal id closes with 4404.

    :param app: The FastAPI app fixture.
    """
    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_local/resources/terminals/terminal_bash_nope/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


async def test_attach_terminal_local_fallback_spawns_tmux(
    app: FastAPI,
    server_registry: TerminalRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With a seeded local entry and no WS factory, the route resolves
    the terminal id back to the registry entry and spawns ``tmux
    attach -t <target>`` against the local socket path. The fork is
    intercepted so we can verify the argv without invoking tmux.

    :param app: The FastAPI app fixture.
    :param server_registry: The runtime registry fixture.
    :param tmp_path: Pytest tmpdir.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    _seed_registry(
        server_registry,
        "conv_local_attach",
        [_make_running_instance("bash", "s1", tmp_path)],
    )
    # argv (list) and the child env (dict) land under separate keys.
    captured: dict[str, object] = {}

    def fake_fork() -> tuple[int, int]:
        return 0, 0

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        captured["argv"] = argv
        captured["env"] = env
        raise OSError("stop child path")

    exit_exc = RuntimeError("child exited")
    monkeypatch.setattr("omnigent.terminals.ws_bridge.pty.fork", fake_fork)
    # Production resolves the absolute tmux path and builds the child env
    # in the parent; the child calls os.execve (no PATH search, explicit
    # env) — patch execve, not execv/execvp.
    monkeypatch.setattr("omnigent.terminals.ws_bridge.os.execve", fake_execve)
    monkeypatch.setattr(
        "omnigent.terminals.ws_bridge.os._exit",
        lambda code: (_ for _ in ()).throw(exit_exc),
    )

    with pytest.raises(RuntimeError, match="child exited"):
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_local_attach/resources/terminals/terminal_bash_s1/attach"
            "?read_only=true"
        ):
            pass

    # tmux argv must include -r (read-only) and the local socket
    # path the registry knew about. If the socket path is the wrong
    # one, the resolver picked up the wrong registry entry.
    assert captured["argv"][0] == "tmux"
    assert "-r" in captured["argv"], (
        f"Expected '-r' in argv for read_only=true, got {captured['argv']!r}"
    )
    assert str(tmp_path / "bash-s1.sock") in captured["argv"], (
        f"Expected the registry's socket path in argv, got {captured['argv']!r}"
    )
    # The attach client always advertises the web terminal's real type;
    # inheriting the ambient TERM broke headless (sandbox) hosts.
    assert captured["env"]["TERM"] == "xterm-256color"
