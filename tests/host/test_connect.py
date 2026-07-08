"""Tests for the host connect process (launch/stop handling)."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus, InvalidURI
from websockets.http11 import Response

from omnigent.host.connect import (
    HostConnectError,
    HostProcess,
    _build_runner_env,
    _RunnerHandle,
    run_host_process,
)
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE,
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostListDirFrame,
    HostListDirResultFrame,
    HostRunnerExitedFrame,
    HostStatFrame,
    HostStatResultFrame,
    HostStopRunnerFrame,
    HostStopRunnerResultFrame,
    decode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.runner.identity import (
    RUNNER_ID_ENV_VAR,
    RUNNER_PARENT_PID_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_WORKSPACE_ENV_VAR,
    token_bound_runner_id,
)

pytestmark = pytest.mark.asyncio


def _make_host_process() -> HostProcess:
    """Create a HostProcess with a test identity.

    :returns: A :class:`HostProcess` for testing.
    """
    identity = HostIdentity(
        host_id="host_test_connect",
        name="test-laptop",
    )
    return HostProcess(
        identity=identity,
        server_url="http://localhost:8000",
    )


def _cleanup_host(host: HostProcess) -> None:
    """Terminate a test host's spawned runners and exit watchers.

    ``_cleanup_runners`` pops every handle before terminating, so the
    watcher tasks see an intentional stop and never report; cancelling
    them afterwards just avoids "task was destroyed but pending"
    warnings at loop teardown.

    :param host: The host process under test.
    """
    host._cleanup_runners()
    for task in host._watcher_tasks:
        task.cancel()


async def test_handle_launch_spawns_subprocess(
    tmp_path: Path,
) -> None:
    """
    Verify that _handle_launch spawns a subprocess with the correct
    binding token and workspace, and returns status='launched'.

    If the result status is not 'launched', the subprocess spawn
    failed or the result frame construction is wrong.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    frame = HostLaunchRunnerFrame(
        request_id="req_001",
        binding_token="test_token_abc",
        workspace=str(workspace),
    )

    spawned_env: dict[str, str] = {}
    spawned_kwargs: dict[str, object] = {}

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Capture the env vars and spawn a no-op process.

        :param args: Command args (ignored — we spawn a real sleep).
        :param kwargs: Popen kwargs including env and stdin.
        :returns: A real subprocess handle.
        """
        env = kwargs.get("env", {})
        spawned_env.update(env)
        spawned_kwargs.update(kwargs)
        # Spawn a real process that sleeps briefly so poll() returns None.
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert isinstance(result, HostLaunchRunnerResultFrame)
    # Status should be 'launched' since the process started.
    assert result.status == "launched", (
        f"Expected 'launched', got {result.status!r}. Error: {result.error}"
    )
    assert result.request_id == "req_001"
    expected_runner_id = token_bound_runner_id("test_token_abc")
    # runner_id should be derived from the binding token.
    assert result.runner_id == expected_runner_id, (
        "runner_id should be token_bound_runner_id(binding_token)"
    )

    # Verify env vars passed to the subprocess.
    assert spawned_env.get("RUNNER_SERVER_URL") == "http://localhost:8000"
    assert spawned_env.get("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN") == "test_token_abc"
    assert spawned_env.get("OMNIGENT_RUNNER_WORKSPACE") == str(workspace)

    # Runners must get a clean /dev/null stdin, not the daemon's inherited fd:
    # a long-lived (e.g. nohup'd) daemon can end up with a closed/recycled
    # stdin, and an inherited bad fd crashes the runner at interpreter startup
    # ("init_sys_streams: Bad file descriptor") so it never connects. If this
    # regresses, long-lived daemons intermittently fail to start sessions.
    assert spawned_kwargs.get("stdin") == subprocess.DEVNULL, (
        "runner subprocess must be spawned with stdin=subprocess.DEVNULL"
    )

    # Clean up the spawned sleep process (and its exit watcher).
    _cleanup_host(host)


async def test_handle_launch_fails_for_bad_workspace() -> None:
    """
    Verify that _handle_launch returns status='failed' when the
    workspace path does not exist.

    If it returns 'launched', the path validation is missing and
    the runner would start in a nonexistent directory.
    """
    host = _make_host_process()
    frame = HostLaunchRunnerFrame(
        request_id="req_002",
        binding_token="token_xyz",
        workspace="/nonexistent/path/that/does/not/exist",
    )

    result = await host._handle_launch(frame)

    assert isinstance(result, HostLaunchRunnerResultFrame)
    assert result.status == "failed", "Should fail for nonexistent workspace"
    assert "does not exist" in (result.error or ""), (
        f"Error should mention path doesn't exist, got: {result.error!r}"
    )
    assert result.runner_id is None


async def test_handle_launch_refuses_unconfigured_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify _handle_launch refuses to spawn when the frame's harness is
    not configured, with the structured error_code and a message that
    names the harness, the host, and the `omnigent setup` fix.

    If this regresses, an unconfigured launch spawns a runner whose
    first turn dies inside the executor — the exact dead-session UX
    this check exists to prevent — and the server's 412 mapping (keyed
    on error_code) never fires.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()
    # Patch the symbol connect.py imported, with the real function's
    # signature; the workspace exists so ONLY the harness check can fail.
    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        lambda harness: False,
    )

    frame = HostLaunchRunnerFrame(
        request_id="req_unconfigured",
        binding_token="token_abc",
        workspace=str(workspace),
        harness="codex",
    )
    result = await host._handle_launch(frame)

    assert isinstance(result, HostLaunchRunnerResultFrame)
    assert result.status == "failed"
    # The structured code is what the server's 412 mapping keys on.
    assert result.error_code == HARNESS_NOT_CONFIGURED_ERROR_CODE
    # The message is shown verbatim to the user — it must name the
    # harness, the host, and the remediation command.
    assert "'codex'" in (result.error or "")
    assert "test-laptop" in (result.error or "")
    assert "omnigent setup" in (result.error or "")
    assert result.runner_id is None
    # No runner subprocess may exist after a refusal.
    assert host._runners == {}


async def test_handle_launch_native_cursor_message_points_at_cursor_installer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A native-Cursor refusal must name the ``cursor-agent`` installer and
    login, not ``omnigent setup`` — which only configures the SDK ``cursor``
    harness and never installs the ``cursor-agent`` CLI ``omni cursor`` boots.

    Here ``harness_setup_hint`` is the real function (only the readiness check
    is forced False), so this exercises the connect.py → hint wiring end to end.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        lambda harness: False,
    )

    frame = HostLaunchRunnerFrame(
        request_id="req_cursor_native",
        binding_token="token_abc",
        workspace=str(workspace),
        harness="cursor-native",
    )
    result = await host._handle_launch(frame)

    assert result.status == "failed"
    assert result.error_code == HARNESS_NOT_CONFIGURED_ERROR_CODE
    message = result.error or ""
    assert "'cursor-native'" in message
    assert "test-laptop" in message
    assert "cursor.com/install" in message
    assert "cursor-agent login" in message
    assert "omnigent setup" not in message
    assert host._runners == {}


async def test_handle_launch_configured_harness_proceeds_to_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify a configured harness passes the check and the launch
    proceeds to the normal spawn path.

    If this fails with status='failed', the readiness check is
    refusing configured harnesses — every launch on the host would
    break, not just unconfigured ones.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        lambda harness: True,
    )

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a no-op process so poll() returns None.

        :param args: Command args (ignored — we spawn a real sleep).
        :param kwargs: Popen kwargs (ignored).
        :returns: A real subprocess handle.
        """
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    frame = HostLaunchRunnerFrame(
        request_id="req_configured",
        binding_token="token_def",
        workspace=str(workspace),
        harness="claude-sdk",
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert result.status == "launched", (
        f"Configured harness must launch, got {result.status!r}: {result.error}"
    )
    assert result.error_code is None

    # Clean up the spawned sleep process (and its exit watcher).
    _cleanup_host(host)


async def test_handle_launch_without_harness_skips_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify a launch frame with harness=None (an older server) never
    consults the readiness check — fail open across version skew.

    If the check ran anyway, upgrading hosts before servers would
    break launches for any session whose harness the host considers
    unconfigured, even though the server never asked for the check.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    def _must_not_be_called(harness: str) -> bool:
        """Fail the test if the readiness check runs for harness=None.

        :param harness: The harness the production code passed.
        :returns: Never returns.
        """
        raise AssertionError("harness_is_configured must not be called when frame.harness is None")

    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        _must_not_be_called,
    )

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a no-op process so poll() returns None.

        :param args: Command args (ignored — we spawn a real sleep).
        :param kwargs: Popen kwargs (ignored).
        :returns: A real subprocess handle.
        """
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    frame = HostLaunchRunnerFrame(
        request_id="req_no_harness",
        binding_token="token_ghi",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert result.status == "launched"

    # Clean up the spawned sleep process (and its exit watcher).
    _cleanup_host(host)


async def test_handle_launch_prints_exact_runner_log_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The launch banner names the exact runner log file, not just the dir.

    A foreground host's terminal shows the lifecycle line, but the runner's
    real output — the agent turn, tracebacks — lands only in the per-runner
    log file. The user needs that precise path to tail it, so the launch
    print must include it. We repoint ``Path.home`` so the log lands under
    tmp (no write to the developer's real ``~/.omnigent``).
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    frame = HostLaunchRunnerFrame(
        request_id="req_log",
        binding_token="tok_log",
        workspace=str(workspace),
        session_id="conv_log",
    )

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a harmless sleep so poll() reports the runner as alive.

        :param args: Command args (ignored — a real sleep is spawned).
        :param kwargs: Popen kwargs (ignored).
        :returns: A real subprocess handle.
        """
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert result.status == "launched", result.error
    # Exactly one runner-*.log was created under the host-runner dir.
    runner_log_dir = tmp_path / ".omnigent" / "logs" / "host-runner"
    log_files = list(runner_log_dir.glob("runner-*.log"))
    assert len(log_files) == 1
    out = capsys.readouterr().out
    assert "↑ Runner started:" in out
    # The exact file path is printed, home-collapsed to ``~`` for readability.
    assert f"log: ~/.omnigent/logs/host-runner/{log_files[0].name}" in out
    assert "session: conv_log" in out

    _cleanup_host(host)


