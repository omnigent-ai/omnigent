"""Terminal attach tests for Codex session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest

from omnigent.harnesses.codex_native import main as codex_native
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
)
from tests.harnesses.codex_native.session._support import (
    _FakeCodexAppServerClient,
)


class _FakeTerminalClient:
    """
    Minimal async client for terminal-launch helper tests.

    :param response: HTTP response returned from ``post``.
    """

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, Any], float | None]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        Capture a terminal-launch request.

        :param url: Request URL.
        :param json: JSON request body.
        :param timeout: Request timeout.
        :returns: Canned response.
        """
        self.posts.append((url, json, timeout))
        return self.response


def test_launch_codex_terminal_starts_fresh_remote_tui() -> None:
    """
    Fresh sessions let the Codex TUI create the remote app-server
    thread instead of resuming a pre-created rollout-less thread.

    :returns: None.
    """
    client = _FakeTerminalClient(httpx.Response(200, json={"id": "terminal_codex_main"}))

    launched = asyncio.run(
        codex_native._launch_codex_terminal(
            client,  # type: ignore[arg-type]
            "conv_abc",
            codex_args=("-c", "approval_policy=on-request"),
            command="/opt/codex/bin/codex",
            thread_id=None,
            remote_url="ws://127.0.0.1:9876",
            env={"CODEX_HOME": "/tmp/codex-home"},
        )
    )

    assert launched.terminal_id == "terminal_codex_main"
    assert launched.tmux_socket is None
    assert launched.tmux_target is None
    assert client.posts[0][1]["spec"]["args"] == [
        "-c",
        "approval_policy=on-request",
        "--remote",
        "ws://127.0.0.1:9876",
    ]
    assert client.posts[0][1]["spec"]["command"] == "/opt/codex/bin/codex"
    assert client.posts[0][1]["spec"]["tmux_allow_passthrough"] is True
    assert client.posts[0][1]["spec"]["tmux_start_on_attach"] is True


@pytest.mark.parametrize(
    ("codex_cli_version", "permission_args"),
    [
        ((0, 153, 1), ["-c", "approval_policy=on-request"]),
        ((0, 154, 0), []),
        (None, []),
    ],
)
def test_launch_codex_terminal_uses_remote_resume_order(
    codex_cli_version: tuple[int, int, int] | None,
    permission_args: list[str],
) -> None:
    """
    Terminal launch uses the Codex resume subcommand with ``--remote``
    before the thread id, matching Codex CLI parsing coverage.

    :returns: None.
    """
    client = _FakeTerminalClient(httpx.Response(200, json={"id": "terminal_codex_main"}))

    launched = asyncio.run(
        codex_native._launch_codex_terminal(
            client,  # type: ignore[arg-type]
            "conv_abc",
            codex_args=("-c", "approval_policy=on-request"),
            command="/opt/codex/bin/codex",
            thread_id="thread_123",
            remote_url="ws://127.0.0.1:9876",
            env={"CODEX_HOME": "/tmp/codex-home"},
            codex_cli_version=codex_cli_version,
        )
    )

    assert launched.terminal_id == "terminal_codex_main"
    assert client.posts == [
        (
            "/v1/sessions/conv_abc/resources/terminals",
            {
                "terminal": "codex",
                "session_key": "main",
                "spec": {
                    "command": "/opt/codex/bin/codex",
                    "args": [
                        *permission_args,
                        "resume",
                        "--remote",
                        "ws://127.0.0.1:9876",
                        "thread_123",
                    ],
                    "os_env_type": "caller_process",
                    "cwd": str(Path.cwd()),
                    "env": {"CODEX_HOME": "/tmp/codex-home"},
                    "scrollback": 100_000,
                    "tmux_allow_passthrough": True,
                    "tmux_start_on_attach": True,
                },
            },
            30.0,
        )
    ]


def test_launch_codex_terminal_extracts_tmux_attach_metadata(
    tmp_path: Path,
) -> None:
    """
    Terminal launch returns the runner tmux coordinates needed for
    direct local attach.

    This fails if the Codex wrapper only keeps the terminal id and is
    forced back through the WebSocket terminal bridge even when the
    runner exposed a local tmux socket.

    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """
    socket_path = tmp_path / "tmux.sock"
    client = _FakeTerminalClient(
        httpx.Response(
            200,
            json={
                "id": "terminal_codex_main",
                "metadata": {
                    "tmux_socket": str(socket_path),
                    "tmux_target": "main",
                },
            },
        )
    )

    launched = asyncio.run(
        codex_native._launch_codex_terminal(
            client,  # type: ignore[arg-type]
            "conv_abc",
            codex_args=(),
            command="/opt/codex/bin/codex",
            thread_id="thread_123",
            remote_url="ws://127.0.0.1:9876",
            env={},
        )
    )

    assert launched.terminal_id == "terminal_codex_main"
    assert launched.tmux_socket == socket_path
    assert launched.tmux_target == "main"


