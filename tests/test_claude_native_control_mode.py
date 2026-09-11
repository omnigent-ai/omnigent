"""Opt-in control-mode attachment for native Claude terminals."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, AsyncMock

import pytest

from omnigent.harnesses.claude_native import main as claude_native


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
        monkeypatch.delenv("OMNIGENT_EXPERIMENTAL_CLAUDE_CONTROL_MODE", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_EXPERIMENTAL_CLAUDE_CONTROL_MODE", flag)
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
