"""Intent transport, CLI/service targeting, and launch races without live daemons."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner

from omnigent import cli
from omnigent.host import service, shutdown
from omnigent.host.connect import HostProcess, _RunnerHandle
from omnigent.host.daemon_lifecycle import DaemonLifecycleLock
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostShutdownAckFrame,
    HostShutdownFrame,
    HostStopRunnerFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.host.shutdown import ShutdownIntent


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE", "0")
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")


def _record(pid: int = 4242, target: str = "http://127.0.0.1:1") -> cli._HostDaemonRecord:
    return cli._HostDaemonRecord(
        pid=pid,
        target=target,
        mode="server",
        server_url=target,
        log_path="unused.log",
        started_at=1,
        host_id="a" * 32,
        process_id=f"{pid:032x}",
        connection_id="b" * 32,
    )


@pytest.mark.posix_only
@pytest.mark.parametrize("unknown_sigint", [False, True])
def test_shutdown_handlers_preserve_native_and_ignored_dispositions(
    monkeypatch: pytest.MonkeyPatch, unknown_sigint: bool
) -> None:
    """A native disposition cannot be restored with signal.signal(sig, None)."""
    original = {
        signal.SIGINT: None if unknown_sigint else signal.default_int_handler,
        signal.SIGTERM: None,
        signal.SIGHUP: signal.SIG_IGN,
    }
    handlers = dict(original)
    installed = []

    def set_handler(sig, handler):
        assert handler is not None
        installed.append(sig)
        previous = handlers[sig]
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(signal, "getsignal", handlers.get)
    monkeypatch.setattr(signal, "signal", set_handler)
    monkeypatch.setattr(asyncio, "get_running_loop", Mock(return_value=Mock()))
    host = HostProcess(HostIdentity(host_id="a" * 32, name="test"), "http://127.0.0.1:1")
    restore = host._install_shutdown_handlers()
    if not unknown_sigint:
        callback = handlers[signal.SIGINT]
        callback(signal.SIGINT, None)
        with pytest.raises(KeyboardInterrupt):
            callback(signal.SIGINT, None)
    restore()
    restore()
    assert handlers == original
    assert installed == ([] if unknown_sigint else [signal.SIGINT, signal.SIGINT])


def test_new_frames_round_trip_and_legacy_frames_remain_valid() -> None:
    intent = cli._daemon_shutdown_intent(_record(), force=True, daemon_only=True)
    for frame in (
        HostHelloFrame("test", 1, "host", process_id="process", connection_id="connection"),
        HostShutdownFrame(intent, ["runner"]),
        HostShutdownAckFrame(intent.shutdown_id),
        HostStopRunnerFrame("request", "runner", intent),
    ):
        assert decode_host_frame(encode_host_frame(frame)) == frame
    legacy = decode_host_frame(
        json.dumps(
            {
                "kind": "host.hello",
                "version": "old",
                "frame_protocol_version": 1,
                "name": "old",
            }
        )
    )
    assert legacy.process_id is None and legacy.connection_id is None
    stop = decode_host_frame('{"kind":"host.stop_runner","request_id":"old","runner_id":"runner"}')
    assert stop.shutdown_intent is None


@pytest.mark.posix_only
def test_connection_record_update_preserves_lifetime_lock(tmp_path: Path) -> None:
    record = _record()
    cli._write_daemon_record(record)
    path = cli._daemon_record_path(record.target)
    lock = DaemonLifecycleLock.for_target(record.target, base_dir=tmp_path, pid=record.pid)
    assert lock.acquire()
    try:
        inode = path.stat().st_ino
        lock.publish_connection(record.process_id, "c" * 32)
        assert path.stat().st_ino == inode
        assert lock.process_id() == record.process_id
        assert cli._read_daemon_record(path).connection_id == "c" * 32
        lock.publish_connection("retired-process", "d" * 32)
        assert cli._read_daemon_record(path).connection_id == "c" * 32
        competing = DaemonLifecycleLock.for_target(
            record.target, base_dir=tmp_path, pid=record.pid
        )
        assert not competing.acquire()
    finally:
        lock.release()


def test_mailbox_checks_process_and_pid_and_tolerates_unlink_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    intent = cli._daemon_shutdown_intent(record)
    path = tmp_path / "intent.json"
    shutdown.write_shutdown_request(path, intent)
    assert shutdown.read_shutdown_request(path, "retired", record.pid) is None
    assert shutdown.read_shutdown_request(path, record.process_id, record.pid + 1) is None
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

    def failed_unlink(*_args, **_kwargs):
        raise PermissionError("read-only directory")

    monkeypatch.setattr(Path, "unlink", failed_unlink)
    assert shutdown.read_shutdown_request(path, record.process_id, record.pid) == intent


@pytest.mark.parametrize(
    "flags", [[], ["--force"], ["--daemon-only"], ["--force", "--daemon-only"]]
)
def test_host_stop_records_intent_before_termination(
    flags: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    cli._write_daemon_record(record)
    monkeypatch.setattr(cli, "_selected_daemon_records", lambda **_kwargs: [record])
    monkeypatch.setattr(cli, "_pid_is_recorded_daemon", lambda _record: True)
    calls = []

    def notify(**kwargs):
        calls.append(("notify", ShutdownIntent.model_validate(kwargs["json_body"])))
        return cli._HostHttpResult(status_code=0, body="server unavailable")

    def drain(_record, **kwargs):
        calls.append(("drain", ShutdownIntent.model_validate(kwargs["shutdown_intent"])))
        return 0

    def terminate(stopped, *, force):
        path = cli._daemon_record_path(stopped.target).with_name(
            f"shutdown-{stopped.process_id}.json"
        )
        intent = ShutdownIntent.model_validate_json(path.read_text())
        assert intent.force == force == ("--force" in flags)
        assert intent.daemon_only == ("--daemon-only" in flags)
        assert intent.initiator_user_id is None
        assert calls[-1][0] == "notify"
        assert calls[-1][1].shutdown_id == intent.shutdown_id
        calls.append(("terminate", intent))

    monkeypatch.setattr(cli, "_host_http_json", notify)
    monkeypatch.setattr(cli, "_stop_daemon_sessions", drain)
    monkeypatch.setattr(cli, "_terminate_daemon", terminate)
    result = CliRunner().invoke(cli.cli, ["host", "stop", *flags])
    assert result.exit_code == 0, result.output
    assert [name for name, _ in calls] == (["drain"] if not flags else []) + [
        "notify",
        "terminate",
    ]
    assert len({intent.shutdown_id for _, intent in calls}) == 1


def test_cli_notification_thread_is_bounded_and_catches_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    cli._write_daemon_record(record)
    monkeypatch.setattr(cli, "_pid_is_recorded_daemon", lambda _record: True)
    monkeypatch.setattr(shutdown, "SHUTDOWN_NOTIFY_TIMEOUT_S", 0.05)
    release, completed = threading.Event(), threading.Event()

    def unavailable(**_kwargs):
        try:
            release.wait(5)
            raise OSError("credential provider failed")
        finally:
            completed.set()

    monkeypatch.setattr(cli, "_host_http_json", unavailable)
    started = time.monotonic()
    try:
        path = cli._prepare_daemon_shutdown(record, cli._daemon_shutdown_intent(record))
        assert path is not None and path.exists()
        assert time.monotonic() - started < 1
    finally:
        release.set()
        assert completed.wait(5)


def test_daemon_notification_preserves_command_time_after_session_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    cli._write_daemon_record(record)
    monkeypatch.setattr(cli, "_pid_is_recorded_daemon", lambda _record: True)
    notify = Mock(return_value=cli._HostHttpResult(200, {}))
    monkeypatch.setattr(cli, "_host_http_json", notify)
    intent = cli._daemon_shutdown_intent(record).model_copy(
        update={"requested_at_ms": shutdown.timestamp_ms() - 30_000}
    )
    path = cli._prepare_daemon_shutdown(record, intent)
    assert path is not None
    observed = shutdown.read_shutdown_request(path, record.process_id, record.pid)
    assert observed == intent
    assert notify.call_args.kwargs["json_body"]["requested_at_ms"] == intent.requested_at_ms


def test_cli_instrumentation_failure_still_terminates(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _record()
    monkeypatch.setattr(cli, "_selected_daemon_records", lambda **_kwargs: [record])
    monkeypatch.setattr(
        cli, "_prepare_daemon_shutdown", Mock(side_effect=OSError("mailbox unavailable"))
    )
    terminate = Mock()
    monkeypatch.setattr(cli, "_terminate_daemon", terminate)
    result = CliRunner().invoke(cli.cli, ["host", "stop", "--force"])
    assert result.exit_code == 0, result.output
    terminate.assert_called_once_with(record, force=True)


def test_host_disable_attributes_only_service_managed_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed, foreground = _record(), _record(4343, "http://127.0.0.1:2")
    monkeypatch.setattr(cli, "_list_daemon_records", lambda: [managed, foreground])
    monkeypatch.setattr(cli, "_pid_alive", lambda _pid: True)
    prepared = []

    def prepare(record, intent):
        prepared.append((record, intent))

    def disable(*, before_stop):
        before_stop(managed.pid)
        return service.HostService("systemd_user", tmp_path / "test.service", "test.service")

    monkeypatch.setattr(cli, "_prepare_daemon_shutdown", prepare)
    monkeypatch.setattr(service, "disable_user_host_service", disable)
    result = CliRunner().invoke(cli.cli, ["host", "disable"])
    assert result.exit_code == 0, result.output
    assert len(prepared) == 1 and prepared[0][0] == managed
    assert prepared[0][1].action == "host_disable"


@pytest.mark.parametrize("kind", ["launchd", "systemd_user"])
def test_service_pid_lookup_and_instrumentation_failure_do_not_block_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    installed = service.HostService(kind, tmp_path / "test.service", "test.service")
    installed.path.touch()
    monkeypatch.setattr(service, "_service_for_current_platform", lambda: installed)
    monkeypatch.setattr(service, "_forget_service", lambda _service: None)
    monkeypatch.setattr(service, "_wait_for_launchd_unload", lambda _target: True)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "show" in args or "print" in args:
            assert kwargs["timeout"] == 2
            return subprocess.CompletedProcess(
                args, 0, "  pid = 4242\n" if kind == "launchd" else "4242\n", ""
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    def before_stop(pid):
        assert pid == 4242
        assert len(calls) == 1
        raise ValueError("invalid evidence")

    monkeypatch.setattr(service.subprocess, "run", run)
    service.disable_user_host_service(before_stop=before_stop)
    assert not installed.path.exists()
    assert any("bootout" in call or "disable" in call for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_preflight", [False, True])
async def test_shutdown_rejects_new_and_preflight_launches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    during_preflight: bool,
) -> None:
    host = HostProcess(HostIdentity("a" * 32, "test"), "http://127.0.0.1:1")
    intent = cli._daemon_shutdown_intent(_record())
    if during_preflight:

        def auth(**_kwargs):
            host._shutdown_intent = intent
            return

        monkeypatch.setattr(host, "_current_auth_token", auth)
    else:
        host._shutdown_intent = intent
    spawn = Mock(side_effect=AssertionError("shutdown must reject spawn"))
    monkeypatch.setattr(host, "_spawn_runner_proc", spawn)
    result = await host._handle_launch(HostLaunchRunnerFrame("request", "token", str(tmp_path)))
    assert result.status == "failed" and result.error == "Host is shutting down"
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_notification_captures_spawn_that_finishes_during_poll(
    tmp_path: Path,
) -> None:
    host = HostProcess(HostIdentity("a" * 32, "test"), "http://127.0.0.1:1")
    host._shutdown_intent = cli._daemon_shutdown_intent(_record())
    started, release = threading.Event(), threading.Event()

    def poll():
        started.set()
        assert release.wait(5)

    host._runners["existing"] = _RunnerHandle(SimpleNamespace(poll=poll), tmp_path / "unused")
    host._spawning_runner_ids.add("launch")
    host._ws = AsyncMock()
    host._shutdown_ack.set()
    task = asyncio.create_task(host._notify_shutdown())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        host._spawning_runner_ids.remove("launch")
        host._runners["launch"] = _RunnerHandle(
            SimpleNamespace(poll=lambda: None), tmp_path / "unused"
        )
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    frame = decode_host_frame(host._ws.send.call_args.args[0])
    assert frame.runner_ids == ["existing", "launch"]