@pytest.mark.asyncio
@pytest.mark.parametrize("attach_fails", [False, True])
async def test_attach_retains_preload_subscription_until_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attach_fails: bool,
) -> None:
    """The same subscribed client must survive attachment and reach the forwarder."""
    client = _FakeCodexAppServerClient()
    forwarded: list[object] = []

    async def forward(**kwargs: Any) -> None:
        forwarded.append(kwargs["client"])
        await asyncio.Event().wait()

    async def attach(**_kwargs: Any) -> None:
        await asyncio.sleep(0)
        assert forwarded == [client]
        assert not client.closed
        if attach_fails:
            raise RuntimeError("attach failed")

    async def close(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(codex_native, "supervise_forwarder", forward)
    monkeypatch.setattr(codex_native, "_attach_terminal_resource", attach)
    monkeypatch.setattr(codex_native, "_close_codex_terminal", close)
    prepared = codex_native.PreparedCodexTerminal(
        session_id="conv_test",
        terminal_id="terminal_test",
        tmux_socket=None,
        tmux_target=None,
        bridge_dir=tmp_path,
        thread_id="thread_test",
        app_server_url="ws://127.0.0.1:9876",
        app_server=SimpleNamespace(close=close),  # type: ignore[arg-type]
        event_client=client,  # type: ignore[arg-type]
        reattached=False,
    )
    operation = codex_native._attach_with_forwarder(
        base_url="http://127.0.0.1:8000", headers={}, prepared=prepared, prompt=None
    )
    if attach_fails:
        with pytest.raises(RuntimeError, match="attach failed"):
            await operation
    else:
        await operation
    assert client.closed


def test_attach_with_forwarder_uses_direct_tmux_when_socket_is_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A local runner tmux socket bypasses the WebSocket terminal bridge.

    Breaking the direct attach branch would make this call invoke
    ``_attach_with_reconnect`` and fail the test before any fake tmux
    attach is recorded.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    attached: list[tuple[Path, str]] = []

    async def fake_attach_direct_tmux(path: Path, target: str) -> None:
        """
        Record the direct tmux attach request.

        :param path: Tmux socket path.
        :param target: Tmux target.
        :returns: None.
        """
        attached.append((path, target))

    async def fail_attach_with_reconnect(**_kwargs: object) -> None:
        """
        Fail if the WebSocket bridge path is used.

        :returns: None.
        """
        raise AssertionError("WebSocket attach path should not be used")

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.main.shutil.which", lambda _name: "/usr/bin/tmux"
    )
    monkeypatch.setattr(codex_native, "_attach_direct_tmux", fake_attach_direct_tmux)
    monkeypatch.setattr(codex_native, "_attach_with_reconnect", fail_attach_with_reconnect)

    asyncio.run(
        codex_native._attach_with_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            prepared=codex_native.PreparedCodexTerminal(
                session_id="conv_abc",
                terminal_id="terminal_codex_main",
                tmux_socket=socket_path,
                tmux_target="main",
                bridge_dir=tmp_path / "bridge",
                thread_id="thread_123",
                app_server_url="ws://127.0.0.1:9876",
                app_server=None,
                event_client=None,
                reattached=True,
            ),
            prompt=None,
        )
    )

    assert attached == [(socket_path, "main")]


