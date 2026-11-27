"""Shell request wiring with process creation, seccomp, and pidfd signaling replaced."""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from omnigent.inner import _bwrap_shell_request as shell_request
from omnigent.inner import os_env as os_env_module
from omnigent.inner._bounded_output import CapturedOutput, _TextPrefix
from omnigent.inner._bwrap_request_control import BwrapRequestResult, RequestCancelled
from omnigent.inner._bwrap_shell_request import request_launcher
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
    [
        "platform",
        "inactive",
        "backend",
        "cow",
        "proxy",
        "missing",
        "relative",
        "flags",
        "kernel",
        "memfd",
    ],
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
    monkeypatch.setenv("PATH", "/selected/host-tools")
    selected = "relative/bwrap" if unsupported == "relative" else str(executable)
    which = MagicMock(return_value=None if unsupported == "missing" else selected)
    monkeypatch.setattr(shell_request.shutil, "which", which)
    probe = MagicMock(return_value=unsupported != "flags")
    monkeypatch.setattr(shell_request, "_has_request_options", probe)
    pidfds: list[int] = []
    memory_fds: list[int] = []
    create_memfd = shell_request.os.memfd_create

    def memfd_create(name: str, flags: int) -> int:
        if unsupported == "memfd":
            raise PermissionError("memfd denied")
        descriptor = create_memfd(name, flags)
        memory_fds.append(descriptor)
        return descriptor

    def pidfd_open(_pid: int) -> int:
        if unsupported == "kernel":
            raise OSError("pidfd unsupported")
        descriptor = os.open(os.devnull, os.O_RDONLY)
        pidfds.append(descriptor)
        return descriptor

    monkeypatch.setattr(shell_request.os, "pidfd_open", pidfd_open)
    memory = MagicMock(side_effect=memfd_create)
    monkeypatch.setattr(shell_request.os, "memfd_create", memory)
    monkeypatch.setattr(shell_request.signal, "pidfd_send_signal", MagicMock())
    assert shell_request.request_launcher(policy) is None
    if unsupported in {"flags", "kernel", "missing", "relative", "memfd"}:
        which.assert_called_with("bwrap", path="/selected/host-tools")
    if unsupported == "memfd":
        assert len(pidfds) == 1
        memory.assert_called_once()
        shell_request.signal.pidfd_send_signal.assert_called_once_with(pidfds[0], 0)
    for descriptor in pidfds + memory_fds:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_launcher_probe_uses_the_selected_executable_and_rechecks_replacements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launch_case: _LaunchCase
) -> None:
    case = launch_case
    executable = tmp_path / "bwrap"
    executable.write_text("first")
    monkeypatch.setenv("PATH", "/selected/host-tools")
    which = MagicMock(return_value=str(executable))
    monkeypatch.setattr(shell_request.shutil, "which", which)
    monkeypatch.setattr(shell_request, "request_launcher", request_launcher)
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
        for error in (OSError("temporary probe failure"), subprocess.TimeoutExpired("bwrap", 2)):
            run.side_effect = error
            assert request_launcher(_policy(tmp_path), executable=str(executable)) is None
        assert run.call_count == 2
        run.side_effect = None
        supported = run.return_value
        for status in (1, -signal.SIGTERM):
            shell_request._has_request_options.cache_clear()
            run.return_value = subprocess.CompletedProcess([], status, stdout="temporary failure")
            assert request_launcher(_policy(tmp_path), executable=str(executable)) is None
            run.return_value = supported
            assert request_launcher(_policy(tmp_path), executable=str(executable)) == str(
                executable
            )
        for _ in range(2):
            assert request_launcher(_policy(tmp_path)) == str(executable)
        which.assert_called_with("bwrap", path="/selected/host-tools")
        assert run.call_count == 6
        assert run.call_args.args[0] == [str(executable), "--help"]
        assert run.call_args.kwargs["env"] == {"PATH": os.defpath, "LANG": "C"}
        case.launcher = str(executable)
        case.request = shell_request.BwrapShellRequest(launcher=case.launcher)
        which.return_value = str(tmp_path / "command-tools" / "bwrap")
        _launch(case, tmp_path)
        assert which.call_count == 2
        assert case.spawn.call_args.args[0][0] == str(executable)
        assert case.spawn.call_args.args[1] == {"PATH": os.defpath, "LANG": "C"}
        assert run.call_count == 6
        executable.write_text("replacement with no request flags")
        run.return_value = subprocess.CompletedProcess([], 0, stdout="--unshare-pid")
        for _ in range(2):
            assert request_launcher(_policy(tmp_path), executable=str(executable)) is None
        assert run.call_count == 7
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
    launcher: str = "/selected/bwrap"


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
    owner.admitted = False
    configuration: dict[str, object] = {}
    descriptors: list[int] = []
    transfer: list[int] = []
    poll = MagicMock(return_value=[(1, 1)])
    monkeypatch.setattr(
        shell_request.select, "poll", lambda: SimpleNamespace(register=lambda *_: None, poll=poll)
    )
    monkeypatch.setattr(
        shell_request, "request_launcher", lambda *_args, **_kwargs: "/selected/bwrap"
    )
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
        descriptors.extend(fds)
        configuration.update(json.loads(os.pread(fds[1], 1_000_000, 0)))
        os.write(fds[2], b'{"child-pid": 23456}')
        assert argv[0] == case.launcher
        assert env == {"PATH": os.defpath, "LANG": "C"}
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
        owner.admitted = True
        os.close(transfer.pop())
        return BwrapRequestResult(b"done\r\n", b"", 0, False, "utf-8")

    owner.run.side_effect = run
    receive_mock = MagicMock(side_effect=receive)
    monkeypatch.setattr(shell_request.BwrapRequestControl, "receive", receive_mock)
    case = _LaunchCase(
        request,
        process,
        owner,
        receive_mock,
        poll,
        MagicMock(side_effect=spawn),
        configuration,
        descriptors,
    )
    return case