class _FakeTunnel:
    """In-memory stand-in for the host's WebSocket tunnel connection.

    Captures frames sent by the host process so tests can assert on
    outbound reports; ``recv`` immediately raises so ``_serve_frames``
    exits after its post-hello work.

    :param sent: Encoded frames sent via ``send``, in order.
    """

    def __init__(self) -> None:
        """Initialize with an empty sent-frame log."""
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        """Record an outbound frame.

        :param data: Encoded frame text.
        """
        self.sent.append(data)

    async def recv(self) -> str:
        """Simulate an immediate disconnect.

        :returns: Never returns.
        :raises ConnectionError: Always — ends the serve loop.
        """
        raise ConnectionError("test disconnect")


async def test_handle_launch_immediate_exit_reports_exit_code_and_log_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An immediate runner death fails the launch with the actual cause.

    Before this, the daemon answered only "runner exited immediately
    with code N" — the real error (a traceback, a missing module)
    stayed in a log file on the host. The launch failure must now
    carry the exit code, the log path, and the log tail so the server
    can surface the cause to the user verbatim.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a process that writes to the real captured log and dies.

        Passes through the production-opened log handles so the error
        output lands exactly where the daemon will read the tail from,
        and waits for exit so ``poll()`` reports the death immediately.

        :param args: Command args (ignored — a failing sh is spawned).
        :param kwargs: Popen kwargs from production, including the log
            file handles.
        :returns: A finished subprocess handle with returncode 7.
        """
        proc = original_popen(
            ["sh", "-c", "echo 'RuntimeError: boom-traceback' >&2; exit 7"],
            stdin=subprocess.DEVNULL,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )
        proc.wait()
        return proc

    frame = HostLaunchRunnerFrame(
        request_id="req_dead",
        binding_token="tok_dead",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert result.status == "failed"
    error = result.error or ""
    # The exit code identifies the failure class without log-reading.
    assert "code 7" in error
    # The log path lets the user fetch the full log on the host.
    assert "~/.omnigent/logs/host-runner/runner-" in error
    # The tail carries the actual cause — the whole point of the report.
    assert "RuntimeError: boom-traceback" in error


async def test_watch_runner_reports_unexpected_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that dies after launch is reported via host.runner_exited.

    This is the launch-succeeded-then-crashed path (auth rejection, bad
    env, import error) — the daemon already told the server "launched",
    so only the watcher can carry the cause. Without the report, the
    client polls its full timeout and the user is sent to a log
    directory on the host.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr("omnigent.host.connect._RUNNER_WATCH_INTERVAL_S", 0.01)
    host = _make_host_process()
    tunnel = _FakeTunnel()
    host._ws = tunnel  # type: ignore[assignment] — duck-typed send
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a runner that lives briefly, logs a cause, then dies.

        The initial sleep keeps ``poll()`` None at launch time so the
        launch itself succeeds; the process then writes the cause to
        the production log handles and exits 3.

        :param args: Command args (ignored).
        :param kwargs: Popen kwargs from production, including the log
            file handles.
        :returns: A live subprocess handle.
        """
        return original_popen(
            ["sh", "-c", "echo 'tunnel rejected: crash-cause' >&2; sleep 0.2; exit 3"],
            stdin=subprocess.DEVNULL,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )

    frame = HostLaunchRunnerFrame(
        request_id="req_watch",
        binding_token="tok_watch",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)
    assert result.status == "launched", result.error

    # Wait for the watcher to observe the death and send its report
    # (bounded — a hang means the watcher never fired).
    await asyncio.wait_for(asyncio.gather(*host._watcher_tasks), timeout=5.0)

    # Exactly one report; a second would double-record server-side.
    assert len(tunnel.sent) == 1
    report = decode_host_frame(tunnel.sent[0])
    assert isinstance(report, HostRunnerExitedFrame)
    assert report.runner_id == token_bound_runner_id("tok_watch")
    # The report carries the exit code and the log tail with the cause.
    assert "code 3" in report.error
    assert "tunnel rejected: crash-cause" in report.error


async def test_watch_runner_silent_on_intentional_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host.stop_runner termination is NOT reported as a crash.

    ``_handle_stop`` pops the handle before terminating; the watcher
    must read that as intentional and send nothing. A false report
    here would attach a scary "runner process exited" error to every
    cleanly stopped session.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr("omnigent.host.connect._RUNNER_WATCH_INTERVAL_S", 0.01)
    host = _make_host_process()
    tunnel = _FakeTunnel()
    host._ws = tunnel  # type: ignore[assignment] — duck-typed send
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a long-lived stand-in runner.

        :param args: Command args (ignored).
        :param kwargs: Popen kwargs (ignored beyond stdio defaults).
        :returns: A live subprocess handle.
        """
        return original_popen(
            ["sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    launch = HostLaunchRunnerFrame(
        request_id="req_stop",
        binding_token="tok_stop",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(launch)
    assert result.status == "launched", result.error

    runner_id = token_bound_runner_id("tok_stop")
    stop_result = host._handle_stop(
        HostStopRunnerFrame(request_id="req_stop_2", runner_id=runner_id)
    )
    assert stop_result.status == "stopped"

    # Let the watcher observe the (intentional) death and finish.
    await asyncio.wait_for(asyncio.gather(*host._watcher_tasks), timeout=5.0)

    # No runner_exited report and nothing parked for a reconnect —
    # either would mark a clean stop as a crash.
    assert tunnel.sent == []
    assert host._unreported_exits == {}


async def test_unreported_exit_flushes_after_reconnect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An exit report that raced a disconnect is sent after the next hello.

    A runner can die while the host tunnel is down (server restart,
    network blip). The report parks in ``_unreported_exits`` and must
    flush right after the reconnect hello — otherwise the death is
    never reported and the waiting client polls to its timeout.
    """
    host = _make_host_process()
    host._unreported_exits["runner_parked"] = "runner process exited with code 1"
    tunnel = _FakeTunnel()

    # _serve_frames sends hello, flushes parked reports, then hits the
    # fake recv's immediate disconnect.
    with pytest.raises(ConnectionError, match="test disconnect"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type] — duck-typed ws

    assert len(tunnel.sent) == 2
    hello = decode_host_frame(tunnel.sent[0])
    assert isinstance(hello, HostHelloFrame)
    report = decode_host_frame(tunnel.sent[1])
    assert isinstance(report, HostRunnerExitedFrame)
    assert report.runner_id == "runner_parked"
    assert report.error == "runner process exited with code 1"
    # The queue drained — a retained entry would re-send on every
    # reconnect forever.
    assert host._unreported_exits == {}


async def test_hello_advertises_installed_version() -> None:
    """The ``host.hello`` frame reports the omnigent version, not a placeholder.

    A hard-coded placeholder would make every host look like the same
    stale build in the server's version popover. The hello must carry the
    shared resolved version this host is actually running.
    """
    from omnigent.version import VERSION

    host = _make_host_process()
    tunnel = _FakeTunnel()

    with pytest.raises(ConnectionError, match="test disconnect"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type] — duck-typed ws

    hello = decode_host_frame(tunnel.sent[0])
    assert isinstance(hello, HostHelloFrame)
    assert hello.version == VERSION
    # Guard against the old hard-coded literal creeping back.
    assert hello.version != "0.1.0"