def test_attach_with_forwarder_attaches_before_waiting_for_fresh_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Fresh tmux Codex sessions attach before waiting for ``thread/started``.

    The Codex pane waits for the first tmux client before starting, so
    waiting for the app-server thread before attaching would deadlock
    and would also force Codex to query terminal colors while detached.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """
    attach_started = asyncio.Event()
    allow_attach_exit = asyncio.Event()
    forwarded_threads: list[str | None] = []

    class _FakeAppServer:
        """Minimal app-server handle for attach cleanup."""

        async def close(self) -> None:
            """
            Close the fake app-server.

            :returns: None.
            """

    async def fake_attach_terminal_resource(**_kwargs: object) -> None:
        """
        Record that the terminal attach started and wait to exit.

        :returns: None.
        """
        attach_started.set()
        await allow_attach_exit.wait()

    async def fake_initialize_fresh_terminal_thread(**kwargs: object) -> str:
        """
        Assert initialization runs only after the attach has started.

        :param kwargs: Initialization keyword arguments.
        :returns: Fake Codex thread id.
        """
        del kwargs
        assert attach_started.is_set()
        allow_attach_exit.set()
        return "thread_123"

    def fake_start_codex_forwarder(**kwargs: object) -> asyncio.Task[None]:
        """
        Record the thread id used for the forwarder.

        :param kwargs: Forwarder keyword arguments.
        :returns: Cancellable no-op task.
        """
        prepared = kwargs["prepared"]
        assert isinstance(prepared, codex_native.PreparedCodexTerminal)
        forwarded_threads.append(prepared.thread_id)
        return asyncio.create_task(asyncio.sleep(3600))

    async def fake_start_initial_turn(_socket_path: Path, _thread_id: str, _prompt: str) -> None:
        """
        Fail if an initial prompt is unexpectedly sent.

        :returns: None.
        """
        raise AssertionError("no initial prompt expected")

    monkeypatch.setattr(codex_native, "_attach_terminal_resource", fake_attach_terminal_resource)
    monkeypatch.setattr(
        codex_native,
        "_initialize_fresh_terminal_thread",
        fake_initialize_fresh_terminal_thread,
    )
    monkeypatch.setattr(codex_native, "_start_codex_forwarder", fake_start_codex_forwarder)
    monkeypatch.setattr(codex_native, "_start_initial_turn", fake_start_initial_turn)

    asyncio.run(
        codex_native._attach_with_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            prepared=codex_native.PreparedCodexTerminal(
                session_id="conv_abc",
                terminal_id="terminal_codex_main",
                tmux_socket=tmp_path / "tmux.sock",
                tmux_target="main",
                bridge_dir=tmp_path / "bridge",
                thread_id=None,
                app_server_url="ws://127.0.0.1:9876",
                app_server=_FakeAppServer(),  # type: ignore[arg-type]
                event_client=object(),  # type: ignore[arg-type]
                reattached=False,
            ),
            prompt=None,
        )
    )

    assert forwarded_threads == ["thread_123"]


def test_attach_with_forwarder_closes_active_rotated_session_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Wrapper exit closes the terminal on the active rotated Omnigent session.

    Codex ``/clear`` transfers the terminal resource to a replacement
    session. Shutdown must follow the bridge state written by the
    forwarder; closing the original session would leave the transferred
    terminal live.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake bridge state.
    :returns: None.
    """
    bridge_dir = tmp_path / "bridge"
    closed_terminals: list[tuple[str, str]] = []
    app_server_closed = False

    class _FakeAppServer:
        """Minimal app-server handle for attach cleanup."""

        async def close(self) -> None:
            """
            Record app-server cleanup.

            :returns: None.
            """
            nonlocal app_server_closed
            app_server_closed = True

    async def fake_attach_terminal_resource(**_kwargs: object) -> None:
        """
        Simulate ``/clear`` rotating Omnigent ownership during attach.

        :returns: None.
        """
        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id="conv_rotated",
                socket_path=str(tmp_path / "codex.sock"),
                thread_id="thread_after_clear",
                codex_home=str(tmp_path / "codex-home"),
            ),
        )

    def fake_start_codex_forwarder(**_kwargs: object) -> asyncio.Task[None]:
        """
        Return a cancellable no-op forwarder task.

        :returns: Running task that never completes on its own.
        """
        return asyncio.create_task(asyncio.sleep(3600))

    async def fake_close_codex_terminal(**kwargs: object) -> None:
        """
        Record the terminal close target.

        :param kwargs: Close helper keyword arguments.
        :returns: None.
        """
        closed_terminals.append(
            (
                str(kwargs["session_id"]),
                str(kwargs["terminal_id"]),
            )
        )

    monkeypatch.setattr(codex_native, "_attach_terminal_resource", fake_attach_terminal_resource)
    monkeypatch.setattr(codex_native, "_start_codex_forwarder", fake_start_codex_forwarder)
    monkeypatch.setattr(codex_native, "_close_codex_terminal", fake_close_codex_terminal)

    asyncio.run(
        codex_native._attach_with_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            prepared=codex_native.PreparedCodexTerminal(
                session_id="conv_original",
                terminal_id="terminal_codex_main",
                tmux_socket=tmp_path / "tmux.sock",
                tmux_target="main",
                bridge_dir=bridge_dir,
                thread_id="thread_before_clear",
                app_server_url="ws://127.0.0.1:9876",
                app_server=_FakeAppServer(),  # type: ignore[arg-type]
                event_client=None,
                reattached=False,
            ),
            prompt=None,
        )
    )

    assert closed_terminals == [("conv_rotated", "terminal_codex_main")]
    assert app_server_closed


