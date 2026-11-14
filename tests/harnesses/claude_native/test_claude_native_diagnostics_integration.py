"""Claude launch and forwarding paths honor the shared diagnostics opt-in."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
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
    loop = asyncio.get_running_loop()

    async def wait_for_hook_state(*_args: object, **_kwargs: object) -> None:
        hook_wait_started.set()
        await hook_release.wait()

    def poll(session_id: str) -> None:
        observed_sessions.append(session_id)
        if session_id == "original-session":
            loop.call_soon_threadsafe(first_poll.set)
        if session_id == "cleared-session":
            loop.call_soon_threadsafe(rotated_poll.set)

    follower = SimpleNamespace(poll=Mock(side_effect=poll), close=Mock(), health_snapshot=dict)
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

    follower = SimpleNamespace(poll=Mock(), close=Mock(), health_snapshot=dict)
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


@pytest.mark.parametrize(
    "owned_request,failure_mode",
    [
        (False, "rejected"),
        (False, "transport"),
        (False, "cancelled"),
        (True, "rejected"),
        (True, "reactivated-ack"),
        (True, "transport"),
        (True, "cleanup-failure"),
        (True, "read"),
        (True, "write"),
        (True, "timeout"),
        (True, "cancelled"),
        (True, "unacknowledged"),
        (True, "mismatched"),
    ],
)
async def test_cli_launch_failure_drains_diagnostics_and_preserves_original_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
    owned_request: bool,
) -> None:
    from omnigent.harnesses.claude_native.settings_generation import (
        ClaudeSettingsLease,
        ClaudeSettingsRequest,
        acquire_claude_settings_request,
        prune_unused_claude_settings,
        request_claude_bridge_deletion,
    )

    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    monkeypatch.setattr(claude_native, "resolve_claude_launch", lambda cmd, args: (cmd, args))
    bridge_dir = (
        bridge.prepare_bridge_dir("failed-child", workspace=tmp_path)
        if failure_mode == "reactivated-ack"
        else tmp_path
    )
    follower = SimpleNamespace(close=Mock())
    make_follower = Mock(return_value=follower)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.diagnostics.ClaudeDebugLogFollower", make_follower
    )
    posted: list[dict[str, object]] = []
    original_error: BaseException | None = None
    receiver: ClaudeSettingsLease | None = None
    admitted_settings: bytes | None = None
    uncertain_errors = {
        "read": httpx.ReadError,
        "write": httpx.WriteError,
        "timeout": httpx.ReadTimeout,
    }
    if failure_mode == "cleanup-failure":
        monkeypatch.setattr(
            ClaudeSettingsRequest,
            "cancel_undelivered_dispatch",
            Mock(side_effect=OSError("simulated cleanup failure")),
        )

    def launch_failure(request: httpx.Request) -> httpx.Response:
        nonlocal original_error, receiver, admitted_settings
        if request.method == "GET":
            return httpx.Response(
                200, json={"claude_settings_request_protocol": 1} if owned_request else {}
            )
        posted.append(json.loads(request.content))
        if failure_mode == "cancelled":
            original_error = asyncio.CancelledError("launch cancelled")
            raise original_error
        if failure_mode in {"transport", "cleanup-failure"}:
            original_error = httpx.ConnectError(
                "simulated launch transport failure", request=request
            )
            raise original_error
        if failure_mode in {"unacknowledged", "mismatched"}:
            return httpx.Response(
                200,
                json={
                    "id": "terminal_claude_main",
                    **(
                        {"claude_settings_request_ack": "0" * 32}
                        if failure_mode == "mismatched"
                        else {}
                    ),
                },
            )
        if owned_request:
            if failure_mode in uncertain_errors:
                receiver = acquire_claude_settings_request(
                    bridge_dir, posted[-1]["claude_settings_request"]
                )
                admitted_settings = (
                    bridge_dir / f"claude-settings-{receiver.generation}.json"
                ).read_bytes()
                original_error = uncertain_errors[failure_mode](
                    "simulated failure after admission", request=request
                )
                raise original_error
            generation = posted[-1]["claude_settings_request"]
            with acquire_claude_settings_request(bridge_dir, generation):
                if failure_mode == "reactivated-ack":
                    request_claude_bridge_deletion(bridge_dir, "failed-child")
                    bridge.prepare_bridge_dir("failed-child", workspace=tmp_path)
                    return httpx.Response(
                        409,
                        json={
                            "detail": "simulated launch rejection",
                            "claude_settings_request_ack": generation,
                        },
                    )
        return httpx.Response(503, json={"detail": "simulated launch rejection"})

    expected_error, expected_message = {
        "rejected": (ClickException, "503"),
        "reactivated-ack": (ClickException, r"\(409\): simulated launch rejection"),
        "transport": (httpx.ConnectError, "simulated launch transport failure"),
        "cleanup-failure": (httpx.ConnectError, "simulated launch transport failure"),
        "read": (httpx.ReadError, "simulated failure after admission"),
        "write": (httpx.WriteError, "simulated failure after admission"),
        "timeout": (httpx.ReadTimeout, "simulated failure after admission"),
        "cancelled": (asyncio.CancelledError, "launch cancelled"),
        "unacknowledged": (ClickException, "did not acknowledge"),
        "mismatched": (ClickException, "did not acknowledge"),
    }[failure_mode]
    try:
        async with httpx.AsyncClient(
            base_url="http://unused.invalid", transport=httpx.MockTransport(launch_failure)
        ) as client:
            with pytest.raises(expected_error, match=expected_message) as raised:
                await claude_native._launch_claude_terminal(
                    client, "failed-child", (), command="claude", bridge_dir=bridge_dir
                )

        if original_error is not None:
            assert raised.value is original_error
        assert len(posted) == 1
        assert ("claude_settings_request" in posted[0]) is owned_request
        if owned_request:
            generation = posted[0]["claude_settings_request"]
            if failure_mode in {"rejected", "transport"}:
                assert not list(bridge_dir.glob("claude-settings-*"))
            else:
                settings = bridge_dir / f"claude-settings-{generation}.json"
                assert settings.exists()
                if admitted_settings is not None:
                    assert settings.read_bytes() == admitted_settings
                assert (bridge_dir / f"claude-settings-{generation}.dispatch.owner").exists() is (
                    failure_mode not in uncertain_errors and failure_mode != "reactivated-ack"
                )
        assert "--debug-file" in posted[0]["spec"]["args"]
        make_follower.assert_called_once_with(bridge_dir)
        follower.close.assert_called_once_with("failed-child")
    finally:
        if receiver is not None:
            receiver.close()
    if failure_mode == "reactivated-ack":
        prune_unused_claude_settings(bridge_dir)
    if failure_mode in uncertain_errors or failure_mode == "reactivated-ack":
        assert not list(bridge_dir.glob("claude-settings-*"))


@pytest.mark.parametrize("reactivated", [False, True], ids=["current-epoch", "reactivated"])
async def test_cli_settings_request_is_released_after_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reactivated: bool
) -> None:
    from omnigent.harnesses.claude_native.settings_generation import (
        acquire_claude_settings_request,
        prune_unused_claude_settings,
        request_claude_bridge_deletion,
    )

    monkeypatch.setattr(claude_native, "resolve_claude_launch", lambda cmd, args: (cmd, args))
    bridge_dir = (
        bridge.prepare_bridge_dir("acknowledged-child", workspace=tmp_path)
        if reactivated
        else tmp_path
    )
    posted = []
    terminal_closers: list[Callable[[], None]] = []
    terminal = Mock()
    terminal.add_resource_cleanup.side_effect = terminal_closers.append
    reactivated_lifecycle: bytes | None = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal reactivated_lifecycle
        if request.method == "GET":
            return httpx.Response(200, json={"claude_settings_request_protocol": 1})
        body = json.loads(request.content)
        posted.append(body)
        generation = body["claude_settings_generation"]
        assert (bridge_dir / f"claude-settings-{generation}.json").exists()
        if reactivated:
            with acquire_claude_settings_request(bridge_dir, generation) as receiver:
                receiver.attach(terminal)
            assert not (bridge_dir / f"claude-settings-{generation}.dispatch.owner").exists()
            request_claude_bridge_deletion(bridge_dir, "acknowledged-child")
            lifecycle_path = bridge_dir / ".claude-settings-lifecycle.json"
            before = json.loads(lifecycle_path.read_bytes())
            assert before["delete_for_session"] == "acknowledged-child"
            bridge.prepare_bridge_dir("acknowledged-child", workspace=tmp_path)
            reactivated_lifecycle = lifecycle_path.read_bytes()
            after = json.loads(reactivated_lifecycle)
            assert after["incarnation"] == before["incarnation"]
            assert after["admission_epoch"] != before["admission_epoch"]
        return httpx.Response(
            200, json={"id": "terminal_claude_main", "claude_settings_request_ack": generation}
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://unused.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            terminal_id = await claude_native._launch_claude_terminal(
                client, "acknowledged-child", (), command="claude", bridge_dir=bridge_dir
            )

        assert terminal_id == "terminal_claude_main"
        assert len(posted) == 1
        if reactivated:
            generation = posted[0]["claude_settings_generation"]
            assert (bridge_dir / f"claude-settings-{generation}.json").exists()
            assert (
                len(list(bridge_dir.glob(f"claude-settings-{generation}.terminal-*.owner"))) == 1
            )
            lifecycle_path = bridge_dir / ".claude-settings-lifecycle.json"
            assert lifecycle_path.read_bytes() == reactivated_lifecycle
        else:
            assert not list(bridge_dir.glob("claude-settings-*"))
    finally:
        for close in terminal_closers:
            close()
    if reactivated:
        assert not list(bridge_dir.glob("*.terminal-*.owner"))
        prune_unused_claude_settings(bridge_dir)
        assert not list(bridge_dir.glob("claude-settings-*"))


@pytest.mark.parametrize("phase", ["create", "dispatch", "acknowledge", "undelivered", "close"])
async def test_cli_settings_ownership_drains_workers_after_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    from omnigent.harnesses.claude_native.settings_generation import (
        ClaudeSettingsRequest,
        acquire_claude_settings_request,
        prune_unused_claude_settings,
    )

    monkeypatch.setattr(claude_native, "resolve_claude_launch", lambda cmd, args: (cmd, args))
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.diagnostics.ClaudeDebugLogFollower", Mock()
    )
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    launch_context = ContextVar("settings_launch", default=False)
    worker_observations: list[tuple[int, bool]] = []
    requests: list[ClaudeSettingsRequest] = []
    descriptors: list[int] = []
    posted: list[dict[str, object]] = []
    create = ClaudeSettingsRequest.create
    close = ClaudeSettingsRequest.close

    def pause(name: str) -> None:
        worker_observations.append((threading.get_ident(), launch_context.get()))
        if name == phase:
            loop.call_soon_threadsafe(started.set)
            if threading.get_ident() != loop_thread and not release.wait(timeout=5):
                raise TimeoutError("settings ownership worker was not released")

    def create_request(cls, bridge_dir: Path) -> ClaudeSettingsRequest:
        request = create(bridge_dir)
        requests.append(request)
        descriptors.extend(
            fd
            for fd in (request._request_fd, request._lease._fd, request._lease._bridge_fd)
            if fd is not None
        )
        pause("create")
        return request

    monkeypatch.setattr(ClaudeSettingsRequest, "create", classmethod(create_request))
    for name, method_name in (
        ("dispatch", "mark_dispatched"),
        ("acknowledge", "acknowledge"),
        ("undelivered", "cancel_undelivered_dispatch"),
        ("close", "close"),
    ):
        method = getattr(ClaudeSettingsRequest, method_name)

        def observed(self, *args, name=name, method=method):
            if name == "close":
                pause(name)
            result = method(self, *args)
            if name != "close":
                pause(name)
            return result

        monkeypatch.setattr(ClaudeSettingsRequest, method_name, observed)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"claude_settings_request_protocol": 1})
        body = json.loads(request.content)
        posted.append(body)
        if phase == "undelivered":
            raise httpx.ConnectError("request was not delivered", request=request)
        generation = body["claude_settings_request"]
        with acquire_claude_settings_request(tmp_path, generation):
            return httpx.Response(
                200,
                json={"id": "terminal_claude_main", "claude_settings_request_ack": generation},
            )

    async def launch(client: httpx.AsyncClient) -> None:
        launch_context.set(True)
        await claude_native._launch_claude_terminal(
            client, "cancelled-child", (), command="claude", bridge_dir=tmp_path
        )

    async with httpx.AsyncClient(
        base_url="http://unused.invalid", transport=httpx.MockTransport(respond)
    ) as client:
        task = asyncio.create_task(launch(client))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            assert requests
            baseline = tmp_path / f"claude-settings-{requests[0].generation}.json"
            baseline_bytes = baseline.read_bytes() if baseline.exists() else None
            for _attempt in range(3):
                task.cancel()
                checkpoint = loop.create_future()
                loop.call_soon(checkpoint.set_result, None)
                await checkpoint
                assert not task.done()
                assert requests[0]._request_fd is not None
                if phase == "dispatch":
                    assert baseline.read_bytes() == baseline_bytes
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert all(
                thread != loop_thread and context for thread, context in worker_observations
            )
            assert requests[0]._request_fd is None
            assert requests[0]._lease._fd is None
            assert requests[0]._lease._bridge_fd is None
            for fd in descriptors:
                with pytest.raises(OSError):
                    os.fstat(fd)
            assert bool(posted) is (phase not in {"create", "dispatch"})
            assert not list(tmp_path.glob("claude-settings-*"))
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            for request in requests:
                close(request)
            prune_unused_claude_settings(tmp_path)


async def test_cli_logs_launch_error_before_cancellation_during_diagnostic_drain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    monkeypatch.setattr(claude_native, "resolve_claude_launch", lambda cmd, args: (cmd, args))
    logger = logging.getLogger(f"{__name__}.launch_failure")
    caplog.set_level(logging.ERROR, logger=logger.name)
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(claude_native, "_logger", logger)
    loop = asyncio.get_running_loop()
    draining = asyncio.Event()
    drained = asyncio.Event()
    release = threading.Event()

    def close(_session_id: str) -> None:
        loop.call_soon_threadsafe(draining.set)
        if not release.wait(timeout=30):
            raise TimeoutError("test did not release diagnostic drain")
        loop.call_soon_threadsafe(drained.set)

    follower = SimpleNamespace(close=Mock(side_effect=close))
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.diagnostics.ClaudeDebugLogFollower",
        lambda _path: follower,
    )
    original_error = httpx.ConnectError("simulated launch transport failure")

    def launch_failure(_request: httpx.Request) -> httpx.Response:
        raise original_error

    async with httpx.AsyncClient(
        base_url="http://unused.invalid", transport=httpx.MockTransport(launch_failure)
    ) as client:
        task = asyncio.create_task(
            claude_native._launch_claude_terminal(
                client, "failed-child", (), command="claude", bridge_dir=tmp_path
            )
        )
        try:
            await asyncio.wait_for(draining.wait(), timeout=10)
            records = [
                record
                for record in caplog.records
                if record.getMessage() == "Claude terminal launch failed: session=failed-child"
            ]
            assert len(records) == 1
            assert records[0].exc_info is not None
            assert records[0].exc_info[1] is original_error
            assert records[0].session_id == "failed-child"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await asyncio.wait_for(drained.wait(), timeout=10)

    follower.close.assert_called_once_with("failed-child")


@pytest.mark.parametrize("failure_point", ["session_metadata", "poll", "close"])
async def test_diagnostic_failures_preserve_the_original_forwarder_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    follower = SimpleNamespace(poll=Mock(), close=Mock(), health_snapshot=dict)
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


@pytest.mark.parametrize("cancel_again", [False, True])
async def test_blocked_diagnostic_io_leaves_loop_responsive_and_drains_after_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancel_again: bool
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    polling = asyncio.Event()
    closed = asyncio.Event()
    release = threading.Event()
    order: list[str] = []
    worker_threads: list[int] = []

    def poll(_session_id: str) -> None:
        worker_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(polling.set)
        if not release.wait(timeout=3):
            raise TimeoutError("test did not release diagnostic I/O")
        order.append("poll")

    def close(_session_id: str) -> None:
        worker_threads.append(threading.get_ident())
        order.append("close")
        loop.call_soon_threadsafe(closed.set)

    follower = SimpleNamespace(
        poll=Mock(side_effect=poll), close=Mock(side_effect=close), health_snapshot=dict
    )
    monkeypatch.setattr(forwarder, "ClaudeDebugLogFollower", lambda _path: follower)
    monkeypatch.setattr(forwarder, "read_active_session_id", lambda _path: "active-session")

    async def forward() -> None:
        async with forwarder._forward_claude_diagnostics(tmp_path, "original-session", 60):
            await asyncio.Event().wait()

    task = asyncio.create_task(forward())
    try:
        await asyncio.wait_for(polling.wait(), timeout=1)
        assert not release.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        follower.close.assert_not_called()
        if cancel_again:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            follower.close.assert_not_called()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), timeout=1)
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert order == ["poll", "close"]
    assert all(thread != loop_thread for thread in worker_threads)
    follower.close.assert_called_once_with("active-session")


async def test_direct_collector_cancellation_serializes_close_and_preserves_original_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    loop = asyncio.get_running_loop()
    polling = asyncio.Event()
    release = threading.Event()
    order: list[str] = []

    def poll(_session_id: str) -> None:
        loop.call_soon_threadsafe(polling.set)
        if not release.wait(timeout=3):
            raise TimeoutError("test did not release diagnostic I/O")
        order.append("poll")

    follower = SimpleNamespace(
        poll=Mock(side_effect=poll),
        close=Mock(side_effect=lambda _session: order.append("close")),
        health_snapshot=dict,
    )
    monkeypatch.setattr(forwarder, "ClaudeDebugLogFollower", lambda _path: follower)
    original_error = RuntimeError("forwarding failed after diagnostic cancellation")

    with pytest.raises(RuntimeError) as raised:
        async with forwarder._forward_claude_diagnostics(tmp_path, "cancel-inner", 60):
            try:
                await asyncio.wait_for(polling.wait(), timeout=1)
                collector = next(
                    task
                    for task in asyncio.all_tasks()
                    if task.get_name() == "claude-diagnostics-cancel-inner"
                )
                collector.cancel()
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(collector), timeout=0.05)
                follower.close.assert_not_called()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await collector
            raise original_error

    assert raised.value is original_error
    assert order == ["poll", "close"]
    follower.close.assert_called_once_with("cancel-inner")