def test_handle_stop_terminates_process(tmp_path: Path) -> None:
    """
    Verify that _handle_stop terminates a tracked runner and
    returns status='stopped'.

    If the process isn't terminated, the runner keeps consuming
    resources after the server requested a stop.
    """
    host = _make_host_process()
    proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    host._runners["runner_aaa"] = _RunnerHandle(proc=proc, log_path=tmp_path / "runner-a.log")

    frame = HostStopRunnerFrame(
        request_id="req_003",
        runner_id="runner_aaa",
    )
    result = host._handle_stop(frame)

    assert isinstance(result, HostStopRunnerResultFrame)
    assert result.status == "stopped"
    # Process should be terminated.
    assert proc.poll() is not None, "Runner process should be terminated after stop"
    # Runner should be removed from tracking.
    assert "runner_aaa" not in host._runners


def test_handle_stop_unknown_runner() -> None:
    """
    Verify that _handle_stop returns status='failed' for an
    unknown runner_id.

    If it returns 'stopped', the stop is silently succeeding for
    a runner that doesn't exist, which could mask bugs.
    """
    host = _make_host_process()
    frame = HostStopRunnerFrame(
        request_id="req_004",
        runner_id="runner_nonexistent",
    )
    result = host._handle_stop(frame)

    assert isinstance(result, HostStopRunnerResultFrame)
    assert result.status == "failed"
    assert "unknown runner" in (result.error or "")


def test_alive_runner_ids_cleans_dead(tmp_path: Path) -> None:
    """
    Verify that _alive_runner_ids removes dead processes and returns
    only alive ones.

    If dead runners persist, reconnect reconciliation would report
    runners that are no longer running.
    """
    host = _make_host_process()

    alive_proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dead_proc = subprocess.Popen(
        ["true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dead_proc.wait()

    host._runners["runner_alive"] = _RunnerHandle(
        proc=alive_proc, log_path=tmp_path / "runner-alive.log"
    )
    host._runners["runner_dead"] = _RunnerHandle(
        proc=dead_proc, log_path=tmp_path / "runner-dead.log"
    )

    alive_ids = host._alive_runner_ids()

    assert "runner_alive" in alive_ids
    assert "runner_dead" not in alive_ids, "Dead runner should be cleaned up"
    assert "runner_dead" not in host._runners, "Dead runner should be removed from tracking dict"

    alive_proc.terminate()
    alive_proc.wait()


def test_cleanup_runners_terminates_all(tmp_path: Path) -> None:
    """
    Verify that _cleanup_runners terminates every tracked runner.

    This is the graceful shutdown path (Ctrl-C / finally block).
    If any runner survives, the host leaves orphaned processes.
    """
    host = _make_host_process()
    procs = []
    for name in ("runner_a", "runner_b", "runner_c"):
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        host._runners[name] = _RunnerHandle(proc=proc, log_path=tmp_path / f"{name}.log")
        procs.append(proc)

    host._cleanup_runners()

    # All processes should be terminated.
    for proc in procs:
        assert proc.poll() is not None, f"Runner pid={proc.pid} should be dead after cleanup"
    # Tracking dict should be empty.
    assert host._runners == {}


def test_reap_orphans_reaps_orphaned_children(tmp_path: Path) -> None:
    """Regression for #1782: orphaned children are reaped, not leaked.

    In production the leak is a harness tool subprocess (node/chromium/tmux)
    whose runner parent died: it is orphaned and reparented to the host
    (PID 1 in a container, or a subreaper). Once reparented it is a *direct*
    child of the host that nothing ``wait()``s — so it lingers as a
    ``<defunct>`` zombie forever. This test models that end state directly by
    forking a child the host does not track and never waits: ``os.fork`` is
    portable (no ``PR_SET_CHILD_SUBREAPER``, which macOS lacks), and a
    reparented orphan is indistinguishable from a plain unwaited child to the
    reaper. ``_reap_orphans_once`` must drain it.
    """
    import errno
    import os

    host = _make_host_process()

    # Fork a bare child (NOT a tracked runner, NOT wrapped in Popen) that
    # exits immediately — the faithful model of an orphan reparented to us.
    pid = os.fork()
    if pid == 0:  # pragma: no cover — child leg never returns to pytest
        os._exit(0)

    # Let it exit and become a zombie parented to this process.
    deadline = time.monotonic() + 5.0
    reaped_total = 0
    while time.monotonic() < deadline:
        reaped_total += host._reap_orphans_once()
        if reaped_total >= 1:
            break
        time.sleep(0.05)

    assert reaped_total >= 1, "orphaned child was not reaped (zombie leak — #1782)"
    # It is truly reaped: a direct waitpid now raises ECHILD (no such child).
    with pytest.raises(OSError) as exc_info:
        os.waitpid(pid, 0)
    assert exc_info.value.errno == errno.ECHILD
    # A second sweep with no orphans left is a clean no-op.
    assert host._reap_orphans_once() == 0


def test_reap_orphans_never_steals_tracked_runner_exit_code(tmp_path: Path) -> None:
    """The reaper must not consume a tracked runner's exit status (#1782).

    A naive ``waitpid(-1)`` reaper would reap a just-exited tracked runner
    behind ``Popen``'s back, making ``_watch_runner``'s ``poll()`` report a
    bogus exit 0 for a crash. ``_reap_orphans_once`` peeks with ``WNOWAIT``
    and skips tracked pids, so the runner's real exit code survives for the
    ``host.runner_exited`` report.
    """
    host = _make_host_process()

    # A tracked runner that exits non-zero (a "crash").
    runner = subprocess.Popen(["python3", "-c", "import sys; sys.exit(42)"])
    host._runners["runner_crash"] = _RunnerHandle(
        proc=runner, log_path=tmp_path / "runner-crash.log"
    )
    # Wait until the OS reports it as exited (zombie), WITHOUT Popen.wait().
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and runner.poll() is None:
        # poll() would itself reap; instead peek via the reaper repeatedly.
        host._reap_orphans_once()
        time.sleep(0.05)

    # The reaper ran while the crashed runner was reapable; it must NOT have
    # stolen it. Popen must still see the true exit code.
    assert runner.poll() == 42, "reaper corrupted the tracked runner's exit code"

    runner.wait()


def test_reaper_does_not_steal_host_owned_subprocess_exit_code(tmp_path: Path) -> None:
    """The reaper must not reap a host-owned subprocess's child (#1782).

    Regression for the Polly-flagged race: the host also spawns *direct*
    children that are not tracked runners — the ``git`` commands in
    :mod:`omnigent.host.git_worktree`, run via ``subprocess.run`` under
    ``asyncio.to_thread`` from the worktree handlers. If the 2s reaper sweep
    fires after such a git child has exited but before ``subprocess``'s own
    ``wait()`` collects it, a blind reaper would ``waitpid`` it — and CPython
    then swallows the resulting ``ECHILD`` and reports ``returncode == 0``,
    silently turning a *failed* ``git worktree`` op into a success.

    ``_host_subprocess_op`` pauses the reaper for exactly that window. Here a
    ``sh -c 'exit 42'`` stands in for a failing git command: while the op is
    marked in flight, ``_reap_orphans_once`` must be a no-op and must NOT
    consume the child, so the owner still reads the true exit code 42.
    """
    host = _make_host_process()

    # A host-owned subprocess (git stand-in) that FAILS with a distinctive
    # code. NOT a tracked runner — indistinguishable from an orphan to a naive
    # reaper.
    proc = subprocess.Popen(["sh", "-c", "exit 42"])
    # Let it exit so it is reapable (the dangerous window subprocess.run has
    # between the child exiting and its internal wait()).
    time.sleep(0.3)

    with host._host_subprocess_op():
        # Every sweep during the op must be a no-op — the reaper is paused.
        for _ in range(5):
            assert host._reap_orphans_once() == 0, "reaper ran during a host-owned op"
            time.sleep(0.02)

    # The owner still collects its child's TRUE exit code — not corrupted to 0.
    assert proc.poll() == 42, "reaper stole the host-owned subprocess's exit code (#1782)"
    proc.wait()


def test_host_subprocess_op_guard_is_reentrant_and_balanced(tmp_path: Path) -> None:
    """``_host_subprocess_op`` nests correctly and always rebalances (#1782).

    The reaper resumes only when the counter returns to 0, and the decrement
    must survive an exception in the guarded body (``finally``), or a raising
    worktree op would wedge the reaper off permanently.
    """
    host = _make_host_process()
    assert host._owned_subprocess_ops == 0

    with host._host_subprocess_op():
        assert host._owned_subprocess_ops == 1
        with host._host_subprocess_op():  # re-entrant
            assert host._owned_subprocess_ops == 2
        assert host._owned_subprocess_ops == 1
    assert host._owned_subprocess_ops == 0

    # An exception inside the guarded body must still rebalance the counter.
    with pytest.raises(RuntimeError):
        with host._host_subprocess_op():
            assert host._owned_subprocess_ops == 1
            raise RuntimeError("worktree op blew up")
    assert host._owned_subprocess_ops == 0, "guard leaked a ref on exception — reaper wedged"


def test_install_child_subreaper_is_safe_to_call() -> None:
    """``_install_child_subreaper`` never raises and reports a bool.

    ``True`` on Linux where ``prctl`` set the bit; ``False`` on non-Linux or
    when ``prctl`` is unavailable — both are acceptable, non-fatal outcomes.
    """
    import sys

    from omnigent.host.connect import _install_child_subreaper

    result = _install_child_subreaper()
    assert isinstance(result, bool)
    if sys.platform != "linux":
        assert result is False


def test_host_spawned_runner_has_parent_pid_env(
    tmp_path: Path,
) -> None:
    """
    Verify that runners spawned by the host have
    OMNIGENT_RUNNER_PARENT_PID set to the host's PID.

    The runner's parent-PID watchdog uses this to auto-exit when
    the host dies. If the env var is missing or wrong, runners
    would become orphans on hard kill.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    frame = HostLaunchRunnerFrame(
        request_id="req_pid",
        binding_token="token_for_pid_test",
        workspace=str(workspace),
    )

    import os

    spawned_env: dict[str, str] = {}
    original_popen = subprocess.Popen

    def _capture_env(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Capture env vars from the subprocess spawn.

        :param args: Command args.
        :param kwargs: Popen kwargs including env.
        :returns: A real subprocess.
        """
        env = kwargs.get("env", {})
        spawned_env.update(env)
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_capture_env):
        import asyncio

        result = asyncio.run(host._handle_launch(frame))

    assert result.status == "launched"
    # RUNNER_PARENT_PID should be the host's own PID.
    parent_pid = spawned_env.get("OMNIGENT_RUNNER_PARENT_PID")
    assert parent_pid == str(os.getpid()), (
        f"Expected RUNNER_PARENT_PID={os.getpid()}, got {parent_pid}. "
        "Without this, the runner watchdog can't detect host death."
    )

    # Clean up.
    _cleanup_host(host)


# ── host.stat handler ───────────────────────────────────


def test_handle_stat_returns_directory_for_existing_dir(
    tmp_path: Path,
) -> None:
    """
    Verify that ``_handle_stat`` reports ``exists: true,
    type: "directory"`` for an existing directory and returns its
    realpath as ``canonical_path``.

    These three fields drive the server-side validation contract
    (path exists, is a directory, canonical_path is what gets
    stored). If any one is wrong, every host-launched session
    would either be rejected or land in the wrong directory.
    """
    host = _make_host_process()
    target = tmp_path / "project"
    target.mkdir()

    result = host._handle_stat(HostStatFrame(request_id="r_dir", path=str(target)))

    assert isinstance(result, HostStatResultFrame)
    assert result.status == "ok"
    assert result.exists is True
    assert result.type == "directory"
    # Compare via os.path.realpath because tmp_path on macOS goes
    # through /var → /private/var symlinks; the design says the
    # stored canonical_path is the realpath.
    import os

    assert result.canonical_path == os.path.realpath(target)
    assert result.error is None


def test_handle_stat_returns_file_for_existing_file(
    tmp_path: Path,
) -> None:
    """
    Verify that ``_handle_stat`` reports ``type: "file"`` for a
    regular file. The validator rejects non-directories at session
    create — without ``type``, it would happily store a file path
    as the workspace and the runner would fail on ``os.chdir``.
    """
    host = _make_host_process()
    target = tmp_path / "README.md"
    target.write_text("hi")

    result = host._handle_stat(HostStatFrame(request_id="r_file", path=str(target)))

    assert result.exists is True
    assert result.type == "file"


def test_handle_stat_follows_symlink_to_directory(
    tmp_path: Path,
) -> None:
    """
    Verify symlinks are followed: symlink to a directory returns
    ``type: "directory"`` and ``canonical_path`` is the target's
    realpath (not the symlink itself).

    This is the load-bearing case for the "symlinks cannot smuggle
    a workspace out of the agent's boundary" guarantee. If the
    canonical_path were the symlink path (not the target), a
    ``cwd: ~/foo`` boundary check would pass for ``~/foo/link →
    /etc``, and the runner would end up in /etc.
    """
    host = _make_host_process()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    result = host._handle_stat(HostStatFrame(request_id="r_sym", path=str(link)))

    assert result.exists is True
    assert result.type == "directory"
    import os

    assert result.canonical_path == os.path.realpath(real_dir)


def test_handle_stat_dangling_symlink_returns_not_exists(
    tmp_path: Path,
) -> None:
    """
    Verify a symlink pointing at a non-existent target returns
    ``exists: false``.

    Without this collapse, the runner would later fail on chdir
    with a confusing error and no useful surface for the user.
    The design defines this as part of the "exists/not exists"
    contract — see HostStatResultFrame docstring.
    """
    host = _make_host_process()
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "does_not_exist")

    result = host._handle_stat(HostStatFrame(request_id="r_dangling", path=str(dangling)))

    assert result.status == "ok"
    assert result.exists is False
    assert result.canonical_path is None


