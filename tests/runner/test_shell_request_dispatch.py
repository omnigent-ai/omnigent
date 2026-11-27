"""Reusable shell dispatch, conservative fallback, and real Linux request acceptance."""

from __future__ import annotations

import asyncio
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from omnigent.inner import os_env as os_env_module
from omnigent.inner._bwrap_shell_request import BwrapShellRequest
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import CallerProcessOSEnvironment, OSEnvironment, _HelperProcessClient
from omnigent.inner.sandbox import SandboxPolicy, resolve_sandbox
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.tool_dispatch import _execute_os_env_tool
from omnigent.spec.types import AgentSpec
from tests._helpers.polling import poll_until_async
from tests._helpers.shell_lifecycle import detached_output_command

_LIVE_LINUX = (
    sys.platform.startswith("linux") and shutil.which("bwrap", path=os.defpath) is not None
)


def _spec(cwd: Path, *, write_paths: list[str] | None = None) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="linux_bwrap", write_paths=write_paths),
        ),
    )


def _fake_environments(monkeypatch: pytest.MonkeyPatch, cwd: Path) -> list[Mock]:
    environments: list[Mock] = []

    def create(*_args: object, **_kwargs: object) -> Mock:
        environment = Mock(spec=OSEnvironment)
        environment.cwd = cwd
        environment.read = AsyncMock(return_value={"content": "file"})
        environment.shell = AsyncMock(return_value={"stdout": "disposable", "exit_code": 0})
        environment.isolated_shell = AsyncMock(return_value={"stdout": "isolated", "exit_code": 0})
        environments.append(environment)
        return environment

    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", create)
    monkeypatch.setattr(
        "omnigent.runner.resource_registry.resolve_sandbox",
        lambda *_: SandboxPolicy(
            backend_type="linux_bwrap",
            active=True,
            read_roots=None,
            write_roots=[cwd],
            write_files=[],
            allow_network=False,
        ),
    )
    monkeypatch.setattr(
        "omnigent.inner._bwrap_shell_request.request_launcher", lambda *_: "/supported/bwrap"
    )
    return environments


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["success", "nonzero", "timeout", "lost", "raised", "invalid-command", "invalid-timeout"],
)
async def test_supported_shells_lease_the_file_environment_and_retire_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    environments = _fake_environments(monkeypatch, tmp_path)
    registry = SessionResourceRegistry()
    execute = partial(
        _execute_os_env_tool,
        agent_spec=_spec(tmp_path),
        conversation_id="shell-request",
        resource_registry=registry,
    )
    try:
        assert json.loads(await execute("sys_os_read", {"path": "note.txt"})) == {
            "content": "file"
        }
        environment = environments[0]
        results = {
            "success": {"stdout": "isolated", "exit_code": 0},
            "nonzero": {"error": "command failed", "exit_code": 7},
            "timeout": {"error": "timed out", "timed_out": True},
            "lost": {"error": "reply lost; command was not sent again"},
            "invalid-command": {"error": "command must be a non-empty string"},
            "invalid-timeout": {"error": "timeout must be >= 1"},
        }
        if outcome == "raised":
            environment.isolated_shell.side_effect = OSError("request failed")
        else:
            environment.isolated_shell.return_value = results[outcome]
        result = json.loads(
            await execute(
                "sys_os_shell",
                {
                    "command": " " if outcome == "invalid-command" else "example",
                    "timeout": 0 if outcome == "invalid-timeout" else 7,
                },
            )
        )
        assert result == ({"error": "request failed"} if outcome == "raised" else results[outcome])
        if outcome.startswith("invalid-"):
            environment.isolated_shell.assert_not_awaited()
        else:
            environment.isolated_shell.assert_awaited_once_with(
                command="example", timeout=7, launcher="/supported/bwrap"
            )
        environment.shell.assert_not_awaited()
        assert len(environments) == 1
        assert json.loads(await execute("sys_os_read", {"path": "note.txt"})) == {
            "content": "file"
        }
        if outcome in {"success", "nonzero", "invalid-command", "invalid-timeout"}:
            assert len(environments) == 1
            assert environment.read.await_count == 2
            environment.close.assert_not_called()
        else:
            assert len(environments) == 2
            environment.close.assert_called_once_with()
    finally:
        await registry.cleanup_session("shell-request")
    for environment in environments:
        environment.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_a_busy_file_environment_keeps_its_lease_and_the_shell_uses_a_disposable_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environments = _fake_environments(monkeypatch, tmp_path)
    registry = SessionResourceRegistry()
    spec = _spec(tmp_path)
    assert spec.os_env is not None
    held = await registry.lease_tool_environment("busy-shell", spec.os_env)
    assert held is not None
    try:
        result = await _execute_os_env_tool(
            "sys_os_shell",
            {"command": "example"},
            agent_spec=spec,
            conversation_id="busy-shell",
            resource_registry=registry,
        )
        assert json.loads(result) == {"stdout": "disposable", "exit_code": 0}
        assert len(environments) == 2
        environments[0].close.assert_not_called()
        environments[0].shell.assert_not_awaited()
        environments[0].isolated_shell.assert_not_awaited()
        environments[1].shell.assert_awaited_once()
        environments[1].close.assert_called_once_with()
    finally:
        await held.release(reusable=True)
        await registry.cleanup_session("busy-shell")


