"""Tests for the runner's
``WS /v1/sessions/{id}/resources/terminals/{terminal_id}/attach`` endpoint.

The endpoint resolves the opaque terminal resource id back to the
runner-local registry entry and bridges PTY bytes to the
browser-facing WebSocket via ``tmux attach``. These tests pin the
route boundary and registry lookup; the actual PTY bridge is
exercised by stubbing ``pty.fork`` / ``os.execve`` (the bridge
logic itself is unit-tested elsewhere via the shared helper).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.entities.session_resources import SessionResourceView
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import (
    OMNIGENT_REPL_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.terminals import TerminalRegistry
from tests.unit.runner.helpers import NullServerClient, make_test_terminal_instance


def _make_running_instance(name: str, session_key: str, tmp_path: Path) -> TerminalInstance:
    """A :class:`TerminalInstance` flagged running, bypassing real tmux.

    :param name: Terminal name from the spec, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param tmp_path: Pytest tmp directory used as the socket parent.
    :returns: The seeded :class:`TerminalInstance`.
    """
    return make_test_terminal_instance(name, session_key, tmp_path, running=True)


def _seed_registry(
    registry: TerminalRegistry,
    conversation_id: str,
    instance: TerminalInstance,
) -> None:
    """Insert *instance* into *registry* under *conversation_id*.

    :param registry: The :class:`TerminalRegistry` under test.
    :param conversation_id: Owning conversation/session id.
    :param instance: The :class:`TerminalInstance` to insert.
    """
    slot = registry._by_conversation.setdefault(conversation_id, {})
    slot[(instance.name, instance.session_key)] = instance


def test_runner_resource_attach_spawns_tmux_for_running_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With a running registry entry, the runner spawns ``tmux attach``
    against the entry's local socket path.

    Intercepts ``pty.fork`` to act as the child branch (returning 0)
    so ``execve`` runs in the test process; ``execve`` is stubbed to
    capture argv and the child env. ``_exit`` is raised as an exception so the child
    branch terminates the test rather than continuing into the
    parent path.

    :param tmp_path: Pytest tmp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    registry = TerminalRegistry()
    instance = _make_running_instance("bash", "s1", tmp_path)
    _seed_registry(registry, "conv_abc", instance)

    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
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
            "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach"
        ):
            pass

    # ``-r`` is absent (read_only defaulted to false) and the local
    # socket path from the registry is what's threaded in.
    assert captured["argv"][0] == "tmux"
    assert "-r" not in captured["argv"], (
        f"Expected no -r without read_only, got argv={captured['argv']!r}"
    )
    assert str(tmp_path / "bash-s1.sock") in captured["argv"]
    # The attach client always advertises the web terminal's real type;
    # inheriting the ambient TERM broke headless (sandbox) hosts.
    assert captured["env"]["TERM"] == "xterm-256color"


def test_runner_resource_attach_passes_read_only_to_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?read_only=true`` propagates as ``tmux attach -r``.

    :param tmp_path: Pytest tmp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    registry = TerminalRegistry()
    _seed_registry(
        registry,
        "conv_abc",
        _make_running_instance("bash", "s1", tmp_path),
    )
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
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
            "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?read_only=true"
        ):
            pass

    assert "-r" in captured["argv"], (
        f"Expected -r with read_only=true, got argv={captured['argv']!r}"
    )


def test_runner_resource_attach_unknown_terminal_closes_4404(tmp_path: Path) -> None:
    """An unknown terminal id closes with 4404.

    :param tmp_path: Pytest tmp directory.
    """
    registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_nope/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


def test_runner_resource_attach_defunct_terminal_closes_4404(tmp_path: Path) -> None:
    """A registry entry with ``running=False`` closes with 4404.

    Defunct entries exist briefly between tmux session death and the
    registry's eviction sweep; attaching to one would race the
    cleanup. Closing 4404 lets the browser show the same "no such
    terminal" path the list endpoint shows.

    :param tmp_path: Pytest tmp directory.
    """
    registry = TerminalRegistry()
    defunct = _make_running_instance("bash", "stale", tmp_path)
    defunct.running = False
    _seed_registry(registry, "conv_abc", defunct)
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_stale/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


def test_runner_resource_attach_dead_tmux_with_stale_flag_closes_4404(
    tmp_path: Path,
) -> None:
    """
    A stale ``running=True`` flag still closes with 4404 when tmux is gone.

    This pins the Claude-native exit bug: after Claude exits, the
    runner can still have an in-memory terminal entry marked running.
    Reattaching to that socket makes tmux print ``"no sessions"``.
    The attach route must probe tmux liveness first and surface the
    same terminal-gone close code the wrapper already treats as a
    normal end-of-session.

    :param tmp_path: Pytest tmp directory.
    """
    registry = TerminalRegistry()
    stale = _make_running_instance("bash", "stale", tmp_path)

    async def dead_tmux() -> bool:
        """
        Simulate tmux ``has-session`` reporting no live session.

        :returns: ``False`` after flipping the optimistic running flag.
        """
        stale.running = False
        return False

    stale.is_alive = dead_tmux  # type: ignore[method-assign]
    _seed_registry(registry, "conv_abc", stale)
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_stale/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    # 4404 is the wrapper's terminal-gone signal. A generic close
    # would look like a server bounce and restart the reconnect loop.
    assert exc_info.value.code == 4404
    assert stale.running is False


def test_runner_resource_attach_recreates_dead_repl_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A dead embedded REPL terminal is recreated on attach, not rejected.

    Pins the "[empty] terminal" bug: the REPL pane dies whenever the
    ``omnigent attach`` process exits (user Ctrl+C, crash at deferred
    start), but the registry keeps the stale entry, so before the fix
    every later attach closed 4404 and the web Terminal view stayed a
    dead, blank pane for the rest of the session. The attach route must
    instead tear down the stale entry, re-run the REPL auto-create, and
    bridge the fresh pane — and must NOT recreate again on the next
    attach once the fresh pane is live (recreating a live REPL would
    kill the user's running TUI).

    :param tmp_path: Pytest tmp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    registry = TerminalRegistry()
    stale = _make_running_instance("tui", "main", tmp_path)

    async def dead_tmux() -> bool:
        """
        Simulate ``tmux has-session`` reporting the REPL pane gone.

        :returns: ``False`` after flipping the optimistic running flag
            (mirrors the real ``is_alive`` side effect).
        """
        stale.running = False
        return False

    stale.is_alive = dead_tmux  # type: ignore[method-assign]
    _seed_registry(registry, "conv_abc", stale)

    resource_registry = SessionResourceRegistry(terminal_registry=registry)
    # Role stamped at auto-create time in production (resource_role);
    # seeded directly here to avoid spawning real tmux.
    resource_registry._terminal_roles[("conv_abc", "terminal_tui_main")] = (
        OMNIGENT_REPL_TERMINAL_ROLE
    )

    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=resource_registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # The recreated pane: same (tui, main) key, distinct socket path so
    # the bridge argv proves the fresh instance (not the stale one) was
    # attached.
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh = _make_running_instance("tui", "main", fresh_dir)
    auto_create_sessions: list[str] = []

    async def fake_auto_create(
        session_id: str,
        rr: SessionResourceRegistry,
        publish_event: object,
        *,
        server_client: object,
        agent_spec: object = None,
    ) -> SessionResourceView:
        """
        Stand-in for ``_auto_create_repl_terminal`` that registers a
        live pane without spawning real tmux.

        :param session_id: Session being recreated, e.g. ``"conv_abc"``.
        :param rr: The runner's resource registry (unused by the stub).
        :param publish_event: Per-session SSE emitter (unused).
        :param server_client: Omnigent server client (unused).
        :param agent_spec: Resolved session agent spec threaded by the
            recreate path so the REPL terminal inherits the agent sandbox
            (unused by the stub).
        :returns: Terminal resource view for the fresh pane.
        """
        auto_create_sessions.append(session_id)
        _seed_registry(registry, session_id, fresh)
        return SessionResourceView(
            id="terminal_tui_main",
            type="terminal",
            session_id=session_id,
            name="tui",
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_repl_terminal", fake_auto_create)

    attach_argvs: list[list[str]] = []

    def fake_fork() -> tuple[int, int]:
        """Drive the child branch of ``pty.fork`` in-process.

        :returns: ``(0, 0)`` — pid 0 selects the child path.
        """
        return 0, 0

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        """Capture the tmux attach argv instead of exec'ing.

        :param path: Absolute tmux binary path (unused).
        :param argv: Full attach argv, recorded per-attach so the
            test can assert which socket each bridge targeted.
        :param env: Child env built in the parent (unused here; the
            TERM pin is asserted by the spawns_tmux test above).
        """
        attach_argvs.append(argv)
        raise OSError("stop child path")

    exit_exc = RuntimeError("child exited")
    monkeypatch.setattr("omnigent.terminals.ws_bridge.pty.fork", fake_fork)
    monkeypatch.setattr("omnigent.terminals.ws_bridge.os.execve", fake_execve)
    monkeypatch.setattr(
        "omnigent.terminals.ws_bridge.os._exit",
        lambda code: (_ for _ in ()).throw(exit_exc),
    )

    # First attach: dead pane → recreate → bridge the fresh pane.
    with pytest.raises(RuntimeError, match="child exited"):
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_abc/resources/terminals/terminal_tui_main/attach"
        ):
            pass

    # The recreate ran exactly once, for this session. [] means the
    # route still closes 4404 (the pre-fix dead-end); a wrong id means
    # the recreate targeted another session's REPL.
    assert auto_create_sessions == ["conv_abc"]
    # The bridge attached the FRESH pane's socket. The stale socket
    # here would mean the route bridged the dead instance it was
    # supposed to replace.
    assert str(fresh_dir / "tui-main.sock") in attach_argvs[0]
    # The stale entry was evicted: the registry now resolves the
    # (tui, main) key to the recreated instance. The stale instance
    # surviving would leak its activity watcher and scratch dir.
    assert registry.get("conv_abc", "tui", "main") is fresh

    # Second attach: the fresh pane is live → bridge it directly. A
    # second auto-create call would mean the route recreates
    # unconditionally, killing the user's running REPL on every attach.
    with pytest.raises(RuntimeError, match="child exited"):
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_abc/resources/terminals/terminal_tui_main/attach"
        ):
            pass

    assert auto_create_sessions == ["conv_abc"]
    assert str(fresh_dir / "tui-main.sock") in attach_argvs[1]


def test_runner_resource_attach_dead_non_repl_terminal_keeps_4404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Recreate-on-attach is scoped to the REPL role — other dead
    terminals keep the strict 4404 contract.

    A dead agent-created terminal is meaningful state (the command
    ended); silently relaunching it would erase that signal and rerun
    its command. With the resource registry wired but no
    ``omnigent-repl`` role stamped, the dead-pane attach must close
    4404 and must not touch the REPL auto-create.

    :param tmp_path: Pytest tmp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    registry = TerminalRegistry()
    stale = _make_running_instance("bash", "s1", tmp_path)

    async def dead_tmux() -> bool:
        """
        Simulate ``tmux has-session`` reporting the pane gone.

        :returns: ``False`` after flipping the optimistic running flag.
        """
        stale.running = False
        return False

    stale.is_alive = dead_tmux  # type: ignore[method-assign]
    _seed_registry(registry, "conv_abc", stale)
    resource_registry = SessionResourceRegistry(terminal_registry=registry)

    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=resource_registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async def must_not_recreate(*args: object, **kwargs: object) -> None:
        """
        Fail the test if the REPL auto-create is reached.

        :param args: Positional arguments (unused).
        :param kwargs: Keyword arguments (unused).
        :returns: None.
        """
        raise AssertionError(
            "REPL auto-create was invoked for a non-REPL terminal — the "
            "recreate path must be gated on OMNIGENT_REPL_TERMINAL_ROLE."
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_repl_terminal", must_not_recreate)

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


def test_runner_resource_attach_without_registry_closes_4404() -> None:
    """Without a registry wired in, the endpoint closes 4404 rather
    than crashing.

    The runner scaffold path (``create_runner_app()`` with no args)
    hits this branch. Production paths always pass a registry.
    """
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


def test_runner_resource_attach_closes_4404_when_pty_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTY EOF mid-attach surfaces as 4404, not as a normal close.

    Models the user-reported failure mode: claude exits / tmux dies
    while the browser (or ``omnigent claude --server``) is attached.
    Without the dedicated close code, the client's reconnect loop in
    ``omnigent/claude_native.py`` interprets the close as a transient
    bounce and spins forever on "Claude session connection closed by
    server; reconnecting...". The fix has the bridge close with the
    same ``WS_CLOSE_TERMINAL_NOT_FOUND`` (4404) it already uses for
    the pre-attach lookup-miss case so the client's existing
    4404-handling exits the loop cleanly.

    Drives the parent branch of ``pty.fork`` with a socketpair as a
    stand-in for the PTY master fd. Closing the test-side socket
    triggers an EOF read on the bridge side, which ends ``_pty_to_ws``
    first and routes the close through the 4404 branch.

    :param tmp_path: Pytest tmp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import socket

    registry = TerminalRegistry()
    _seed_registry(registry, "conv_abc", _make_running_instance("bash", "s1", tmp_path))
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    pty_side, bridge_side = socket.socketpair()
    # Address the bridge_side socket via its fd — the bridge reads
    # with ``os.read(master_fd, ...)`` and registers it with
    # ``loop.add_reader``, both of which accept any readable fd.
    bridge_fd = bridge_side.fileno()

    def fake_fork() -> tuple[int, int]:
        # Parent branch: positive (deliberately invalid) pid plus the
        # socketpair fd as the PTY master. ``os.kill`` / ``os.waitpid``
        # in the bridge's finally are wrapped in ``contextlib.suppress``,
        # but monkey-patch ``os.kill`` here anyway to be safe in case
        # the bogus pid coincidentally maps to a live process the test
        # runner does not own.
        return 999_999, bridge_fd

    monkeypatch.setattr("omnigent.terminals.ws_bridge.pty.fork", fake_fork)
    monkeypatch.setattr("omnigent.terminals.ws_bridge.os.kill", lambda *_args, **_kw: None)

    try:
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach"
        ) as ws:
            # Simulate claude exiting inside tmux: the PTY read side
            # hits EOF as soon as the other end of the pair closes.
            pty_side.close()
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_bytes()
    finally:
        # ``pty_side`` is already closed; ``bridge_side`` was closed
        # by the bridge's ``os.close(master_fd)`` finally. Wrapping
        # the cleanup keeps the test green even if the bridge's
        # close ordering changes.
        with contextlib.suppress(OSError):
            pty_side.close()
        with contextlib.suppress(OSError):
            bridge_side.close()

    assert exc_info.value.code == 4404, (
        f"Expected 4404 close code on PTY EOF, got {exc_info.value.code}. "
        "Without 4404, the client's reconnect loop will spin on 'Claude "
        "session connection closed by server; reconnecting...' indefinitely."
    )