def test_handle_stat_missing_path_returns_not_exists(
    tmp_path: Path,
) -> None:
    """
    Verify a missing path returns ``exists: false`` (not status
    "failed"). The design treats non-existence as a normal answer,
    not an error — so the route can return a 400 with a clean
    "doesn't exist" message instead of a 500.
    """
    host = _make_host_process()
    missing = tmp_path / "does_not_exist"

    result = host._handle_stat(HostStatFrame(request_id="r_missing", path=str(missing)))

    assert result.status == "ok"
    assert result.exists is False
    assert result.canonical_path is None
    assert result.error is None


def test_handle_stat_permission_denied_returns_not_exists(
    tmp_path: Path,
) -> None:
    """
    Verify a path the host process can't access (EACCES) collapses
    to ``exists: false`` rather than surfacing the EACCES.

    Per the design: v1 collapses ENOENT and EACCES into
    a single answer for simplicity. If we ever distinguish them,
    the wire shape changes (a ``readable`` field gets added);
    this test pins the v1 contract and would fail loud on a
    silent regression.
    """
    host = _make_host_process()
    locked = tmp_path / "locked"
    locked.mkdir()
    # Set permissions to 0 — even the owner cannot stat children.
    # We then ask for a child path to force EACCES rather than
    # ENOENT (the directory itself is still stat-able by its owner).
    import stat as stat_mod

    locked.chmod(0)
    try:
        child = locked / "child"
        result = host._handle_stat(HostStatFrame(request_id="r_eacces", path=str(child)))
        # Many filesystems / kernels return EACCES from stat
        # on the child of a 0-permission dir; some ultra-permissive
        # setups (e.g. macOS as root, certain CI sandboxes) may
        # short-circuit and return ENOENT. Both collapse to
        # exists:false per the design — so we accept either route.
        assert result.status == "ok"
        assert result.exists is False
    finally:
        # Restore so tmp_path cleanup doesn't fail.
        locked.chmod(stat_mod.S_IRWXU)


def test_handle_stat_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    """
    Verify that ``~`` in the input path is expanded to the host
    process owner's home directory.

    The host (not the server) is the source of truth for ``~`` —
    designs/SESSION_WORKSPACE_SELECTION.md. If the host
    handler skipped expansion, agent specs with ``cwd: ~/foo``
    would never resolve and validation would fail on every host.
    """
    # Point HOME at our tmp_path so ~ resolves predictably.
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "subdir"
    target.mkdir()

    host = _make_host_process()
    result = host._handle_stat(HostStatFrame(request_id="r_tilde", path="~/subdir"))

    import os

    assert result.exists is True
    assert result.type == "directory"
    assert result.canonical_path == os.path.realpath(target)