def test_attach_terminal_resource_runner_owned_missing_socket_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Runner-owned Codex terminals do not fall back to WebSocket attach.

    The CLI should only attach to the runner's tmux socket for this
    shape; if the socket metadata is stale or non-local, falling back to
    the Omnigent terminal WebSocket would reintroduce CLI-owned terminal IO.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """

    async def fail_attach_with_reconnect(**_kwargs: object) -> None:
        """
        Fail if the WebSocket bridge path is used.

        :returns: None.
        """
        raise AssertionError("Runner-owned Codex attach must not use WebSocket")

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.main.shutil.which", lambda _name: "/usr/bin/tmux"
    )
    monkeypatch.setattr(codex_native, "_attach_with_reconnect", fail_attach_with_reconnect)

    with pytest.raises(click.ClickException) as exc_info:
        asyncio.run(
            codex_native._attach_terminal_resource(
                base_url="http://127.0.0.1:8000",
                headers={},
                prepared=codex_native.PreparedCodexTerminal(
                    session_id="conv_abc",
                    terminal_id="terminal_codex_main",
                    tmux_socket=tmp_path / "missing.sock",
                    tmux_target="main",
                    bridge_dir=tmp_path / "bridge",
                    thread_id="thread_123",
                    app_server_url=None,
                    app_server=None,
                    event_client=None,
                    reattached=True,
                ),
                recover=None,
            )
        )

    message = str(exc_info.value)
    assert "Runner-owned Codex terminal requires direct tmux attach" in message
    assert str(tmp_path / "missing.sock") in message
    assert "WebSocket" not in message


def test_attach_with_forwarder_falls_back_when_tmux_socket_is_not_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Non-local runner sockets keep using the Omnigent terminal attach bridge.

    This is the remote-runner case: the resource may advertise a socket
    path from another host, but the CLI can only direct-attach when that
    path exists locally.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """
    websocket_attaches: list[str] = []
    bridge_dir = tmp_path / "bridge"
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_rotated",
            socket_path=str(tmp_path / "codex.sock"),
            thread_id="thread_456",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )

    async def fail_attach_direct_tmux(_path: Path, _target: str) -> None:
        """
        Fail if direct tmux attach is attempted.

        :returns: None.
        """
        raise AssertionError("Direct tmux attach should not be used")

    async def fake_attach_with_reconnect(**kwargs: object) -> None:
        """
        Record the WebSocket attach URL.

        :param kwargs: Attach loop keyword arguments.
        :returns: None.
        """
        attach_url = kwargs["attach_url"]
        assert isinstance(attach_url, str)
        assert kwargs["session_name"] == "Codex"
        active_session_id_reader = kwargs["active_session_id_reader"]
        assert callable(active_session_id_reader)
        assert active_session_id_reader() == "conv_rotated"
        websocket_attaches.append(attach_url)

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.main.shutil.which", lambda _name: "/usr/bin/tmux"
    )
    monkeypatch.setattr(codex_native, "_attach_direct_tmux", fail_attach_direct_tmux)
    monkeypatch.setattr(codex_native, "_attach_with_reconnect", fake_attach_with_reconnect)

    asyncio.run(
        codex_native._attach_with_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            prepared=codex_native.PreparedCodexTerminal(
                session_id="conv_abc",
                terminal_id="terminal_codex_main",
                tmux_socket=tmp_path / "missing.sock",
                tmux_target="main",
                bridge_dir=bridge_dir,
                thread_id="thread_123",
                app_server_url="ws://127.0.0.1:9876",
                app_server=None,
                event_client=None,
                reattached=True,
            ),
            prompt=None,
        )
    )

    assert websocket_attaches == [
        "ws://127.0.0.1:8000/v1/sessions/conv_abc/resources/terminals/terminal_codex_main/attach"
    ]