def _launch(case: _LaunchCase, cwd: Path) -> BwrapRequestResult:
    return case.request.run(
        argv=["/bin/sh", "-c", "printf done"],
        cwd=cwd,
        policy=replace(
            _policy(cwd), egress_relay_port=1234, egress_socket_path="/private/broker.sock"
        ),
        command_env={"HTTP_PROXY": "http://omnigent:broker-secret@127.0.0.1:1234"},
        timeout=7,
        max_output=17,
        output_encoding="ascii",
        spawn=case.spawn,
    )


@pytest.mark.parametrize("symlinked_import", [False, True])
def test_launch_delivers_private_config_and_preserves_the_relay_until_owner_completion(
    launch_case: _LaunchCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlinked_import: bool,
) -> None:
    case = launch_case
    if symlinked_import:
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        alias = tmp_path / "runtime-link"
        alias.symlink_to(runtime, target_is_directory=True)
        monkeypatch.setattr(shell_request, "__file__", str(alias / "_bwrap_shell_request.py"))
    assert _launch(case, tmp_path) == BwrapRequestResult(b"done\r\n", b"", 0, False, "utf-8")
    assert case.config["env"] == {"HTTP_PROXY": "http://omnigent:broker-secret@127.0.0.1:1234"}
    assert case.config["report_encoding"] is True
    assert case.config["output_encoding"] == "ascii"
    assert case.config["relay"] == {
        "source": str(Path(shell_request.__file__).resolve().with_name("egress") / "relay.py"),
        "port": 1234,
        "socket_path": "/private/broker.sock",
    }
    case.owner.run.assert_called_once()
    assert case.request.admitted
    assert case.owner.run.call_args.kwargs["max_output"] == 17
    case.process.communicate.assert_not_called()
    assert case.process.stdout.closed and case.process.stderr.closed
    for descriptor in case.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    "failure",
    [
        "unsupported",
        "readiness-timeout",
        "invalid-metadata",
        "empty-metadata",
        "malformed-metadata",
        "receive",
        "spawn",
        "unadmitted",
        "admission-lost",
        "cleanup-timeout",
        "launcher-pidfd",
    ],
)
def test_launch_failures_preserve_admission_and_retire_resources_without_retry(
    launch_case: _LaunchCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    case = launch_case
    signaling = MagicMock()
    monkeypatch.setattr(shell_request.signal, "pidfd_send_signal", signaling)
    if failure == "unsupported":
        monkeypatch.setattr(shell_request, "request_launcher", lambda *_args, **_kwargs: None)
    elif failure == "readiness-timeout":
        case.poll.return_value = []
    elif failure in {"invalid-metadata", "empty-metadata", "malformed-metadata"}:
        original = case.spawn.side_effect

        def invalid_metadata(
            argv: list[str], env: dict[str, str], fds: tuple[int, ...]
        ) -> MagicMock:
            result = original(argv, env, fds)
            os.ftruncate(fds[2], 0)
            payload = {
                "invalid-metadata": b'{"child-pid": 0}',
                "empty-metadata": b"",
                "malformed-metadata": b"{",
            }[failure]
            os.pwrite(fds[2], payload, 0)
            return result

        case.spawn.side_effect = invalid_metadata
    elif failure == "receive":
        case.receive.side_effect = OSError("readiness rejected")
    elif failure in {"cleanup-timeout", "launcher-pidfd"}:
        case.poll.return_value = []
        case.process.communicate.side_effect = [
            subprocess.TimeoutExpired("fixture", 1),
            (b"", b""),
        ]
        if failure == "launcher-pidfd":
            monkeypatch.setattr(
                shell_request.os,
                "pidfd_open",
                MagicMock(side_effect=OSError("launcher pidfd unavailable")),
            )
    elif failure in {"unadmitted", "admission-lost"}:
        original_run = case.owner.run.side_effect

        def failed_run(*args: object, **kwargs: object) -> None:
            original_run(*args, **kwargs)
            case.owner.admitted = failure == "admission-lost"
            raise OSError("admission failed")

        case.owner.run.side_effect = failed_run
    else:
        case.spawn.side_effect = OSError("spawn failed")
    if failure in {"readiness-timeout", "cleanup-timeout"}:
        ticks = iter((0.0, 0.0, shell_request._STARTUP_TIMEOUT_S))
        monkeypatch.setattr(shell_request, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(OSError) as caught:
        _launch(case, tmp_path)
    if failure in {"empty-metadata", "malformed-metadata"}:
        assert str(caught.value) == (
            "Shell request launcher did not publish valid startup metadata; "
            "check Bubblewrap startup and sandbox support"
        )
        case.receive.assert_not_called()
    assert case.request.admitted == (failure == "admission-lost")
    if failure == "unsupported":
        case.spawn.assert_not_called()
    else:
        case.spawn.assert_called_once()
    if failure in {"unadmitted", "admission-lost"}:
        case.owner.run.assert_called_once()
    else:
        case.owner.run.assert_not_called()
    if failure in {
        "readiness-timeout",
        "invalid-metadata",
        "empty-metadata",
        "malformed-metadata",
        "receive",
    }:
        case.process.communicate.assert_called_once_with(timeout=1)
    if failure in {"cleanup-timeout", "launcher-pidfd"}:
        assert case.process.communicate.call_args_list == [call(timeout=1), call()]
        if failure == "launcher-pidfd":
            case.process.kill.assert_called_once_with()
            signaling.assert_not_called()
        else:
            descriptor = signaling.call_args.args[0]
            assert descriptor in case.descriptors
            signaling.assert_called_once_with(descriptor, shell_request.signal.SIGKILL)
            case.process.kill.assert_not_called()
    else:
        signaling.assert_not_called()
        case.process.kill.assert_not_called()
    if failure not in {"spawn", "unsupported"}:
        assert case.process.stdout.closed and case.process.stderr.closed
    for descriptor in case.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("phase", ["before-launch", "after-spawn", "during-receive"])
def test_cancelled_request_never_reaches_spawn(
    launch_case: _LaunchCase, tmp_path: Path, phase: str
) -> None:
    case = launch_case
    if phase == "before-launch":
        case.request.cancel()
    else:
        operation = case.spawn if phase == "after-spawn" else case.receive
        original = operation.side_effect

        def cancel_before_attachment(*args: object, **kwargs: object) -> MagicMock:
            result = original(*args, **kwargs)
            case.request.cancel()
            case.owner.cancel.assert_not_called()
            return result

        operation.side_effect = cancel_before_attachment

        def refuse_admission(*_args: object, **_kwargs: object) -> None:
            case.owner.cancel.assert_called_once_with()
            assert not case.owner.admitted
            os.close(case.receive.call_args.kwargs["launcher_pidfd"])
            raise RequestCancelled("cancelled before admission")

        case.owner.run.side_effect = refuse_admission
    with pytest.raises(RequestCancelled):
        _launch(case, tmp_path)
    assert not case.request.admitted
    if phase in {"before-launch", "after-spawn"}:
        if phase == "before-launch":
            case.spawn.assert_not_called()
        else:
            case.spawn.assert_called_once()
            case.process.communicate.assert_called_once_with(timeout=1)
        case.receive.assert_not_called()
        case.owner.run.assert_not_called()
    else:
        case.spawn.assert_called_once()
        case.receive.assert_called_once()
        case.owner.run.assert_called_once()
    if phase != "before-launch":
        assert case.process.stdout.closed and case.process.stderr.closed
        for descriptor in case.descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)


def test_startup_cancellation_retires_the_unadmitted_launcher_promptly(
    launch_case: _LaunchCase, tmp_path: Path
) -> None:
    case = launch_case
    waiting, release = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def silent_readiness(timeout_ms: int) -> list[tuple[int, int]]:
        waiting.set()
        release.wait(timeout_ms / 1000)
        return []

    def launch() -> None:
        try:
            _launch(case, tmp_path)
        except BaseException as exc:
            failures.append(exc)

    case.poll.side_effect = silent_readiness
    worker = threading.Thread(target=launch, daemon=True)
    worker.start()
    try:
        assert waiting.wait(2), "The request did not enter startup readiness"
        case.request.cancel()
        worker.join(2)
        assert not worker.is_alive(), "Cancellation remained blocked on startup readiness"
    finally:
        release.set()
        worker.join(2)
    assert len(failures) == 1 and isinstance(failures[0], RequestCancelled)
    assert not case.request.admitted
    case.receive.assert_not_called()
    case.owner.run.assert_not_called()
    case.process.communicate.assert_called_once_with(timeout=1)
    assert case.process.stdout.closed and case.process.stderr.closed
    for descriptor in case.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    "ending",
    [
        "success",
        "nonzero",
        "nonzero-truncated",
        "nonzero-discarded-tail",
        "timeout",
        "not-admitted",
        "lost",
        "decode",
        "timeout-invalid",
        "invalid-locale",
    ],
)
def test_helper_uses_only_prepared_environment_and_preserves_shell_result_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ending: str
) -> None:
    helper = _HelperProcessClient(cwd=tmp_path, shell_path="/bin/sh", sandbox=_policy(tmp_path))
    prepared_env = {
        "PATH": os.defpath,
        "HTTP_PROXY": "http://127.0.0.1:1234",
        "PYTHONPATH": str(os_env_module._project_root()),
        "LC_CTYPE": "C",
    }
    tcp_broker = MagicMock()
    tcp_broker.socket_path = tmp_path / "tcp-egress.sock"
    tcp_broker.token = "fixture-token"
    os_env_module.apply_tcp_egress_env(prepared_env, tcp_broker)
    prepared = _PreparedHelper(
        replace(_policy(tmp_path), egress_relay_port=1234, egress_socket_path="/broker.sock"),
        tmp_path,
        prepared_env,
    )
    monkeypatch.setenv("UNGRANTED_SECRET", "must-stay-in-the-parent")
    monkeypatch.setenv("LC_CTYPE", "C.UTF-8")
    helper._prepared = prepared
    helper._egress_auth_token = "broker-secret"
    start = MagicMock()
    monkeypatch.setattr(helper, "_ensure_started_locked", start)
    request = shell_request.BwrapShellRequest()
    encoding = "utf-8" if ending in {"decode", "timeout-invalid"} else "iso8859-1"
    monkeypatch.setattr(
        os_env_module, "sys", SimpleNamespace(flags=SimpleNamespace(utf8_mode=encoding == "utf-8"))
    )
    monkeypatch.setattr(os_env_module.locale, "getencoding", lambda: encoding)
    metadata = MagicMock(
        side_effect=lambda *_args: os_env_module._handle_helper_request(
            request={"op": "shell_encoding"},
            cwd=tmp_path,
            shell_path="/bin/sh",
            sandbox=prepared.sandbox,
        )
    )
    monkeypatch.setattr(helper, "_read_shell_metadata_locked", metadata)
    if ending == "invalid-locale":
        metadata.side_effect = None
        metadata.return_value = {"encoding": encoding, "locale": {"UNGRANTED_SECRET": "fixture"}}
    limit = 16 if ending == "nonzero" else 4

    def capture(raw: bytes) -> CapturedOutput:
        output = _TextPrefix(encoding, limit)
        output.feed(raw, final=True)
        return output.result()

    result = BwrapRequestResult(
        capture(b"caf\xe9\r\n"),
        capture(b"    problem\r" if ending == "nonzero-discarded-tail" else b"problem\r"),
        None if ending.startswith("timeout") else 7 if ending.startswith("nonzero") else 0,
        ending.startswith("timeout"),
        encoding,
    )
    run = MagicMock(return_value=result)
    if ending == "not-admitted":
        run.side_effect = OSError("request did not start")
    elif ending == "lost":
        request._admitted = True
        run.side_effect = OSError("result lost after admission")
    monkeypatch.setattr(request, "run", run)
    try:
        actual = helper.shell_request("printf result", 7, limit, request)
        if ending == "invalid-locale":
            assert actual == {"error": "Failed to read the OS helper's shell locale and encoding"}
            run.assert_not_called()
            return
        run.assert_called_once()
        options = run.call_args.kwargs
        assert options["policy"] is prepared.sandbox
        assert options["cwd"] == tmp_path
        assert options["max_output"] == limit
        assert options["output_encoding"] == encoding
        assert options["command_env"]["LC_CTYPE"] == "C.UTF-8"
        assert prepared.env["LC_CTYPE"] == "C"
        metadata.assert_called_once_with(request, 7)
        assert "launch_env" not in options
        assert options["command_env"]["PATH"] == prepared.env["PATH"]
        assert prepared.env["PATH"].startswith(str(tmp_path) + os.pathsep)
        assert "UNGRANTED_SECRET" not in options["command_env"]
        assert "PYTHONPATH" not in options["command_env"]
        assert (
            options["command_env"]["HTTP_PROXY"] == "http://omnigent:broker-secret@127.0.0.1:1234"
        )
        assert prepared.env["HTTP_PROXY"] == "http://127.0.0.1:1234"
        if ending == "not-admitted":
            assert actual == {
                "error": "Failed to start isolated shell request: request did not start"
            }
        elif ending == "lost":
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
            assert actual["stderr"].startswith(
                "    " if ending == "nonzero-discarded-tail" else "prob"
            )
            assert ("truncated" in actual["stderr"]) == (limit == 4)
            assert actual["timed_out"] == (ending == "timeout")
            if ending == "nonzero":
                assert actual["error"] == "Command exited with status 7: problem"
            if ending == "nonzero-truncated":
                assert actual["error"].startswith("Command exited with status 7: prob\n")
                assert "stderr truncated: 4 characters omitted" in actual["error"]
            if ending == "nonzero-discarded-tail":
                assert result.stderr.nonblank and not result.stderr.text.strip()
                assert actual["error"].startswith(
                    "Command exited with status 7: [stderr truncated:"
                )
                assert "café" not in actual["error"]
            if ending == "timeout":
                assert actual["exit_code"] is None
                assert actual["error"] == "Command timed out after 7 seconds"
        if ending == "success":
            warm_request = shell_request.BwrapShellRequest()
            monkeypatch.setattr(warm_request, "run", run)
            assert helper.shell_request("printf result", 7, limit, warm_request) == actual
            assert metadata.call_count == 1 and run.call_count == 2
            assert run.call_args.kwargs["output_encoding"] == encoding
            assert run.call_args.kwargs["command_env"]["LC_CTYPE"] == "C.UTF-8"

            helper._stop_locked()
            assert helper._prepared is None
            helper._prepared = replace(prepared, env={**prepared.env, "PYTHONUTF8": "1"})
            metadata.side_effect = None
            metadata.return_value = {
                "encoding": "utf-8",
                "locale": {"LC_CTYPE": "C"},
            }
            output = _TextPrefix("utf-8", limit)
            output.feed("λok".encode(), final=True)
            run.return_value = BwrapRequestResult(
                output.result(), CapturedOutput(""), 0, False, "utf-8"
            )
            restarted_request = shell_request.BwrapShellRequest()
            monkeypatch.setattr(restarted_request, "run", run)
            refreshed = helper.shell_request("printf result", 7, limit, restarted_request)
            assert refreshed["stdout"] == "λok" and refreshed["exit_code"] == 0
            assert metadata.call_count == 2 and run.call_count == 3
            assert metadata.call_args == call(restarted_request, 7)
            assert run.call_args.kwargs["output_encoding"] == "utf-8"
            assert os_env_module._shell_locale_env(run.call_args.kwargs["command_env"]) == {
                "LC_CTYPE": "C",
            }
            assert run.call_args.kwargs["command_env"]["PYTHONUTF8"] == "1"
    finally:
        helper._egress_auth_token = None
        helper.close()