def test_build_runner_env_allowlists_host_env_and_strips_secrets() -> None:
    """
    A spawned runner inherits only allowlisted host env vars — process
    essentials pass through, the host owner's NON-HARNESS secrets do
    not — plus the runner wiring vars. Harness credentials
    (HARNESS_CREDENTIAL_ENV_VARS) are the deliberate exception: the
    host owner provisions those precisely so runners can use them.
    Guards against unrelated host secrets (cloud creds, workspace
    tokens) leaking into every runner subprocess.
    """
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/alice",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "UTF-8",
        "DATABRICKS_CONFIG_PROFILE": "ambient",
        "DATABRICKS_CONFIG_FILE": "/tmp/databrickscfg",
        "DATABRICKS_AUTH_STORAGE": "plaintext",
        "ANTHROPIC_API_KEY": "sk-harness",
        "IS_SANDBOX": "1",
        "DATABRICKS_TOKEN": "dapi-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "SOME_RANDOM_VAR": "x",
        "OMNIGENT_CLAUDE_SDK_NO_SANDBOX": "1",
        "KUBECONFIG": "/home/alice/.kube/config",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    # Process essentials + the locale family pass through.
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/alice"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_CTYPE"] == "UTF-8"
    # Databricks config selectors are allowlisted ambient passthrough —
    # the ambient value reaches the runner unmodified (no flag override).
    assert env["DATABRICKS_CONFIG_PROFILE"] == "ambient"
    assert env["DATABRICKS_CONFIG_FILE"] == "/tmp/databrickscfg"
    # The token-storage backend selector forwards too — without it the runner
    # falls back to the ~/.databrickscfg default and can read a different token
    # store than the host/daemon, failing to mint a token (runner tunnel 401).
    assert env["DATABRICKS_AUTH_STORAGE"] == "plaintext"
    # Harness credentials forward — they exist FOR the runner's
    # harnesses (laptop: exported keys; managed sandbox: the
    # deployment's injected provider secrets).
    assert env["ANTHROPIC_API_KEY"] == "sk-harness"
    # The sandbox-image environment descriptor forwards: Claude Code
    # needs it to allow --dangerously-skip-permissions under root in
    # sandbox containers. Only the baked host image ever sets it.
    assert env["IS_SANDBOX"] == "1"
    # The claude-sdk sandbox bypass flag forwards — it is read inside the
    # harness, so a bare ``OMNIGENT_CLAUDE_SDK_NO_SANDBOX=1 omnigent run …``
    # must reach the runner without also forcing
    # ``OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_CLAUDE_SDK_NO_SANDBOX``.
    assert env["OMNIGENT_CLAUDE_SDK_NO_SANDBOX"] == "1"
    # KUBECONFIG is a filesystem path (not a secret) — kubectl, helm, k9s
    # need it to resolve the user's cluster contexts and namespaces.
    assert env["KUBECONFIG"] == "/home/alice/.kube/config"
    # CLAUDE_CODE_SKIP_BEDROCK_AUTH disables AWS SigV4 auth for LiteLLM
    # proxies — a non-secret boolean, same rationale as CLAUDE_CODE_USE_BEDROCK.
    assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
    # Non-harness secrets are stripped — the point of the allowlist.
    assert "DATABRICKS_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    # Non-allowlisted vars are dropped (allowlist, not denylist).
    assert "SOME_RANDOM_VAR" not in env
    # Runner wiring is layered on.
    assert env["RUNNER_SERVER_URL"] == "http://server"
    assert env[RUNNER_ID_ENV_VAR] == "runner_abc"
    assert env[RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR] == "tok"
    assert env[RUNNER_WORKSPACE_ENV_VAR] == "/ws"
    assert env[RUNNER_PARENT_PID_ENV_VAR] == "42"


def test_build_runner_env_forwards_harness_credentials_and_endpoints() -> None:
    """
    Every var in HARNESS_CREDENTIAL_ENV_VARS forwards when present —
    keys AND endpoint wiring (base URLs travel with their credentials
    or gateway setups break in confusing ways). Absent vars are simply
    not set rather than defaulted.
    """
    from omnigent.host.connect import HARNESS_CREDENTIAL_ENV_VARS

    base = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "ANTHROPIC_API_KEY": "sk-a",
        "ANTHROPIC_BASE_URL": "https://gateway.example.com/anthropic",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-sub",
        "CODEX_ACCESS_TOKEN": "codex-workspace-token",
        "OPENAI_API_KEY": "sk-o",
        "OPENAI_BASE_URL": "https://gateway.example.com/openai",
        "GEMINI_API_KEY": "g-key",
        "AWS_BEARER_TOKEN_BEDROCK": "absk-fwd",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock-runtime.us-east-1.amazonaws.com",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GEMINI_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_BEDROCK_BASE_URL",
    ):
        # Pins each conventional name into the default set — dropping
        # one breaks that harness's credentials on managed sandboxes.
        assert name in HARNESS_CREDENTIAL_ENV_VARS
        assert env[name] == base[name]
    # ANTHROPIC_AUTH_TOKEN wasn't in base: present in the default set
    # but never invented into the env.
    assert "ANTHROPIC_AUTH_TOKEN" in HARNESS_CREDENTIAL_ENV_VARS
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_build_runner_env_forwards_omnigent_prefixed_harness_credentials() -> None:
    """Prefixed harness credential aliases forward without creating raw names."""
    from omnigent.host.connect import HARNESS_CREDENTIAL_ENV_VARS

    base = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "OMNIGENT_ANTHROPIC_API_KEY": "sk-prefixed",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    assert "OMNIGENT_ANTHROPIC_API_KEY" in HARNESS_CREDENTIAL_ENV_VARS
    assert env["OMNIGENT_ANTHROPIC_API_KEY"] == "sk-prefixed"
    assert "ANTHROPIC_API_KEY" not in env


def test_build_runner_env_passthrough_extends_forwarded_set() -> None:
    """
    OMNIGENT_RUNNER_ENV_PASSTHROUGH names EXTRA vars to forward (for
    `providers:`-config `env:` refs and custom gateway wiring) without
    opening the allowlist to anything unnamed.
    """
    base = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "MY_GATEWAY_TOKEN, MY_GATEWAY_URL",
        "MY_GATEWAY_TOKEN": "tok-123",
        "MY_GATEWAY_URL": "https://llm.internal.example.com",
        "UNLISTED_SECRET": "nope",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    # Named extras forward (whitespace around commas tolerated).
    assert env["MY_GATEWAY_TOKEN"] == "tok-123"
    assert env["MY_GATEWAY_URL"] == "https://llm.internal.example.com"
    # Anything unnamed stays behind the allowlist.
    assert "UNLISTED_SECRET" not in env