@pytest.mark.asyncio
async def test_claude_sdk_shell_applies_credential_isolation_before_the_reusable_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environments = _fake_environments(monkeypatch, tmp_path)
    access = object()
    resolve = Mock(return_value=access)
    monkeypatch.setattr("omnigent.runtime.workflow.claude_terminal_model_access", resolve)
    registry = SessionResourceRegistry()
    spec = _spec(tmp_path)
    assert spec.os_env is not None
    lease = await registry.lease_tool_environment("sdk-shell", spec.os_env)
    assert lease is not None
    await lease.release(reusable=True)
    environment = environments[0]

    async def isolated_shell(**_kwargs: object) -> dict[str, object]:
        environment.isolate_model_credentials.assert_called_once_with(access)
        return {"stdout": "isolated", "exit_code": 0}

    environment.isolated_shell.side_effect = isolated_shell
    try:
        result = await _execute_os_env_tool(
            "sys_os_shell",
            {"command": "example"},
            agent_spec=spec,
            conversation_id="sdk-shell",
            resource_registry=registry,
            effective_harness="claude-sdk",
        )
        assert json.loads(result) == {"stdout": "isolated", "exit_code": 0}
        resolve.assert_called_once()
        assert len(environments) == 1
        environment.close.assert_not_called()
    finally:
        await registry.cleanup_session("sdk-shell")


async def _helper_process(
    registry: SessionResourceRegistry, spec: AgentSpec
) -> subprocess.Popen[str]:
    assert spec.os_env is not None
    lease = await registry.lease_tool_environment("live-shell", spec.os_env)
    assert lease is not None
    try:
        assert isinstance(lease.environment, CallerProcessOSEnvironment)
        process = lease.environment._helper._proc
        assert process is not None
        return process
    finally:
        await lease.release(reusable=True)


def _require_request_launcher(spec: AgentSpec, cwd: Path) -> None:
    from omnigent.inner._bwrap_shell_request import request_launcher

    assert spec.os_env is not None
    policy = resolve_sandbox(spec.os_env, cwd)
    if request_launcher(policy) is None:
        pytest.skip("Kernel or Bubblewrap requires the disposable shell compatibility path")


