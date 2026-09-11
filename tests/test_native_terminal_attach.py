"""Harness-independent control-mode selection for native terminals."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import click
import pytest
from click.testing import CliRunner
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from omnigent.harnesses.claude_native import main as claude_native
from omnigent.native.terminal_attach import (
    CONTROL_MODE_ATTACH_ENV,
    attach_native_terminal,
    attach_terminal_websocket,
)
from omnigent.terminals.ws_common import (
    WS_CLOSE_TERMINAL_DETACHED,
    WS_CLOSE_TERMINAL_NOT_FOUND,
)

_SIMPLE_LAUNCHERS = {
    "cursor": "Cursor",
    "pi": "Pi",
    "opencode": "OpenCode",
    "goose": "Goose",
    "kimi": "Kimi",
    "hermes": "Hermes",
    "qwen": "Qwen",
    "kiro": "Kiro",
}


@pytest.mark.parametrize("enabled", [False, True])
async def test_shared_selector_preserves_result(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """Only the chosen callback runs, and its lifecycle result is returned intact."""
    monkeypatch.setenv(CONTROL_MODE_ATTACH_ENV, "1" if enabled else "0")
    result = object()
    default = AsyncMock(return_value=result)
    control = AsyncMock(return_value=result)

    assert (
        await attach_native_terminal(default_attach=default, control_mode_attach=control) is result
    )
    selected, unused = (control, default) if enabled else (default, control)
    selected.assert_awaited_once_with()
    unused.assert_not_awaited()


async def test_failed_control_mode_does_not_silently_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed experimental attachment must not unexpectedly start another transport."""
    monkeypatch.setenv(CONTROL_MODE_ATTACH_ENV, "1")
    default = AsyncMock()
    control = AsyncMock(side_effect=RuntimeError("connection failed"))
    with pytest.raises(RuntimeError, match="connection failed"):
        await attach_native_terminal(default_attach=default, control_mode_attach=control)
    default.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("connection refused"),
        TimeoutError("opening handshake timed out"),
        ConnectionClosedError(Close(1011, "server failure"), None),
        InvalidStatus(Response(401, "Unauthorized", Headers())),
        InvalidStatus(Response(403, "Forbidden", Headers())),
        InvalidStatus(Response(502, "Bad Gateway", Headers())),
    ],
    ids=["refused", "timeout", "abnormal-close", "unauthorized", "forbidden", "proxy"],
)
async def test_terminal_websocket_reports_transport_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """The real reconnect loop's transport failures become actionable CLI errors."""
    attach = AsyncMock(side_effect=error)
    monkeypatch.setattr(claude_native, "attach_local_terminal", attach)

    with pytest.raises(
        click.ClickException, match="Terminal WebSocket connection failed"
    ) as raised:
        await attach_terminal_websocket(
            base_url="http://localhost",
            headers={},
            session_id="conv_selection",
            terminal_id="terminal_main",
            session_name="Cursor",
        )

    assert raised.value.__cause__ is error
    assert type(error).__name__ in raised.value.message
    assert str(error) in raised.value.message
    assert "resume" in raised.value.message
    assert "conv_selection" in raised.value.message
    attach.assert_awaited_once()