def test_build_runner_env_preserves_ambient_databricks_profile() -> None:
    """
    Ambient Databricks profile/config-file selectors reach host runners.

    Databricks SDK resolution honors ``DATABRICKS_CONFIG_PROFILE`` and
    ``DATABRICKS_CONFIG_FILE``. If the host strips either selector, a
    native Codex runner can fail to resolve a profile that works in the
    user's shell.
    """
    env = _build_runner_env(
        {
            "PATH": "/usr/bin:/bin",
            "DATABRICKS_CONFIG_PROFILE": "oss",
            "DATABRICKS_CONFIG_FILE": "/tmp/databrickscfg",
        },
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    assert env["DATABRICKS_CONFIG_PROFILE"] == "oss"
    assert env["DATABRICKS_CONFIG_FILE"] == "/tmp/databrickscfg"


def test_build_runner_env_propagates_data_dir_paths_not_db_uri() -> None:
    """
    The runtime data/config-dir PATH vars propagate to runners so the whole
    local chain agrees on where config + data live, but the DB URI (which may
    embed a password) does not.

    Regression guard: ``OMNIGENT_CONFIG_HOME`` was absent from the
    allowlist, so the daemon/runner used ``~/.omnigent`` while a CLI run
    under an isolated config home read the local-server pidfile elsewhere —
    discovery then timed out (the e2e ``OMNIGENT_CONFIG_HOME`` isolation
    case). A failure of the first two asserts means that regression is back;
    a failure of the third means a DB secret can now leak into a (possibly
    hosted) runner.
    """
    base = {
        "PATH": "/usr/bin:/bin",
        "OMNIGENT_CONFIG_HOME": "/tmp/iso-home",
        "OMNIGENT_DATA_DIR": "/tmp/iso-data",
        "OMNIGENT_DATABASE_URI": "postgresql://user:pw@host/db",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    # Path vars propagate — they're how the runner finds the same config/data
    # dir the CLI + daemon + local server use.
    assert env["OMNIGENT_CONFIG_HOME"] == "/tmp/iso-home"
    assert env["OMNIGENT_DATA_DIR"] == "/tmp/iso-data"
    # The DB URI is NOT propagated — it may carry credentials and a runner
    # (hosted or local) has no business holding the server's DB connection.
    assert "OMNIGENT_DATABASE_URI" not in env


def test_build_runner_env_propagates_disable_keyring() -> None:
    """``OMNIGENT_DISABLE_KEYRING`` propagates so the runner resolves
    ``keychain:`` secret refs against the SAME backend the CLI configured.

    Regression guard: with the flag set, ``configure harnesses`` stores pasted
    API keys via the FILE backend (``secrets.json``) and writes
    ``keychain:<name>`` refs. If the flag didn't reach the runner it would fall
    back to the OS keyring and fail with "no stored secret named …" for a key
    the CLI just saved — the headless / file-backend deploy case (and the exact
    failure hit while dogfooding the first-run flow).
    """
    base = {"PATH": "/usr/bin:/bin", "OMNIGENT_DISABLE_KEYRING": "1"}
    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )
    assert env["OMNIGENT_DISABLE_KEYRING"] == "1"


# ── host.list_dir handler ───────────────────────────────


def test_handle_list_dir_returns_sorted_entries(tmp_path: Path) -> None:
    """
    Verify ``_handle_list_dir`` returns entries sorted by name with
    correct types and sizes.

    Sorted order matters for the Web UI tree (stable rendering)
    and for cursor-based pagination across pages.
    """
    host = _make_host_process()
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("hello")
    (tmp_path / "build").mkdir()

    result = host._handle_list_dir(HostListDirFrame(request_id="r1", path=str(tmp_path)))

    assert isinstance(result, HostListDirResultFrame)
    assert result.status == "ok"
    assert result.error is None
    names = [e.name for e in result.entries]
    # Sorted by name — order pinned so cursor pagination is stable.
    assert names == ["README.md", "build", "src"]

    # Type classification.
    by_name = {e.name: e for e in result.entries}
    assert by_name["src"].type == "directory"
    assert by_name["build"].type == "directory"
    assert by_name["README.md"].type == "file"

    # Size only set for files.
    assert by_name["README.md"].bytes == 5  # len("hello")
    assert by_name["src"].bytes is None
    assert by_name["build"].bytes is None


def test_handle_list_dir_missing_path_returns_error(tmp_path: Path) -> None:
    """
    Verify a missing path returns ``status: "ok"`` with an error
    message rather than ``status: "failed"``.

    The design treats missing as a normal answer (so the route
    layer maps it to a 404 with the descriptive error). If we
    surfaced ``status: "failed"`` instead, every missing-directory
    browse would 500.
    """
    host = _make_host_process()
    missing = tmp_path / "does_not_exist"

    result = host._handle_list_dir(HostListDirFrame(request_id="r2", path=str(missing)))

    assert result.status == "ok"
    assert result.error == "path does not exist"
    assert result.entries == []


def test_handle_list_dir_on_file_returns_error(tmp_path: Path) -> None:
    """
    Verify list_dir on a regular file returns "not a directory"
    rather than crashing.

    Without this guard, a user clicking on a file in the picker
    would get a 500 instead of a clear "not a directory" message.
    """
    host = _make_host_process()
    target = tmp_path / "file.txt"
    target.write_text("hi")

    result = host._handle_list_dir(HostListDirFrame(request_id="r3", path=str(target)))

    assert result.status == "ok"
    assert "not a directory" in (result.error or "")
    assert result.entries == []


def test_handle_list_dir_follows_symlink_to_directory(tmp_path: Path) -> None:
    """
    Verify list_dir on a symlinked directory follows the link.

    Same posture as host.stat: symlinks are followed, type
    classification reflects the target. Without this, the picker
    would silently fail to enter symlinked directories.
    """
    host = _make_host_process()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "child.txt").write_text("data")
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    result = host._handle_list_dir(HostListDirFrame(request_id="r4", path=str(link)))

    assert result.status == "ok"
    assert len(result.entries) == 1
    assert result.entries[0].name == "child.txt"


def test_handle_list_dir_skips_dangling_symlink_per_entry(
    tmp_path: Path,
) -> None:
    """
    Verify per-entry ``OSError`` (e.g. dangling symlink) is silently
    skipped rather than failing the whole listing.

    Matches the runner's ``list_dir`` posture: a single broken
    symlink in a large directory shouldn't prevent the user from
    seeing the rest. Without the per-entry try/except in the
    handler, a dangling symlink in a project's tree would 500
    every browse of that directory.
    """
    host = _make_host_process()
    good_file = tmp_path / "good.txt"
    good_file.write_text("ok")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing_target")

    result = host._handle_list_dir(HostListDirFrame(request_id="r5", path=str(tmp_path)))

    assert result.status == "ok"
    names = [e.name for e in result.entries]
    # Good entry survived; dangling was silently skipped.
    assert "good.txt" in names
    assert "dangling" not in names


def test_handle_list_dir_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    """
    Verify ``~`` in the input path is expanded against the host
    process owner's home.

    The host owns ``~`` resolution; the server passes tildes
    through unchanged (designs/SESSION_WORKSPACE_SELECTION.md).
    Without expansion, ``~/projects`` would become a
    literal subdir named ``~`` and fail with ENOENT.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "x.txt").write_text("data")

    host = _make_host_process()
    result = host._handle_list_dir(HostListDirFrame(request_id="r6", path="~/subdir"))

    assert result.status == "ok"
    assert len(result.entries) == 1
    assert result.entries[0].name == "x.txt"


def test_handle_list_dir_pagination_after_cursor(tmp_path: Path) -> None:
    """
    Verify that ``after`` cursor returns entries strictly after
    the cursor.

    Cursors are entry paths so they survive concurrent directory
    mutations between calls. Without this, the Web UI's "next
    page" button would silently return the same page or skip
    entries.
    """
    host = _make_host_process()
    for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
        (tmp_path / name).write_text("x")

    result = host._handle_list_dir(
        HostListDirFrame(
            request_id="r7",
            path=str(tmp_path),
            limit=2,
            after=str(tmp_path / "b.txt"),
        )
    )

    assert result.status == "ok"
    names = [e.name for e in result.entries]
    # After "b.txt", next two are "c.txt" and "d.txt".
    assert names == ["c.txt", "d.txt"]
    assert result.has_more is True  # "e.txt" still remains


def test_handle_list_dir_pagination_before_cursor_returns_previous_page(
    tmp_path: Path,
) -> None:
    """
    Verify that ``before`` returns the page immediately before the cursor.

    Backward pagination should take the tail of the bounded slice, not the
    first entries in the directory. Otherwise a request for the page before
    "e.txt" returns "a.txt", "b.txt" instead of "c.txt", "d.txt".
    """
    host = _make_host_process()
    for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
        (tmp_path / name).write_text("x")

    middle = host._handle_list_dir(
        HostListDirFrame(
            request_id="r_before_middle",
            path=str(tmp_path),
            limit=2,
            before=str(tmp_path / "e.txt"),
        )
    )

    assert middle.status == "ok"
    assert [e.name for e in middle.entries] == ["c.txt", "d.txt"]
    assert middle.has_more is True

    first = host._handle_list_dir(
        HostListDirFrame(
            request_id="r_before_first",
            path=str(tmp_path),
            limit=2,
            before=str(tmp_path / "c.txt"),
        )
    )

    assert first.status == "ok"
    assert [e.name for e in first.entries] == ["a.txt", "b.txt"]
    assert first.has_more is False


def test_handle_list_dir_pagination_last_page_has_more_false(
    tmp_path: Path,
) -> None:
    """
    Verify ``has_more=False`` on the last page.

    The Web UI uses ``has_more`` to decide whether to render the
    "next page" button. If we always returned ``True``, the user
    would click forever past the end of the listing.
    """
    host = _make_host_process()
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("x")

    result = host._handle_list_dir(HostListDirFrame(request_id="r8", path=str(tmp_path), limit=10))

    assert result.status == "ok"
    assert len(result.entries) == 2
    assert result.has_more is False


# ── host.create_dir handler ─────────────────────────────


def test_handle_create_dir_creates_directory(tmp_path: Path) -> None:
    """
    Verify ``_handle_create_dir`` makes the directory and returns its
    absolute path.

    This is the picker's "New folder" happy path — the returned path
    is what the picker navigates into afterward.
    """
    host = _make_host_process()
    target = tmp_path / "new-app"

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m1", path=str(target)))

    assert isinstance(result, HostCreateDirResultFrame)
    assert result.status == "ok"
    assert result.error is None
    assert result.path == str(target)
    assert target.is_dir()


def test_handle_create_dir_creates_missing_parents(tmp_path: Path) -> None:
    """
    Verify missing parent directories are created (``os.makedirs``).

    Lets the picker accept a nested name like ``a/b/c`` in one go
    rather than forcing the user to create each level.
    """
    host = _make_host_process()
    target = tmp_path / "a" / "b" / "c"

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m2", path=str(target)))

    assert result.status == "ok"
    assert target.is_dir()


def test_handle_create_dir_existing_returns_error_not_failed(tmp_path: Path) -> None:
    """
    Verify creating an existing directory returns ``status: "ok"`` with
    an "already exists" error rather than ``status: "failed"``.

    The route maps a non-empty ``error`` to a 409 so the picker shows
    "directory already exists" inline; surfacing ``failed`` would 502
    instead.
    """
    host = _make_host_process()
    existing = tmp_path / "dup"
    existing.mkdir()

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m3", path=str(existing)))

    assert result.status == "ok"
    assert result.error == "directory already exists"
    assert result.path is None


def test_handle_create_dir_leaf_is_file_reports_file_not_directory(tmp_path: Path) -> None:
    """
    Verify a regular file at the target path reports a file, not a
    directory.

    ``os.makedirs`` raises ``FileExistsError`` for both an existing
    directory and an existing file; the handler must distinguish them
    so the picker doesn't mislabel "a file is in the way" as
    "directory already exists".
    """
    host = _make_host_process()
    a_file = tmp_path / "taken"
    a_file.write_text("hi")

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m3b", path=str(a_file)))

    assert result.status == "ok"
    assert result.error == "a file already exists at that path"
    assert result.path is None


def test_handle_create_dir_parent_is_file_returns_error(tmp_path: Path) -> None:
    """
    Verify creating under a path whose parent is a regular file returns
    a clean error rather than crashing.
    """
    host = _make_host_process()
    a_file = tmp_path / "file.txt"
    a_file.write_text("hi")
    target = a_file / "child"

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m4", path=str(target)))

    assert result.status == "ok"
    assert "not a directory" in (result.error or "")
    assert result.path is None


def test_handle_create_dir_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    """
    Verify ``~`` expands against the host process owner's home.

    The host owns ``~`` resolution; without expansion ``~/scratch``
    would become a literal ``~`` subdir of the process cwd.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    host = _make_host_process()
    result = host._handle_create_dir(HostCreateDirFrame(request_id="m5", path="~/scratch"))

    assert result.status == "ok"
    assert (tmp_path / "scratch").is_dir()
    assert result.path == str(tmp_path / "scratch")


