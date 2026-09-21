"""Claude launch and forwarding paths honor the shared diagnostics opt-in."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from click import ClickException

from omnigent.harnesses.claude_native import bridge, forwarder
from omnigent.harnesses.claude_native import main as claude_native
from omnigent.process_logging import HARNESS_STDERR_ENABLED_ENV_VAR


@pytest.fixture(autouse=True)
def _isolated_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(HARNESS_STDERR_ENABLED_ENV_VAR, raising=False)
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)


@pytest.mark.parametrize("setting", [None, "0", "true"])
@pytest.mark.parametrize("launch_path", ["shared", "cli"])
def test_shared_and_cli_launch_arguments_opt_in_to_owned_debug_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    setting: str | None,
    launch_path: str,
) -> None:
    """Both launchers' shared augmentation adds diagnostics only when enabled."""
    if setting is not None:
        monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, setting)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    if launch_path == "cli":
        monkeypatch.setattr(claude_native, "resolve_claude_launch", lambda cmd, args: (cmd, args))
        body = claude_native._claude_terminal_request(
            ("--resume", "claude-session"), command="claude", bridge_dir=bridge_dir
        )
        args = body["spec"]["args"]
    else:
        args = bridge.augment_claude_args(("--resume", "claude-session"), bridge_dir=bridge_dir)

    assert args[:2] == ["--resume", "claude-session"]
    assert "--mcp-config" in args
    assert "--settings" in args
    if setting == "true":
        assert args.count("--debug-file") == 1
        diagnostic_path = Path(args[args.index("--debug-file") + 1])
        assert diagnostic_path.parent == bridge_dir
        assert diagnostic_path.name.startswith("claude-debug-")
        assert diagnostic_path.suffix == ".log"
    else:
        assert not any(arg == "--debug-file" or arg.startswith("--debug-file=") for arg in args)


@pytest.mark.parametrize("joined", [False, True])
def test_common_launch_arguments_preserve_user_debug_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    joined: bool,
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    user_debug_file = tmp_path / "user-debug.log"
    user_debug_file.write_text("existing user diagnostics\n", encoding="utf-8")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    original = (
        (f"--debug-file={user_debug_file}",) if joined else ("--debug-file", str(user_debug_file))
    )

    args = bridge.augment_claude_args(original, bridge_dir=bridge_dir)

    assert args[: len(original)] == list(original)
    assert sum(arg == "--debug-file" or arg.startswith("--debug-file=") for arg in args) == 1
    assert user_debug_file.read_text(encoding="utf-8") == "existing user diagnostics\n"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected forwarder request: {request.method}")

    @asynccontextmanager
    async def open_server_client(
        *_args: object, **_kwargs: object
    ) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            yield client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_server_client)


async def _forward(bridge_dir: Path) -> None:
    await forwarder.forward_claude_transcript_to_session(
        base_url="http://unused.invalid",
        headers={},
        session_id="original-session",
        bridge_dir=bridge_dir,
        agent_name="claude-native-test",
        start_at_end=False,
        poll_interval_s=0.01,
    )


async def test_diagnostics_continue_before_transcript_discovery_and_follow_session_rotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_network: None,
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    hook_wait_started = asyncio.Event()
    hook_release = asyncio.Event()
    first_poll = asyncio.Event()
    rotated_poll = asyncio.Event()
    active = SimpleNamespace(session_id="original-session")
    observed_sessions: list[str] = []

    async def wait_for_hook_state(*_args: object, **_kwargs: object) -> None:
        hook_wait_started.set()
        await hook_release.wait()

    def poll(session_id: str) -> None:
        observed_sessions.append(session_id)
        if session_id == "original-session":
            first_poll.set()
        if session_id == "cleared-session":
            rotated_poll.set()

    follower = SimpleNamespace(poll=Mock(side_effect=poll), close=Mock())
    make_follower = Mock(return_value=follower)
    transcript_discovery = Mock(return_value=None)
    monkeypatch.setattr(forwarder, "ClaudeDebugLogFollower", make_follower)
    monkeypatch.setattr(forwarder, "read_active_session_id", lambda _bridge: active.session_id)
    monkeypatch.setattr(forwarder, "_ensure_hook_state", wait_for_hook_state)
    monkeypatch.setattr(forwarder, "read_transcript_path", transcript_discovery)
    task = asyncio.create_task(_forward(tmp_path))
    try:
        await asyncio.wait_for(hook_wait_started.wait(), timeout=2.0)
        await asyncio.wait_for(first_poll.wait(), timeout=2.0)
        transcript_discovery.assert_not_called()
        active.session_id = "cleared-session"
        await asyncio.wait_for(rotated_poll.wait(), timeout=2.0)
        transcript_discovery.assert_not_called()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    make_follower.assert_called_once_with(tmp_path)
    assert observed_sessions[0] == "original-session"
    assert "cleared-session" in observed_sessions
    follower.close.assert_called_once_with("cleared-session")


