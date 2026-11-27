"""Shell request wiring with process creation, seccomp, and pidfd signaling replaced."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.inner import _bwrap_shell_request as shell_request
from omnigent.inner import os_env as os_env_module
from omnigent.inner._bounded_output import CapturedOutput, _TextPrefix
from omnigent.inner._bwrap_request_control import BwrapRequestResult, RequestCancelled
from omnigent.inner.datamodel import CredentialProxySpec, OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import CallerProcessOSEnvironment, _HelperProcessClient, _PreparedHelper
from omnigent.inner.sandbox import SandboxPolicy
from tests._helpers.polling import poll_until_async

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux anonymous descriptor wiring"
)


def _policy(cwd: Path) -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[cwd],
        write_files=[],
        allow_network=False,
    )


@pytest.mark.parametrize(
    "unsupported",
    ["platform", "inactive", "backend", "cow", "proxy", "missing", "flags", "kernel"],
)
def test_unsupported_launchers_are_rejected_before_any_shell_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsupported: str
) -> None:
    executable = tmp_path / "bwrap"
    executable.write_text("not an executable")
    policy = _policy(tmp_path)
    if unsupported == "platform":
        monkeypatch.setattr(shell_request, "sys", SimpleNamespace(platform="darwin"))
    elif unsupported == "inactive":
        policy = replace(policy, active=False)
    elif unsupported == "backend":
        policy = replace(policy, backend_type="seatbelt")
    elif unsupported == "cow":
        policy = replace(policy, copy_on_write_roots=[tmp_path])
    elif unsupported == "proxy":
        policy = replace(policy, credential_proxy=CredentialProxySpec(entries=[]))
    which = MagicMock(return_value=None if unsupported == "missing" else str(executable))
    monkeypatch.setattr(shell_request.shutil, "which", which)
    probe = MagicMock(return_value=unsupported != "flags")
    monkeypatch.setattr(shell_request, "_has_request_options", probe)
    pidfds: list[int] = []

    def pidfd_open(_pid: int) -> int:
        if unsupported == "kernel":
            raise OSError("pidfd unsupported")
        descriptor = os.open(os.devnull, os.O_RDONLY)
        pidfds.append(descriptor)
        return descriptor

    monkeypatch.setattr(shell_request.os, "pidfd_open", pidfd_open)
    monkeypatch.setattr(shell_request.signal, "pidfd_send_signal", MagicMock())
    assert shell_request.request_launcher(policy, {"PATH": "/selected/path"}) is None
    if unsupported in {"flags", "kernel", "missing"}:
        which.assert_called_with("bwrap", path="/selected/path")
    for descriptor in pidfds:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_launcher_probe_uses_the_selected_executable_and_rechecks_replacements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "bwrap"
    executable.write_text("first")
    monkeypatch.setattr(shell_request.shutil, "which", lambda *_a, **_k: str(executable))
    monkeypatch.setattr(
        shell_request.os, "pidfd_open", lambda _pid: os.open(os.devnull, os.O_RDONLY)
    )
    monkeypatch.setattr(shell_request.signal, "pidfd_send_signal", MagicMock())
    run = MagicMock(
        return_value=subprocess.CompletedProcess(
            [], 0, stdout="--as-pid-1 --info-fd --add-seccomp-fd"
        )
    )
    monkeypatch.setattr(shell_request.subprocess, "run", run)
    shell_request._has_request_options.cache_clear()
    try:
        for _ in range(2):
            assert shell_request.request_launcher(_policy(tmp_path), {}) == str(executable)
        run.assert_called_once()
        assert run.call_args.args[0] == [str(executable), "--help"]
        assert run.call_args.kwargs["env"] == {"PATH": os.defpath, "LANG": "C"}
        executable.write_text("replacement with no request flags")
        run.return_value = subprocess.CompletedProcess([], 0, stdout="--unshare-pid")
        assert shell_request.request_launcher(_policy(tmp_path), {}) is None
        assert run.call_count == 2
    finally:
        shell_request._has_request_options.cache_clear()


@dataclass
class _LaunchCase:
    request: shell_request.BwrapShellRequest
    process: MagicMock
    owner: MagicMock
    receive: MagicMock
    poll: MagicMock
    spawn: MagicMock
    config: dict[str, object]
    descriptors: list[int]


@pytest.fixture
def launch_case(monkeypatch: pytest.MonkeyPatch) -> _LaunchCase:
    request = shell_request.BwrapShellRequest()
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 12345
    process.stdin = None
    process.stdout = io.BytesIO()
    process.stderr = io.BytesIO()
    process.communicate.return_value = (b"", b"")
    owner = MagicMock(spec=shell_request.BwrapRequestControl)
    configuration: dict[str, object] = {}
    descriptors: list[int] = []
    transfer: list[int] = []
    poll = MagicMock(return_value=[(1, 1)])
    monkeypatch.setattr(
        shell_request.select, "poll", lambda: SimpleNamespace(register=lambda *_: None, poll=poll)
    )
    monkeypatch.setattr(shell_request, "request_launcher", lambda *_: "/selected/bwrap")
    monkeypatch.setattr(
        shell_request.BwrapSandboxBackend,
        "wrap_launcher_argv",
        lambda _s, argv, *_a, **_k: ["bwrap", *argv],
    )

    def pidfd_open(_pid: int) -> int:
        descriptor = os.open(os.devnull, os.O_RDONLY)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(shell_request.os, "pidfd_open", pidfd_open)

    def spawn(argv: list[str], env: dict[str, str], fds: tuple[int, ...]) -> MagicMock:
        configuration.update(json.loads(os.pread(fds[1], 1_000_000, 0)))
        os.write(fds[2], b'{"child-pid": 23456}')
        assert argv[0] == "/selected/bwrap"
        assert configuration["token"] not in " ".join(argv)
        assert "broker-secret" not in " ".join(argv)
        assert "broker-secret" not in json.dumps(env)
        return process

    def receive(*_args: object, **kwargs: object) -> MagicMock:
        assert kwargs["expected_init_pid"] == 23456
        assert kwargs["encoding_required"] is True
        descriptor = kwargs["launcher_pidfd"]
        assert isinstance(descriptor, int)
        transfer.append(descriptor)
        descriptors.append(descriptor)
        return owner

    def run(*_args: object, **_kwargs: object) -> BwrapRequestResult:
        os.close(transfer.pop())
        return BwrapRequestResult(b"done\r\n", b"", 0, False, "utf-8")

    owner.run.side_effect = run
    receive_mock = MagicMock(side_effect=receive)
    monkeypatch.setattr(shell_request.BwrapRequestControl, "receive", receive_mock)
    return _LaunchCase(
        request,
        process,
        owner,
        receive_mock,
        poll,
        MagicMock(side_effect=spawn),
        configuration,
        descriptors,
    )


def _launch(case: _LaunchCase, cwd: Path) -> BwrapRequestResult:
    return case.request.run(
        argv=["/bin/sh", "-c", "printf done"],
        cwd=cwd,
        policy=replace(
            _policy(cwd), egress_relay_port=1234, egress_socket_path="/private/broker.sock"
        ),
        launch_env={"PATH": os.defpath, "HTTP_PROXY": "http://127.0.0.1:1234"},
        command_env={"HTTP_PROXY": "http://omnigent:broker-secret@127.0.0.1:1234"},
        timeout=7,
        max_output=17,
        spawn=case.spawn,
    )


def test_launch_delivers_private_config_and_preserves_the_relay_until_owner_completion(
    launch_case: _LaunchCase, tmp_path: Path
) -> None:
    case = launch_case
    assert _launch(case, tmp_path) == BwrapRequestResult(b"done\r\n", b"", 0, False, "utf-8")
    assert case.config["env"] == {"HTTP_PROXY": "http://omnigent:broker-secret@127.0.0.1:1234"}
    assert case.config["report_encoding"] is True
    assert case.config["relay"] == {
        "source": str(Path(shell_request.__file__).with_name("egress") / "relay.py"),
        "port": 1234,
        "socket_path": "/private/broker.sock",
    }
    case.owner.run.assert_called_once()
    assert case.owner.run.call_args.kwargs["max_output"] == 17
    case.process.communicate.assert_not_called()
    assert case.process.stdout.closed and case.process.stderr.closed
    for descriptor in case.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("failure", ["readiness-timeout", "invalid-metadata", "receive", "spawn"])
def test_pre_admission_failure_closes_the_channel_reaps_the_launcher_and_never_retries(
    launch_case: _LaunchCase, tmp_path: Path, failure: str
) -> None:
    case = launch_case
    if failure == "readiness-timeout":
        case.poll.return_value = []
    elif failure == "invalid-metadata":
        original = case.spawn.side_effect

        def invalid_metadata(
            argv: list[str], env: dict[str, str], fds: tuple[int, ...]
        ) -> MagicMock:
            result = original(argv, env, fds)
            os.ftruncate(fds[2], 0)
            os.pwrite(fds[2], b'{"child-pid": 0}', 0)
            return result

        case.spawn.side_effect = invalid_metadata
    elif failure == "receive":
        case.receive.side_effect = OSError("readiness rejected")
    else:
        case.spawn.side_effect = OSError("spawn failed")
    with pytest.raises(OSError):
        _launch(case, tmp_path)
    case.spawn.assert_called_once()
    case.owner.run.assert_not_called()
    if failure != "spawn":
        case.process.communicate.assert_called_once_with(timeout=1)
        assert case.process.stdout.closed and case.process.stderr.closed
    for descriptor in case.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_cancelled_request_never_reaches_spawn(launch_case: _LaunchCase, tmp_path: Path) -> None:
    launch_case.request.cancel()
    with pytest.raises(RequestCancelled):
        _launch(launch_case, tmp_path)
    launch_case.spawn.assert_not_called()


@pytest.mark.parametrize(
    "ending",
    ["success", "nonzero", "nonzero-truncated", "timeout", "lost", "decode", "timeout-invalid"],
)
def test_helper_uses_only_prepared_environment_and_preserves_shell_result_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ending: str
) -> None:
    helper = _HelperProcessClient(cwd=tmp_path, shell_path="/bin/sh", sandbox=_policy(tmp_path))
    prepared = _PreparedHelper(
        replace(_policy(tmp_path), egress_relay_port=1234, egress_socket_path="/broker.sock"),
        tmp_path,
        {
            "PATH": os.defpath,
            "HTTP_PROXY": "http://127.0.0.1:1234",
            "PYTHONPATH": str(os_env_module._project_root()),
        },
    )
    monkeypatch.setenv("UNGRANTED_SECRET", "must-stay-in-the-parent")
    helper._prepared = prepared
    helper._egress_auth_token = "broker-secret"
    start = MagicMock()
    monkeypatch.setattr(helper, "_ensure_started_locked", start)
    request = shell_request.BwrapShellRequest()
    encoding = "utf-8" if ending in {"decode", "timeout-invalid"} else "iso8859-1"
    limit = 16 if ending == "nonzero" else 4

    def capture(raw: bytes) -> CapturedOutput:
        output = _TextPrefix(encoding, limit)
        output.feed(raw, final=True)
        return output.result()

    result = BwrapRequestResult(
        capture(b"caf\xe9\r\n"),
        capture(b"problem\r"),
        None if ending.startswith("timeout") else 7 if ending.startswith("nonzero") else 0,
        ending.startswith("timeout"),
        encoding,
    )
    run = MagicMock(return_value=result)
    if ending == "lost":
        run.side_effect = OSError("result lost after admission")
    monkeypatch.setattr(request, "run", run)
    try:
        actual = helper.shell_request("printf result", 7, limit, request)
        run.assert_called_once()
        options = run.call_args.kwargs
        assert options["policy"] is prepared.sandbox
        assert options["cwd"] == tmp_path
        assert options["max_output"] == limit
        assert options["launch_env"] == prepared.env
        assert "UNGRANTED_SECRET" not in options["command_env"]
        assert "PYTHONPATH" not in options["command_env"]
        assert (
            options["command_env"]["HTTP_PROXY"] == "http://omnigent:broker-secret@127.0.0.1:1234"
        )
        assert prepared.env["HTTP_PROXY"] == "http://127.0.0.1:1234"
        if ending == "lost":
            assert "not sent again" in actual["error"]
        elif ending == "decode":
            assert actual["error"].startswith("os_env helper exception:")
        elif ending == "timeout-invalid":
            assert actual == {
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "timed_out": True,
                "error": "Command timed out after 7 seconds",
                "shell": "/bin/sh",
                "cwd": str(tmp_path),
            }
        else:
            assert isinstance(actual["stdout"], str) and actual["stdout"].startswith("café")
            assert actual["stderr"].startswith("prob")
            assert ("truncated" in actual["stderr"]) == (limit == 4)
            assert actual["timed_out"] == (ending == "timeout")
            if ending == "nonzero":
                assert actual["error"] == "Command exited with status 7: problem"
            if ending == "nonzero-truncated":
                assert actual["error"].startswith("Command exited with status 7: prob\n")
                assert "stderr truncated: 4 characters omitted" in actual["error"]
            if ending == "timeout":
                assert actual["exit_code"] is None
                assert actual["error"] == "Command timed out after 7 seconds"
    finally:
        helper._egress_auth_token = None
        helper.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_actual_worker_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environment = CallerProcessOSEnvironment(
        spec=OSEnvSpec(
            type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="linux_bwrap")
        ),
        cwd=tmp_path,
        sandbox=_policy(tmp_path),
        shell_path="/bin/sh",
        _relaunch_when_stale=True,
    )
    started, cancelled, retirement = (threading.Event() for _ in range(3))
    request = shell_request.BwrapShellRequest()
    cancel = MagicMock(side_effect=cancelled.set)
    monkeypatch.setattr(request, "cancel", cancel)
    monkeypatch.setattr(shell_request, "BwrapShellRequest", lambda: request)

    def run(*_args: object) -> dict[str, object]:
        started.set()
        assert retirement.wait(timeout=5)
        return {"stdout": "retired", "exit_code": 0}

    monkeypatch.setattr(environment._helper, "shell_request", run)
    task = asyncio.create_task(environment.isolated_shell("example-command"))
    try:
        await poll_until_async(started.is_set, timeout_s=2, interval_s=0.001)
        task.cancel()
        await poll_until_async(cancelled.is_set, timeout_s=2, interval_s=0.001)
        task.cancel()
        assert not task.done()
        retirement.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        cancel.assert_called_once()
    finally:
        retirement.set()
        await asyncio.gather(task, return_exceptions=True)
        environment.close()


@pytest.mark.asyncio
async def test_close_cancels_the_active_request_and_retains_brokers_until_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = _HelperProcessClient(cwd=tmp_path, shell_path="/bin/sh", sandbox=_policy(tmp_path))
    helper._prepared = _PreparedHelper(_policy(tmp_path), tmp_path, {"PATH": os.defpath})
    monkeypatch.setattr(helper, "_ensure_started_locked", lambda: None)
    request = shell_request.BwrapShellRequest()
    started, cancelled, retire, stopped = (threading.Event() for _ in range(4))

    def run(**_kwargs: object) -> BwrapRequestResult:
        started.set()
        assert retire.wait(timeout=5)
        return BwrapRequestResult(CapturedOutput(""), CapturedOutput(""), 0, False, "utf-8")

    monkeypatch.setattr(request, "run", run)
    monkeypatch.setattr(request, "cancel", cancelled.set)
    stop = helper._stop_locked

    def stop_after_retirement() -> None:
        assert retire.is_set()
        stopped.set()
        stop()

    monkeypatch.setattr(helper, "_stop_locked", stop_after_retirement)
    result: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: result.append(helper.shell_request("example", 7, 100, request))
    )
    closing = threading.Thread(target=helper.close)
    worker.start()
    try:
        await poll_until_async(started.is_set, timeout_s=2, interval_s=0.001)
        closing.start()
        await poll_until_async(cancelled.is_set, timeout_s=2, interval_s=0.001)
        assert not stopped.is_set()
        assert closing.is_alive() and helper._prepared is not None
        retire.set()
        await poll_until_async(
            lambda: not worker.is_alive() and not closing.is_alive(), timeout_s=2, interval_s=0.001
        )
        assert stopped.is_set() and helper._prepared is None
        assert result[0]["exit_code"] == 0
    finally:
        retire.set()
        worker.join(timeout=5)
        if closing.ident is not None:
            closing.join(timeout=5)
        helper.close()