@pytest.fixture
def caller_environment(tmp_path: Path) -> Iterator[CallerProcessOSEnvironment]:
    environment = CallerProcessOSEnvironment(
        spec=OSEnvSpec(
            type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="linux_bwrap")
        ),
        cwd=tmp_path,
        sandbox=_policy(tmp_path),
        shell_path="/bin/sh",
        _relaunch_when_stale=True,
    )
    try:
        yield environment
    finally:
        environment.close()


@pytest.mark.parametrize(
    "arguments, mode, error",
    [
        pytest.param({"command": None}, None, "command", id="missing-command"),
        pytest.param({"command": 17}, None, "command", id="non-string-command"),
        pytest.param({"command": ""}, None, "command", id="empty-command"),
        pytest.param({"command": " \n\t"}, None, "command", id="blank-command"),
        pytest.param({"timeout": 0}, None, "timeout", id="zero-timeout"),
        pytest.param({"timeout": -1}, None, "timeout", id="negative-timeout"),
        pytest.param({"max_output": 0}, None, "max_output", id="zero-output-limit"),
        pytest.param({"max_output": -1}, None, "max_output", id="negative-output-limit"),
        pytest.param({"max_output": 1.5}, None, "max_output", id="fractional-output-limit"),
        pytest.param({"max_output": "3"}, None, "max_output", id="non-integer-output-limit"),
        pytest.param({}, "no-relaunch", "does not support", id="no-relaunch"),
        pytest.param({}, "fork", "does not support", id="fork"),
        pytest.param({}, "scratch", "does not support", id="scratch"),
        pytest.param({}, "copy-on-write", "does not support", id="copy-on-write"),
    ],
)
@pytest.mark.asyncio
async def test_isolated_shell_rejects_invalid_requests_before_starting_a_worker(
    monkeypatch: pytest.MonkeyPatch,
    caller_environment: CallerProcessOSEnvironment,
    arguments: dict[str, Any],
    mode: str | None,
    error: str,
) -> None:
    environment = caller_environment
    if mode == "no-relaunch":
        environment._relaunch_when_stale = False
    elif mode == "fork":
        environment.spec.fork = True
    elif mode == "scratch":
        environment._start_in_scratch = True
    elif mode == "copy-on-write":
        environment._copy_on_write_environment = MagicMock()
    worker = MagicMock(side_effect=AssertionError("A rejected request started a worker"))
    request = MagicMock(side_effect=AssertionError("A rejected request created an owner"))
    monkeypatch.setattr(os_env_module, "threading", SimpleNamespace(Thread=worker))
    monkeypatch.setattr(shell_request, "BwrapShellRequest", request)
    result = await environment.isolated_shell(**({"command": "example-command"} | arguments))
    assert error in result["error"]
    worker.assert_not_called()
    request.assert_not_called()