@pytest.mark.skipif(not _LIVE_LINUX, reason="requires Linux and Bubblewrap")
@pytest.mark.parametrize("ending", ["complete", "nonzero", "timeout", "cancel", "metadata-cancel"])
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_real_runner_shell_requests_retire_descendants_and_keep_only_clean_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ending: str
) -> None:
    from omnigent.inner import _bwrap_request_control as request_control

    spec = _spec(tmp_path, write_paths=["."])
    _require_request_launcher(spec, tmp_path)
    (tmp_path / "note.txt").write_text("ready\n")
    registry = SessionResourceRegistry()
    execute = partial(
        _execute_os_env_tool,
        agent_spec=spec,
        conversation_id="live-shell",
        resource_registry=registry,
    )
    lifetimes: list[tuple[int, int, request_control.BwrapRequestControl]] = []
    receive = request_control.BwrapRequestControl.receive

    def observe(*args: object, **kwargs: object) -> request_control.BwrapRequestControl:
        owner = receive(*args, **kwargs)
        lifetimes.append((os.dup(owner._init_pidfd), os.dup(owner._launcher_pidfd), owner))
        return owner

    monkeypatch.setattr(request_control.BwrapRequestControl, "receive", observe)
    ready = tmp_path / "descendant-ready"
    stopped: list[int] = []
    task = None
    try:
        assert (
            json.loads(await execute("sys_os_read", {"path": "note.txt"}))["content"] == "ready\n"
        )
        original = await _helper_process(registry, spec)
        if ending == "metadata-cancel":
            assert original.stdout is not None
            output_fd = original.stdout.fileno()
            tree = psutil.Process(original.pid)
            for process in [*tree.children(recursive=True), tree]:
                descriptor = os.pidfd_open(process.pid)
                stopped.append(descriptor)
                signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
            waiting = threading.Event()
            wait = os_env_module._wait_shell_metadata_pipe

            def observe_metadata(
                descriptor: int, event: int, deadline: float, request: BwrapShellRequest
            ) -> None:
                if descriptor == output_fd and event == select.POLLIN:
                    waiting.set()
                wait(descriptor, event, deadline, request)

            monkeypatch.setattr(os_env_module, "_wait_shell_metadata_pipe", observe_metadata)
            task = asyncio.create_task(
                execute("sys_os_shell", {"command": "printf must-not-run", "timeout": 7})
            )
            await poll_until_async(waiting.is_set, timeout_s=5, interval_s=0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert not lifetimes
            await poll_until_async(
                lambda: all(request_control._pidfd_exited(descriptor) for descriptor in stopped),
                timeout_s=5,
                interval_s=0.01,
            )
            assert original.poll() is not None
            assert (
                json.loads(await execute("sys_os_read", {"path": "note.txt"}))["content"]
                == "ready\n"
            )
            assert await _helper_process(registry, spec) is not original
            return
        task = asyncio.create_task(
            execute(
                "sys_os_shell",
                {
                    "command": shlex.join(
                        detached_output_command(
                            ready,
                            "complete" if ending == "nonzero" else ending,
                            exit_code=7 if ending == "nonzero" else 0,
                            ready_timeout=8,
                        )
                    ),
                    "timeout": 3 if ending == "timeout" else 15,
                },
            )
        )
        await poll_until_async(
            lambda: ready.exists() or task.done(), timeout_s=10, interval_s=0.01
        )
        if not ready.exists():
            pytest.fail(f"Descendant did not publish readiness: {await task}")
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = json.loads(await task)
            assert result["timed_out"] == (ending == "timeout")
            assert result["exit_code"] == (
                0 if ending == "complete" else 7 if ending == "nonzero" else None
            )
            assert "descendant-ready\n" in result["stdout"]
            assert "descendant-stderr\n" in result["stderr"]
        assert len(lifetimes) == 1
        for init_fd, launcher_fd, owner in lifetimes:
            assert owner.done.is_set()
            assert request_control._pidfd_exited(init_fd)
            assert request_control._pidfd_exited(launcher_fd)
        assert (
            json.loads(await execute("sys_os_read", {"path": "note.txt"}))["content"] == "ready\n"
        )
        current = await _helper_process(registry, spec)
        if ending in {"complete", "nonzero"}:
            assert current is original and original.poll() is None
        else:
            assert current is not original
            await poll_until_async(
                lambda: original.poll() is not None, timeout_s=5, interval_s=0.01
            )
    finally:
        for descriptor in stopped:
            if not request_control._pidfd_exited(descriptor):
                signal.pidfd_send_signal(descriptor, signal.SIGCONT)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await registry.cleanup_session("live-shell")
        for init_fd, launcher_fd, _owner in lifetimes:
            os.close(init_fd)
            os.close(launcher_fd)
        for descriptor in stopped:
            os.close(descriptor)


@pytest.mark.skipif(not _LIVE_LINUX, reason="requires Linux and Bubblewrap")
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_real_claude_sdk_requests_have_private_relays_and_parent_owned_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.inner import _bwrap_shell_request as shell_request
    from omnigent.inner.claude_model_access import ClaudeModelAccess

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "synthetic-credential.json"
    source.write_text("synthetic-parent-only-secret")
    (workspace / "note.txt").write_text("ready\n")
    spec = _spec(workspace)
    canonical_module = Path(shell_request.__file__).resolve()
    alias = tmp_path / "runtime-link"
    alias.symlink_to(canonical_module.parent, target_is_directory=True)
    alias_module = alias / canonical_module.name
    monkeypatch.setattr(shell_request, "__file__", str(alias_module))
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    spec.os_env.sandbox.read_paths = [str(workspace), str(canonical_module.parents[2])]
    _require_request_launcher(spec, workspace)
    access = ClaudeModelAccess(
        "https://api.anthropic.com",
        "x-api-key",
        lambda: "synthetic-parent-only-secret",
        source_paths=(source,),
        static_credential_value="synthetic-parent-only-secret",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-parent-only-secret")
    monkeypatch.setattr(
        "omnigent.runtime.workflow.claude_terminal_model_access", lambda *_a, **_k: access
    )
    upstream = AsyncMock(
        side_effect=AssertionError("No provider or other upstream request is allowed")
    )
    monkeypatch.setattr("omnigent.inner.egress.proxy._open_upstream", upstream)
    starts: list[_HelperProcessClient] = []
    start = _HelperProcessClient._start_locked

    def record_start(helper: _HelperProcessClient) -> None:
        starts.append(helper)
        start(helper)

    monkeypatch.setattr(_HelperProcessClient, "_start_locked", record_start)
    registry = SessionResourceRegistry()
    execute = partial(
        _execute_os_env_tool,
        agent_spec=spec,
        conversation_id="live-shell",
        resource_registry=registry,
        effective_harness="claude-sdk",
    )
    command = """
import base64, json, os, socket, sys
from pathlib import Path
from urllib.parse import urlsplit
proxy = urlsplit(os.environ["HTTP_PROXY"])
authorization = base64.b64encode((proxy.username + ":" + proxy.password).encode()).decode()
with socket.create_connection((proxy.hostname, proxy.port), timeout=5) as connection:
    connection.sendall((
        "GET http://denied.invalid/ HTTP/1.1\\r\\nHost: denied.invalid\\r\\n"
        "Proxy-Authorization: Basic " + authorization + "\\r\\nConnection: close\\r\\n\\r\\n"
    ).encode())
    status = connection.makefile("rb").readline().decode().strip()
print(json.dumps({
    "placeholder": os.environ.get("ANTHROPIC_AUTH_TOKEN", "").startswith("oa_cred_"),
    "raw_key_absent": "ANTHROPIC_API_KEY" not in os.environ,
    "source_hidden": not Path(sys.argv[1]).exists(),
    "runtime_alias_hidden": not Path(sys.argv[2]).exists(),
    "status": status,
}))
"""
    try:
        assert (
            json.loads(await execute("sys_os_read", {"path": "note.txt"}))["content"] == "ready\n"
        )
        original = await _helper_process(registry, spec)
        for _ in range(2):
            result = json.loads(
                await execute(
                    "sys_os_shell",
                    {
                        "command": shlex.join(
                            [
                                sys.executable,
                                "-I",
                                "-S",
                                "-c",
                                command,
                                str(source),
                                str(alias_module),
                            ]
                        ),
                        "timeout": 15,
                    },
                )
            )
            assert result["exit_code"] == 0, result
            assert json.loads(result["stdout"]) == {
                "placeholder": True,
                "raw_key_absent": True,
                "source_hidden": True,
                "runtime_alias_hidden": True,
                "status": "HTTP/1.1 403 Forbidden",
            }
        assert len(starts) == 1
        assert await _helper_process(registry, spec) is original
        assert original.poll() is None
        upstream.assert_not_awaited()
    finally:
        await registry.cleanup_session("live-shell")


@pytest.mark.skipif(not _LIVE_LINUX, reason="requires Linux and Bubblewrap")
@pytest.mark.parametrize(
    "mode",
    [
        "large-error",
        "timeout-invalid",
        "utf8-disabled",
        "utf8-enabled",
        "locale-coercion",
    ],
)
@pytest.mark.asyncio
@pytest.mark.timeout(45)
async def test_real_runner_shell_bounds_output_and_preserves_invalid_byte_timeouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    from omnigent.inner._bounded_output import BoundedPipeCapture, CapturedOutput

    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    spec = _spec(tmp_path)
    if mode.startswith("utf8-"):
        monkeypatch.setenv("LC_ALL", "C")
        monkeypatch.setenv("PYTHONUTF8", "0" if mode == "utf8-disabled" else "1")
        assert spec.os_env is not None and spec.os_env.sandbox is not None
        spec.os_env.sandbox.env_passthrough = ["PYTHONUTF8"]
    elif mode == "locale-coercion":
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.setenv("LANG", "C")
        monkeypatch.setenv("LC_CTYPE", "C")
        monkeypatch.setenv("PYTHONCOERCECLOCALE", "0")
    _require_request_launcher(spec, tmp_path)
    observations: list[tuple[CapturedOutput, CapturedOutput]] = []
    original_result = BoundedPipeCapture.result

    def observe(capture: BoundedPipeCapture) -> tuple[CapturedOutput, CapturedOutput]:
        output = original_result(capture)
        observations.append(output)
        return output

    monkeypatch.setattr(BoundedPipeCapture, "result", observe)
    command = """
import os, sys, time
if sys.argv[1] == "timeout-invalid":
    os.write(1, b"\\xff")
    time.sleep(30)
elif sys.argv[1].startswith("utf8-"):
    os.write(1, b"\\xc3\\xa9")
else:
    for _ in range(32):
        os.write(1, b"x" * 8192)
        os.write(2, b"e" * 8192)
    raise SystemExit(7 if sys.argv[1] == "large-error" else 0)
"""
    registry = SessionResourceRegistry()
    command_text = (
        r"""printf '\303\251' | wc -m; printf '|%s' "$LC_CTYPE" """
        if mode == "locale-coercion"
        else shlex.join([sys.executable, "-I", "-S", "-c", command, mode])
    )
    try:
        result = json.loads(
            await _execute_os_env_tool(
                "sys_os_shell",
                {
                    "command": command_text,
                    "timeout": 3 if mode == "timeout-invalid" else 15,
                },
                agent_spec=spec,
                conversation_id="live-shell",
                resource_registry=registry,
            )
        )
        assert len(observations) == 1
        stdout, stderr = observations[0]
        if mode.startswith("utf8-") or mode == "locale-coercion":
            disposable = json.loads(
                await _execute_os_env_tool(
                    "sys_os_shell",
                    {
                        "command": command_text,
                        "timeout": 15,
                    },
                    agent_spec=spec,
                    conversation_id="disposable-encoding",
                )
            )
            if mode == "locale-coercion":
                assert stdout.decoding_error is None
                assert result == disposable
                count, effective_locale = result["stdout"].split("|")
                assert int(count.strip()) == 1
                assert effective_locale.strip() not in {"", "C", "POSIX"}
            elif mode == "utf8-disabled":
                assert stdout.decoding_error is not None
                assert result == disposable
                assert result["error"].startswith("os_env helper exception:")
            else:
                assert stdout.decoding_error is None
                assert result == disposable
                assert result["stdout"] == "\u00e9" and result["exit_code"] == 0
        elif mode == "timeout-invalid":
            assert stdout.decoding_error is not None
            assert result == {
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "timed_out": True,
                "error": "Command timed out after 3 seconds",
                "shell": shutil.which("bash") or shutil.which("sh") or "/bin/sh",
                "cwd": str(tmp_path),
            }
        else:
            assert len(stdout.text) == len(stderr.text) == 100_000
            assert stdout.omitted == stderr.omitted == 162_144
            assert result["stdout"].startswith(
                "x" * 100_000 + "\n[stdout truncated: 162,144 characters omitted."
            )
            assert result["stderr"].startswith(
                "e" * 100_000 + "\n[stderr truncated: 162,144 characters omitted."
            )
            assert result["exit_code"] == 7
            assert result["timed_out"] is False
            assert len(result["error"]) < 100_500
            assert result["error"].startswith("Command exited with status 7: e")
    finally:
        await registry.cleanup_session("live-shell")