# --- Fail-loud on permanent tunnel failures ----------------------------
#
# Before the fix, HostProcess.run() caught every connection exception and
# reconnected forever, so an auth/authorization/outdated-server failure
# looked like a silent hang. These tests drive the public run() /
# run_host_process entry points with a stubbed websockets connect whose
# handshake (__aenter__) raises the upgrade rejection, and assert that
# permanent failures fail loud while transient ones still reconnect.


class _HandshakeFailingConnect:
    """Async-CM stand-in for ``websockets.asyncio.client.connect`` whose
    handshake raises a preset exception.

    Models the real library: ``connect(...)`` returns an async context
    manager and the upgrade rejection surfaces from ``__aenter__``.

    :param exc: Exception to raise from ``__aenter__``, e.g. an
        :class:`~websockets.exceptions.InvalidStatus` carrying a 403.
    """

    def __init__(self, exc: BaseException) -> None:
        """Store the exception the handshake will raise.

        :param exc: Exception raised on ``__aenter__``.
        """
        self._exc = exc

    async def __aenter__(self) -> object:
        """Raise the preset handshake exception.

        :returns: Never returns — always raises.
        :raises BaseException: The exception passed at construction.
        """
        raise self._exc

    async def __aexit__(self, *exc_info: object) -> bool:
        """No-op async-CM exit.

        :param exc_info: Standard ``__aexit__`` triple (unused).
        :returns: ``False`` so any exception propagates.
        """
        return False


class _DroppedTunnel:
    """Fake accepted tunnel whose receive loop drops immediately.

    Lets ``_serve_frames`` send the ``host.hello`` frame (proving the
    upgrade was accepted), then fails the first ``recv()`` like an
    abruptly closed connection, returning control to the reconnect loop.
    """

    async def send(self, data: str | bytes) -> None:
        """Accept any outbound frame (the ``host.hello``) silently.

        :param data: Encoded frame payload (ignored).
        :returns: None.
        """

    async def recv(self) -> str:
        """Fail like a connection that closed without a close frame.

        :returns: Never returns — always raises.
        :raises ConnectionClosedError: Always, with no close frames.
        """
        raise ConnectionClosedError(None, None)


class _AcceptingConnect:
    """Async-CM stand-in for a *successful* WS upgrade.

    ``__aenter__`` hands back a :class:`_DroppedTunnel`, so the host
    marks the connection authenticated and then immediately loses it —
    the minimal scripted "connected once" event.
    """

    async def __aenter__(self) -> _DroppedTunnel:
        """Complete the handshake with a tunnel that drops on first recv.

        :returns: A :class:`_DroppedTunnel`.
        """
        return _DroppedTunnel()

    async def __aexit__(self, *exc_info: object) -> bool:
        """No-op async-CM exit.

        :param exc_info: Standard ``__aexit__`` triple (unused).
        :returns: ``False`` so any exception propagates.
        """
        return False


class _ConnectSpy:
    """Stub for ``websockets.asyncio.client.connect`` that records calls
    and scripts each handshake with the next queued entry.

    An exception entry fails that handshake; a ``None`` entry accepts it
    with a tunnel that drops on first ``recv()`` (so the reconnect loop
    regains control). The last queued entry repeats for any further
    calls, so a single fatal exception covers the "fails on first
    attempt" case and a ``[transient, CancelledError]`` pair covers
    "retried once, then stop".

    :param exceptions: Per-call handshake script, e.g.
        ``[None, InvalidStatus(resp_503), asyncio.CancelledError()]``.
    """

    def __init__(self, exceptions: list[BaseException | None]) -> None:
        """Initialize the spy with a handshake script.

        :param exceptions: Exception to raise (or ``None`` to accept) on
            each successive call; the final entry repeats.
        """
        self._exceptions = exceptions
        self.call_count = 0

    def __call__(self, url: str, **kwargs: object) -> _HandshakeFailingConnect | _AcceptingConnect:
        """Return an async-CM scripting the handshake for this call.

        :param url: Tunnel URL passed by production (ignored).
        :param kwargs: Connect kwargs passed by production (ignored).
        :returns: A context manager whose ``__aenter__`` raises the
            queued exception, or completes the handshake for a ``None``
            entry.
        """
        exc = self._exceptions[min(self.call_count, len(self._exceptions) - 1)]
        self.call_count += 1
        if exc is None:
            return _AcceptingConnect()
        return _HandshakeFailingConnect(exc)


def _invalid_status(status_code: int) -> InvalidStatus:
    """Build a real :class:`InvalidStatus` for a rejected WS upgrade.

    Matches what ``websockets`` raises when the server answers the
    upgrade with a non-101 status.

    :param status_code: HTTP status on the upgrade response, e.g.
        ``403``.
    :returns: An ``InvalidStatus`` whose ``response.status_code`` is
        *status_code*.
    """
    return InvalidStatus(Response(status_code, "", Headers()))


def _patch_connect(monkeypatch: pytest.MonkeyPatch, spy: _ConnectSpy) -> None:
    """Stub the WS connect and silence the auth-token factory.

    Patches ``websockets.asyncio.client.connect`` (the module attribute
    production resolves at call time) with *spy*, and forces
    ``_make_auth_token_factory`` to return ``None`` so no real Databricks
    credentials/network are touched.

    :param monkeypatch: The pytest monkeypatch fixture.
    :param spy: Connect stub to install.
    :returns: None.
    """
    import websockets.asyncio.client as ws_client

    import omnigent.runner._entry as entry_mod

    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda *, server_url=None: None)
    monkeypatch.setattr(ws_client, "connect", spy)


def _host(
    server_url: str = "https://app.example.databricks.com",
) -> HostProcess:
    """Build a HostProcess for the fail-loud tests.

    :param server_url: Server URL the host connects to, e.g.
        ``"https://app.example.databricks.com"``.
    :returns: A configured :class:`HostProcess`.
    """
    identity = HostIdentity(host_id="host_test_connect", name="test-laptop")
    return HostProcess(identity, server_url)


def test_build_connect_headers_adds_org_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded ?o= selector rides the tunnel handshake.

    The WS upgrade must name the workspace via ``X-Databricks-Org-Id`` or it
    routes to the account. The header rides alongside the Origin sentinel,
    independent of the bearer/managed-token branch.

    :param monkeypatch: The pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.runner._entry as entry_mod

    # No managed token + no real Databricks creds: isolate the bearer
    # branch so only the routing header is under test.
    monkeypatch.delenv("OMNIGENT_HOST_TOKEN", raising=False)
    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda *, server_url=None: None)
    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_org_id", lambda _url: "2850744067564480"
    )

    headers = _host("https://acme.databricks.com/api/2.0/omnigent")._build_connect_headers()

    assert headers["X-Databricks-Org-Id"] == "2850744067564480"