@pytest.mark.parametrize(
    "timeout, max_output, expected_timeout, expected_limit",
    [
        (None, None, 120, os_env_module._MAX_TOOL_OUTPUT_CHARS),
        (7, None, 7, os_env_module._MAX_TOOL_OUTPUT_CHARS),
        (None, 3, 120, 3),
        (7, 5_000_000, 7, 5_000_000),
        (7, 5_000_001, 7, 5_000_000),
    ],
    ids=["defaults", "explicit-timeout", "explicit-limit", "maximum-limit", "capped-limit"],
)
@pytest.mark.asyncio
async def test_isolated_shell_forwards_validated_limits_to_its_owned_worker(
    monkeypatch: pytest.MonkeyPatch,
    caller_environment: CallerProcessOSEnvironment,
    timeout: int | None,
    max_output: int | None,
    expected_timeout: int,
    expected_limit: int,
) -> None:
    request = shell_request.BwrapShellRequest()
    constructor = MagicMock(return_value=request)
    monkeypatch.setattr(shell_request, "BwrapShellRequest", constructor)
    expected = {"stdout": "result", "exit_code": 0}
    run = MagicMock(return_value=expected)
    monkeypatch.setattr(caller_environment._helper, "shell_request", run)
    result = await caller_environment.isolated_shell(
        "example-command", timeout, max_output, launcher="/selected/bwrap"
    )
    assert result == expected
    constructor.assert_called_once_with(launcher="/selected/bwrap")
    run.assert_called_once_with("example-command", expected_timeout, expected_limit, request)


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_actual_worker_retirement(
    monkeypatch: pytest.MonkeyPatch, caller_environment: CallerProcessOSEnvironment
) -> None:
    environment = caller_environment
    started, cancelled, retirement = (threading.Event() for _ in range(3))
    request = shell_request.BwrapShellRequest()
    cancel = MagicMock(side_effect=cancelled.set)
    monkeypatch.setattr(request, "cancel", cancel)
    constructor = MagicMock(return_value=request)
    monkeypatch.setattr(shell_request, "BwrapShellRequest", constructor)

    def run(*_args: object) -> dict[str, object]:
        started.set()
        assert retirement.wait(timeout=5)
        return {"stdout": "retired", "exit_code": 0}

    monkeypatch.setattr(environment._helper, "shell_request", run)
    task = asyncio.create_task(
        environment.isolated_shell("example-command", launcher="/selected/bwrap")
    )
    try:
        await poll_until_async(started.is_set, timeout_s=2, interval_s=0.001)
        constructor.assert_called_once_with(launcher="/selected/bwrap")
        task.cancel()
        await poll_until_async(cancelled.is_set, timeout_s=2, interval_s=0.001)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        retirement.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        cancel.assert_called_once()
    finally:
        retirement.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending",
    [
        "cancel",
        "close",
        "timeout",
        "partial-timeout",
        "write-cancel",
        "malformed-json",
        "nonobject-json",
        "multiple-frames",
        "oversized",
        "eof",
    ],
)
async def test_metadata_preparation_failures_retire_before_any_command(
    monkeypatch: pytest.MonkeyPatch,
    caller_environment: CallerProcessOSEnvironment,
    ending: str,
) -> None:
    environment = caller_environment
    helper = environment._helper
    peer_input, parent_input = os.pipe()
    parent_output, peer_output = os.pipe()
    process = MagicMock(spec=subprocess.Popen)
    process.stdin = os.fdopen(parent_input, "w", encoding="utf-8")
    process.stdout = os.fdopen(parent_output, "r", encoding="utf-8")
    process.stderr = io.StringIO()
    process.poll.return_value = None
    retired, waiting = threading.Event(), threading.Event()

    def terminate() -> None:
        process.poll.return_value = 0

    def reap(*_args: object, **_kwargs: object) -> int:
        assert process.poll() == 0
        retired.set()
        return 0

    process.terminate.side_effect = terminate
    process.wait.side_effect = reap
    execute = MagicMock(side_effect=AssertionError("Metadata rejection must precede execution"))
    monkeypatch.setattr(shell_request.BwrapShellRequest, "run", execute)
    helper._proc = process
    helper._prepared = _PreparedHelper(helper.sandbox, helper.cwd, {"PATH": os.defpath})
    monkeypatch.setattr(helper, "_ensure_started_locked", lambda: None)
    wait = os_env_module._wait_shell_metadata_pipe

    def observe(
        descriptor: int, event: int, deadline: float, request: shell_request.BwrapShellRequest
    ) -> None:
        if descriptor == (parent_input if ending == "write-cancel" else parent_output):
            waiting.set()
        wait(descriptor, event, deadline, request)

    monkeypatch.setattr(os_env_module, "_wait_shell_metadata_pipe", observe)
    if "timeout" in ending:
        monkeypatch.setattr(os_env_module, "_SHELL_METADATA_TIMEOUT_S", 0.1)
    if ending == "write-cancel":
        os.set_blocking(parent_input, False)
        try:
            while True:
                os.write(parent_input, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(parent_input, True)
    broker_stop = MagicMock(
        side_effect=lambda: retired.is_set() or pytest.fail("broker retired before helper")
    )
    monkeypatch.setattr(helper, "_stop_egress_proxy_locked", broker_stop)
    closing = None
    peer_open = True
    expected: type[BaseException]
    task = asyncio.create_task(environment.isolated_shell("must-not-run", timeout=7))
    try:
        await poll_until_async(waiting.is_set, timeout_s=2, interval_s=0.001)
        if ending != "write-cancel":
            assert json.loads(os.read(peer_input, 4096)) == {"op": "shell_encoding"}
        if ending == "partial-timeout":
            os.write(peer_output, b'{"encoding":')
        if ending in {"malformed-json", "nonobject-json", "multiple-frames", "oversized"}:
            reply = {
                "malformed-json": b"{invalid}\n",
                "nonobject-json": b"[]\n",
                "multiple-frames": b"{}\n{}\n",
                "oversized": b"x" * os_env_module._MAX_SHELL_METADATA_BYTES + b"\n",
            }[ending]
            written = await asyncio.wait_for(asyncio.to_thread(os.write, peer_output, reply), 2)
            assert written == len(reply)
            expected = ValueError if ending == "malformed-json" else OSError
        elif ending == "eof":
            os.close(peer_output)
            peer_open = False
            expected = OSError
        elif ending == "close":
            closing = threading.Thread(target=environment.close)
            closing.start()
            expected = RequestCancelled
        elif "cancel" in ending:
            task.cancel()
            expected = asyncio.CancelledError
        else:
            expected = TimeoutError
        with pytest.raises(expected):
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        if peer_open:
            os.fstat(peer_output)
        assert retired.is_set()
        assert helper._proc is None and helper._prepared is None
        assert process.stdin.closed and process.stdout.closed
        assert broker_stop.call_count >= 1
        execute.assert_not_called()
        if closing is not None:
            await poll_until_async(lambda: not closing.is_alive(), timeout_s=2, interval_s=0.001)
    finally:
        os.close(peer_input)
        if peer_open:
            os.close(peer_output)
        await asyncio.gather(task, return_exceptions=True)
        if closing is not None:
            closing.join(timeout=2)


@pytest.mark.asyncio
async def test_close_cancels_the_active_request_and_retains_brokers_until_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = _HelperProcessClient(cwd=tmp_path, shell_path="/bin/sh", sandbox=_policy(tmp_path))
    helper._prepared = _PreparedHelper(_policy(tmp_path), tmp_path, {"PATH": os.defpath})
    helper._shell_metadata = ("utf-8", {})
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