async def test_disabled_diagnostics_do_not_start_a_follower(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_network: None,
) -> None:
    hook_wait_started = asyncio.Event()
    hook_release = asyncio.Event()

    async def wait_for_hook_state(*_args: object, **_kwargs: object) -> None:
        hook_wait_started.set()
        await hook_release.wait()

    make_follower = Mock()
    monkeypatch.setattr(forwarder, "ClaudeDebugLogFollower", make_follower)
    monkeypatch.setattr(forwarder, "_ensure_hook_state", wait_for_hook_state)
    task = asyncio.create_task(_forward(tmp_path))
    try:
        await asyncio.wait_for(hook_wait_started.wait(), timeout=2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    make_follower.assert_not_called()


async def test_server_client_start_failure_drains_diagnostics_before_first_poll(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    failure = RuntimeError("server client could not start")

    class FailedClientContext:
        async def __aenter__(self) -> None:
            raise failure

        async def __aexit__(self, *_args: object) -> None:
            return None

    follower = SimpleNamespace(poll=Mock(), close=Mock())
    make_follower = Mock(return_value=follower)
    monkeypatch.setattr(forwarder, "ClaudeDebugLogFollower", make_follower)
    monkeypatch.setattr(forwarder, "read_active_session_id", lambda _bridge: "active-child")
    monkeypatch.setattr(
        "omnigent.cli_auth.open_server_client", lambda *_a, **_kw: FailedClientContext()
    )

    with pytest.raises(RuntimeError) as raised:
        await _forward(tmp_path)

    assert raised.value is failure
    make_follower.assert_called_once_with(tmp_path)
    follower.poll.assert_not_called()
    follower.close.assert_called_once_with("active-child")


@pytest.mark.parametrize("failure_mode", ["rejected", "transport", "cancelled"])
async def test_cli_launch_failure_drains_diagnostics_and_preserves_original_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    monkeypatch.setattr(claude_native, "resolve_claude_launch", lambda cmd, args: (cmd, args))
    follower = SimpleNamespace(close=Mock())
    make_follower = Mock(return_value=follower)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.diagnostics.ClaudeDebugLogFollower", make_follower
    )
    posted: list[dict[str, object]] = []

    def launch_failure(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        if failure_mode == "cancelled":
            raise asyncio.CancelledError("launch cancelled")
        if failure_mode == "transport":
            raise httpx.ConnectError("simulated launch transport failure", request=request)
        return httpx.Response(503, json={"detail": "simulated launch rejection"})

    expected_error, expected_message = {
        "rejected": (ClickException, "503"),
        "transport": (httpx.ConnectError, "simulated launch transport failure"),
        "cancelled": (asyncio.CancelledError, "launch cancelled"),
    }[failure_mode]
    async with httpx.AsyncClient(
        base_url="http://unused.invalid", transport=httpx.MockTransport(launch_failure)
    ) as client:
        with pytest.raises(expected_error, match=expected_message):
            await claude_native._launch_claude_terminal(
                client, "failed-child", (), command="claude", bridge_dir=tmp_path
            )

    assert len(posted) == 1
    assert "--debug-file" in posted[0]["spec"]["args"]
    make_follower.assert_called_once_with(tmp_path)
    follower.close.assert_called_once_with("failed-child")


@pytest.mark.parametrize("failure_point", ["session_metadata", "poll", "close"])
async def test_diagnostic_failures_preserve_the_original_forwarder_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    follower = SimpleNamespace(poll=Mock(), close=Mock())
    monkeypatch.setattr(forwarder, "ClaudeDebugLogFollower", lambda _path: follower)
    if failure_point == "session_metadata":
        malformed = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid bridge metadata")
        monkeypatch.setattr(forwarder, "read_active_session_id", Mock(side_effect=malformed))
    elif failure_point == "poll":
        follower.poll.side_effect = ValueError("diagnostic poll failed")
    else:
        follower.close.side_effect = ValueError("diagnostic final drain failed")
    original_error = RuntimeError("original forwarding failure")

    with pytest.raises(RuntimeError) as raised:
        async with forwarder._forward_claude_diagnostics(tmp_path, "original-session", 0.01):
            await asyncio.sleep(0)
            raise original_error

    assert raised.value is original_error
    follower.poll.assert_called_once_with("original-session")
    follower.close.assert_called_once_with("original-session")