@pytest.mark.parametrize(
    "result",
    [
        True,
        False,
        ConnectionClosedError(Close(WS_CLOSE_TERMINAL_NOT_FOUND, ""), None),
        ConnectionClosedError(Close(WS_CLOSE_TERMINAL_DETACHED, ""), None),
    ],
    ids=["user-exit", "clean-close", "terminal-gone", "detached"],
)
async def test_terminal_websocket_preserves_normal_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: bool | ConnectionClosedError,
) -> None:
    """Normal closure and lifecycle sentinels must not become transport errors."""
    attach = (
        AsyncMock(side_effect=result)
        if isinstance(result, ConnectionClosedError)
        else AsyncMock(return_value=result)
    )
    monkeypatch.setattr(claude_native, "attach_local_terminal", attach)

    await attach_terminal_websocket(
        base_url="http://localhost",
        headers={},
        session_id="conv_selection",
        terminal_id="terminal_main",
        session_name="Cursor",
    )

    attach.assert_awaited_once()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "error", [RuntimeError("bug"), asyncio.CancelledError(), SystemExit(143), KeyboardInterrupt()]
)
async def test_terminal_websocket_does_not_wrap_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Programming errors, cancellation, and signal exits retain their meaning."""
    monkeypatch.setattr(claude_native, "attach_local_terminal", AsyncMock(side_effect=error))

    with pytest.raises(type(error)) as raised:
        await attach_terminal_websocket(
            base_url="http://localhost",
            headers={},
            session_id="conv_selection",
            terminal_id="terminal_main",
            session_name="Cursor",
        )

    assert raised.value is error


@pytest.mark.parametrize("harness", _SIMPLE_LAUNCHERS)
@pytest.mark.parametrize(
    ("enabled", "failed"),
    [(False, False), (True, False), (True, True)],
    ids=["default", "control-mode", "control-mode-failure"],
)
def test_native_launchers_use_shared_transport_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    enabled: bool,
    failed: bool,
) -> None:
    """Run each launcher through preparation and actual shared transport selection."""
    import omnigent.chat as chat
    import omnigent.cli as cli
    import omnigent.host.identity as identity

    launcher = importlib.import_module(f"omnigent.harnesses.{harness}_native.main")
    monkeypatch.setenv(CONTROL_MODE_ATTACH_ENV, "1" if enabled else "0")
    headers = {"Authorization": "Bearer test-token"}
    prepared = SimpleNamespace(
        session_id="conv_selection",
        terminal_id="terminal_main",
        reattached=True,
        cold_resumed=False,
    )
    monkeypatch.setattr(chat, "_remote_headers", Mock(return_value=headers))
    monkeypatch.setattr(cli, "_ensure_host_daemon", Mock())
    monkeypatch.setattr(
        identity,
        "load_or_create_host_identity",
        Mock(return_value=SimpleNamespace(host_id="host")),
    )
    monkeypatch.setattr(
        launcher, "_resolve_session_id_for_resume", Mock(return_value=prepared.session_id)
    )
    if hasattr(launcher, "_align_working_directory_with_session"):
        monkeypatch.setattr(launcher, "_align_working_directory_with_session", Mock())
    monkeypatch.setattr(
        launcher, f"_prepare_{harness}_terminal_via_daemon", AsyncMock(return_value=prepared)
    )
    monkeypatch.setattr(launcher, "open_conversation_link_if_enabled", Mock())
    direct = AsyncMock()
    websocket = AsyncMock(
        return_value=claude_native._AttachOutcome.EXITED,
        side_effect=ConnectionRefusedError("connection refused") if failed else None,
    )
    monkeypatch.setattr(launcher, "_attach_terminal_resource", direct)
    monkeypatch.setattr(claude_native, "_attach_with_reconnect", websocket)

    @click.command()
    def launch() -> None:
        launcher._run_with_remote_server(
            "http://localhost",
            tmp_path / "agent.yaml",
            session_id=prepared.session_id,
            resume_picker=False,
            **{f"{harness}_args": ()},
        )

    result = CliRunner().invoke(launch)
    assert result.exit_code == (1 if failed else 0), result.output
    if failed:
        assert isinstance(result.exception, SystemExit)
        assert "Error: Terminal WebSocket connection failed" in result.output
        assert "connection refused" in result.output
        assert "conv_selection" in result.output
        assert "resume" in result.output
        assert "Traceback" not in result.output

    if enabled:
        direct.assert_not_awaited()
        websocket.assert_awaited_once_with(
            attach=claude_native.attach_local_terminal,
            attach_url="ws://localhost/v1/sessions/conv_selection/resources/terminals/terminal_main/attach",
            headers=headers,
            recover=None,
            session_name=_SIMPLE_LAUNCHERS[harness],
            base_url="http://localhost",
            session_id=prepared.session_id,
            terminal_id=prepared.terminal_id,
            close_attach_on_terminal_gone=True,
        )
    else:
        direct.assert_awaited_once_with(prepared)
        websocket.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
async def test_runner_owned_codex_preserves_transport_and_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """Codex can opt in without a CLI app-server while preserving session-transfer hooks."""
    from omnigent.harnesses.codex_native import main as codex_native

    monkeypatch.setenv(CONTROL_MODE_ATTACH_ENV, "1" if enabled else "0")
    prepared = codex_native.PreparedCodexTerminal(
        session_id="conv_codex",
        terminal_id="terminal_main",
        tmux_socket=tmp_path / "tmux.sock",
        tmux_target="main",
        bridge_dir=tmp_path / "bridge",
        thread_id=None,
        app_server_url=None,
        app_server=None,
        event_client=None,
        reattached=True,
    )
    direct = AsyncMock()
    websocket = AsyncMock()
    recover = AsyncMock()
    monkeypatch.setattr(codex_native, "_direct_tmux_unavailable_reason", Mock(return_value=None))
    monkeypatch.setattr(codex_native, "_attach_direct_tmux", direct)
    monkeypatch.setattr(codex_native, "_attach_with_reconnect", websocket)
    active_session = Mock(return_value="conv_transferred")
    monkeypatch.setattr(codex_native, "_active_codex_session_id", active_session)

    await codex_native._attach_terminal_resource(
        base_url="http://localhost", headers={}, prepared=prepared, recover=recover
    )

    if enabled:
        direct.assert_not_awaited()
        websocket.assert_awaited_once()
        arguments = websocket.await_args.kwargs
        assert arguments["recover"] is recover
        assert arguments["session_name"] == "Codex"
        assert arguments["session_id"] == prepared.session_id
        assert arguments["terminal_id"] == prepared.terminal_id
        assert arguments["active_session_id_reader"]() == "conv_transferred"
        active_session.assert_called_once_with(prepared.bridge_dir)
    else:
        direct.assert_awaited_once_with(prepared.tmux_socket, "main")
        websocket.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("detached", [False, True])
async def test_antigravity_transport_preserves_exit_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    detached: bool,
) -> None:
    """Control-mode detach must not be mistaken for an exit that closes the terminal."""
    from omnigent.harnesses.antigravity_native import main as antigravity_native

    monkeypatch.setenv(CONTROL_MODE_ATTACH_ENV, "1" if enabled else "0")
    prepared = antigravity_native.PreparedAntigravityTerminal(
        session_id="conv_antigravity",
        terminal_id="terminal_main",
        bridge_dir=tmp_path,
        tmux_socket=tmp_path / "tmux.sock",
        tmux_target="main",
        reattached=False,
    )
    outcome = (
        claude_native._AttachOutcome.DETACHED if detached else claude_native._AttachOutcome.EXITED
    )
    direct = AsyncMock(return_value=outcome)
    websocket = AsyncMock(return_value=outcome)
    close = AsyncMock()
    recover = AsyncMock()
    monkeypatch.setattr(antigravity_native, "_can_attach_direct_tmux", Mock(return_value=True))
    monkeypatch.setattr(antigravity_native, "_attach_direct_tmux", direct)
    monkeypatch.setattr(antigravity_native, "_attach_with_reconnect", websocket)
    monkeypatch.setattr(antigravity_native, "_close_antigravity_terminal", close)
    monkeypatch.setattr(antigravity_native, "run_reader_with_bridge", AsyncMock())
    monkeypatch.setattr(antigravity_native, "_cold_start_agy_conversation", AsyncMock())

    await antigravity_native._attach_terminal(
        base_url="http://localhost", headers={}, prepared=prepared, recover=recover
    )

    if enabled:
        direct.assert_not_awaited()
        websocket.assert_awaited_once()
        assert websocket.await_args.kwargs["recover"] is recover
        assert websocket.await_args.kwargs["close_attach_on_terminal_gone"] is True
    else:
        direct.assert_awaited_once_with(prepared.tmux_socket, "main")
        websocket.assert_not_awaited()
    if detached:
        close.assert_not_awaited()
    else:
        close.assert_awaited_once_with(
            base_url="http://localhost",
            headers={},
            session_id=prepared.session_id,
            terminal_id=prepared.terminal_id,
        )


@pytest.mark.parametrize("flag", [None, "", "0", "false", "1"])
@pytest.mark.parametrize("local_socket", [False, True])
async def test_control_mode_attach_is_explicitly_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str | None,
    local_socket: bool,
) -> None:
    """Only an explicit opt-in bypasses a usable local tmux attachment."""
    if flag is None:
        monkeypatch.delenv(CONTROL_MODE_ATTACH_ENV, raising=False)
    else:
        monkeypatch.setenv(CONTROL_MODE_ATTACH_ENV, flag)
    socket = tmp_path / "tmux.sock"
    if local_socket:
        socket.touch()
    monkeypatch.setattr(claude_native.shutil, "which", lambda name: "/usr/bin/tmux")
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_selection",
        terminal_id="terminal_claude_main",
        bridge_dir=tmp_path / "bridge",
        reattached=True,
        tmux_socket=socket,
        tmux_target="main",
    )
    direct = AsyncMock(return_value=claude_native._AttachOutcome.DETACHED)
    reconnect = AsyncMock(return_value=claude_native._AttachOutcome.DETACHED)
    recover = AsyncMock()
    monkeypatch.setattr(claude_native, "_attach_direct_tmux", direct)
    monkeypatch.setattr(claude_native, "_attach_with_reconnect", reconnect)
    attach_url = "ws://localhost/terminal/attach"
    headers = {"Authorization": "Bearer test-token"}

    outcome = await claude_native._attach_with_transcript_forwarder(
        base_url="http://localhost",
        headers=headers,
        prepared=prepared,
        agent_name="claude",
        attach_url=attach_url,
        attach=claude_native.attach_local_terminal,
        recover=recover,
        run_transcript_forwarder=False,
    )

    assert outcome is claude_native._AttachOutcome.DETACHED
    assert claude_native._can_attach_direct_tmux(prepared) is local_socket
    if flag == "1" or not local_socket:
        direct.assert_not_awaited()
        reconnect.assert_awaited_once_with(
            attach=claude_native.attach_local_terminal,
            attach_url=attach_url,
            headers=headers,
            recover=recover,
            session_name="Claude",
            base_url="http://localhost",
            session_id=prepared.session_id,
            terminal_id=prepared.terminal_id,
            bridge_dir=prepared.bridge_dir,
            close_attach_on_terminal_gone=True,
        )
    else:
        direct.assert_awaited_once_with(socket, "main", startup_profiler=ANY)
        reconnect.assert_not_awaited()
    output = capsys.readouterr().err
    assert ("Experimental control-mode attach" in output) is (flag == "1")
    profile = claude_native._tmux_profile_detail(prepared)
    if flag == "1":
        assert profile == "websocket attach (experimental control mode)"
    elif local_socket:
        assert profile == "direct-tmux target=main"
    else:
        assert profile == "websocket attach (tmux socket not local)"