async def test_run_retries_on_login_redirect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A login-redirect upgrade failure retries with a warning.

    The Databricks Apps OAuth proxy bounces an unauthenticated ``wss://``
    upgrade to an ``https://.../authorize`` URL; ``websockets`` then raises
    ``InvalidURI`` because the redirect scheme isn't ws/wss. This can
    happen transiently during server restarts, so the host must retry
    with backoff rather than dying. The warning still surfaces the single
    ``omnigent login`` remediation so the operator can act if the cause
    is persistent.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    spy = _ConnectSpy(
        [
            InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss"),
            asyncio.CancelledError(),
        ]
    )
    _patch_connect(monkeypatch, spy)
    host = _host()

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await host.run()

    # 2 = redirect attempt + cancel attempt → it genuinely reconnected.
    assert spy.call_count == 2
    # The warning surfaces the login-page cause and the credentials hint —
    # the single remediation message recommending `omnigent login <url>`.
    assert any("login page" in r.message for r in caplog.records)
    assert any(
        "omnigent login https://app.example.databricks.com" in r.message for r in caplog.records
    )


async def test_login_redirect_prints_warning_to_terminal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first login redirect of a streak warns on stderr, not just the log.

    ``_logger.warning`` goes to the CLI log file, so before this fix a
    not-logged-in ``omnigent host`` sat completely silent on the terminal
    while retrying (the user's only signal was Ctrl-C and reading the log).
    The terminal warning must name the cause and the copy-pasteable
    ``omnigent login <url>`` remedy.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    spy = _ConnectSpy(
        [
            InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss"),
            asyncio.CancelledError(),
        ]
    )
    _patch_connect(monkeypatch, spy)
    host = _host()

    await host.run()

    err = capsys.readouterr().err
    # The cause reached the terminal — if missing, the silent-hang
    # regression is back (warning only in the log file).
    assert "login page" in err
    # The exact remedy command, URL included, so the user can copy-paste.
    assert "omnigent login https://app.example.databricks.com" in err


async def test_fresh_host_fails_loud_after_persistent_login_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent login redirects on a never-connected host fail loud.

    A host that has never completed an upgrade and keeps getting bounced
    to the login page is unauthenticated, not watching a server restart —
    it must raise HostConnectError (→ exit 1 with the fix printed) instead
    of retrying forever with the terminal silent (the silent-hang regression
    could resurface once the redirect was made retryable).
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    # A single queued redirect repeats forever — the streak only ends
    # because the host gives up.
    spy = _ConnectSpy([InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss")])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    message = str(excinfo.value)
    # The fatal message identifies the auth cause and the exact remedy.
    assert "login page" in message
    assert "omnigent login https://app.example.databricks.com" in message
    # 3 = _LOGIN_REDIRECT_FATAL_ATTEMPTS: enough retries to absorb a
    # one-off proxy blip, then fail. 1 would mean the blip
    # tolerance regressed; more (or no raise at all) would mean the
    # fail-loud threshold is broken and the host loops silently again.
    assert spy.call_count == 3


async def test_login_redirect_streak_resets_on_other_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only CONSECUTIVE login redirects count toward the fatal threshold.

    A messy server restart can interleave login redirects with other
    transient failures (5xx bounces). Those must reset the streak —
    otherwise a fresh host riding out a restart would die from redirects
    accumulated across unrelated errors instead of three in a row.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    redirect = InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss")
    # Two redirects, then a 503 (must reset the streak), then redirects
    # repeating forever until the host gives up.
    spy = _ConnectSpy([redirect, redirect, _invalid_status(503), redirect])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError):
        await host.run()

    # 6 = 2 redirects + the streak-resetting 503 + 3 consecutive
    # redirects that hit the fatal threshold. 5 would mean the 503 did
    # NOT reset the streak (the pre-restart redirects were counted),
    # killing fresh hosts during messy restarts.
    assert spy.call_count == 6


async def test_connected_host_retries_login_redirects_indefinitely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login redirects after a successful connect never turn fatal.

    A host that already authenticated and then gets login redirects is
    watching a server restart behind the Apps OAuth proxy —
    killing it would drop its live runners. It must keep retrying past
    the fresh-host fatal threshold.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    redirect = InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss")
    # Accepted upgrade first (None), then MORE redirects than the
    # fresh-host fatal threshold of 3, then a cancel to end the test.
    spy = _ConnectSpy([None, redirect, redirect, redirect, redirect, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    # Returns normally: if the post-connect redirects were misclassified
    # as fatal this would raise HostConnectError after the 3rd redirect
    # (call_count 4) instead.
    await host.run()

    # 6 = accepted connect + 4 retried redirects + the ending cancel.
    # Fewer means the host died mid-streak (restart killed a live host).
    assert spy.call_count == 6


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "HTTP 401"),
        (403, "HTTP 403"),
        (404, "permanent"),
    ],
)
async def test_run_fails_loud_on_permanent_4xx(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: str
) -> None:
    """A permanent 4xx upgrade rejection fails loud on the first attempt.

    401/403/other-4xx mean unauthenticated / unauthorized / wrong-or-old
    server — reconnecting can never succeed, so run() must raise
    HostConnectError immediately rather than backing off.
    """
    spy = _ConnectSpy([_invalid_status(status)])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    # Message identifies the specific permanent failure.
    assert expected in str(excinfo.value)
    # Exactly one attempt → no silent reconnect/backoff. If >1, the 4xx
    # was misclassified as transient and the loop kept retrying.
    assert spy.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_rejection_suggests_omnigent_login(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """401/403 rejections point the user at ``omnigent login``.

    Both statuses are auth failures the user can resolve by logging in to
    an accounts/OIDC-mode server (a Databricks profile token may
    authenticate at the proxy yet be rejected by the server itself). The
    fatal message must name the exact remedy command, including the
    server URL, so the user can copy-paste it.
    """
    server_url = "https://app.example.databricks.com"
    spy = _ConnectSpy([_invalid_status(status)])
    _patch_connect(monkeypatch, spy)
    host = _host(server_url=server_url)

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    # The exact, copy-pasteable command — URL included — must be present,
    # not just the bare word "login".
    assert f"omnigent login {server_url}" in str(excinfo.value)


async def test_non_auth_permanent_4xx_omits_login_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-auth permanent 4xx (404) does NOT suggest ``omnigent login``.

    The login remedy is specific to credential/authorization failures;
    surfacing it on a 404 ("wrong URL / route missing") would misdirect
    the user. This pins the hint to the 401/403 paths only.
    """
    spy = _ConnectSpy([_invalid_status(404)])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    message = str(excinfo.value)
    # Confirm we got the actual 404 fatal message (not some unrelated
    # error that happens to lack "login") before asserting the absence.
    assert "HTTP 404" in message
    assert "omnigent login" not in message


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_run_reconnects_on_transient_upgrade_failure(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Transient upgrade failures reconnect instead of failing loud.

    Retryable 4xx (408/429) and any 5xx are transient (server bounce,
    rate limit) and must stay on the reconnect path. A clean
    ``CancelledError`` on the second attempt ends the loop so the test
    terminates; with backoff zeroed the retry is immediate.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    spy = _ConnectSpy([_invalid_status(status), asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    # Returns normally (no HostConnectError): the transient failure was
    # retried, and the 2nd-attempt CancelledError broke the loop. If the
    # transient status had been treated as fatal, this would raise
    # HostConnectError instead and call_count would be 1.
    await host.run()

    # 2 = transient attempt + cancel attempt → it genuinely reconnected.
    assert spy.call_count == 2


def test_run_host_process_exits_nonzero_on_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run_host_process surfaces a fatal tunnel failure as exit code 1.

    The CLI entry point must print the cause + fix and exit non-zero, not
    hang silently. Driven through the real ``asyncio.run``
    path, so this is a sync test.
    """
    _patch_connect(monkeypatch, _ConnectSpy([_invalid_status(403)]))

    with pytest.raises(SystemExit) as excinfo:
        run_host_process(
            server_url="https://app.example.databricks.com",
            config_path=tmp_path / "config.yaml",
        )

    # Non-zero exit so callers/CI see the failure.
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    # The actionable message reached stderr (banner + the 403 cause).
    assert "Could not connect" in err
    assert "HTTP 403" in err


def test_run_host_process_announces_session_log_dir_on_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``omnigent host`` names the session-log dir on start (was silent).

    The reported regression: ``omnigent host`` ran completely silently, so a
    quiet/stuck host gave no hint where to look. The startup banner now points
    at the per-session runner log dir up front; the exact ``runner-*.log`` is
    printed later when each runner launches. We stub the tunnel to a clean
    cancel so ``run()`` returns immediately, and repoint ``Path.home`` so the
    advertised dir resolves under tmp.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    # A single CancelledError ends the connect loop cleanly (no fatal exit),
    # so run_host_process returns after printing the startup banner.
    _patch_connect(monkeypatch, _ConnectSpy([asyncio.CancelledError()]))

    run_host_process(
        server_url="https://app.example.databricks.com",
        config_path=tmp_path / "config.yaml",
    )

    out = capsys.readouterr().out
    assert "Session logs: ~/.omnigent/logs/host-runner/" in out
