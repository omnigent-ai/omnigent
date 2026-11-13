"""Check harness launch boundaries independently of vendor APIs and model behavior."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from omnigent.inner import claude_sdk_executor, codex_worker, sandbox
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.codex_worker import prepare_codex_worker
from omnigent.inner.datamodel import CredentialProxySpec, OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.goose_executor import GooseExecutor
from omnigent.inner.kimi_executor import KimiExecutor
from omnigent.inner.pi_executor import PiExecutor, _try_sandbox_pi
from omnigent.inner.qwen_executor import QwenExecutor

_HARNESSES = ["acp", "claude-sdk", "codex", "goose", "kimi", "pi", "qwen"]


@contextlib.contextmanager
def _prepared_launch(
    harness: str, executable: Path, workspace: Path, spec: OSEnvSpec | None
) -> Iterator[str]:
    runtime_home = executable.parent / "runtime-home"
    runtime_home.mkdir(exist_ok=True)
    worker = None
    names = ["PATH", "HOME", "CODEX_HOME", "TMPDIR"]
    if harness == "launcher":
        assert spec is not None
        policy = sandbox.resolve_sandbox(spec, workspace)
        policy = sandbox.with_additional_read_roots(policy, [executable.parent])
        launch_path = sandbox.create_exec_launcher(str(executable), policy, cwd=str(workspace))
    elif harness == "codex":
        worker = prepare_codex_worker(
            codex_path=str(executable),
            cwd=workspace,
            codex_home=runtime_home,
            os_env=spec,
            spawn_env_names=names,
            worker_env={},
        )
        launch_path = worker.launch_path
    elif harness == "claude-sdk":
        launch_path = claude_sdk_executor.prepare_claude_cli_path(
            str(executable), spec, cwd=str(workspace)
        ).cli_path
        assert launch_path is not None
    elif harness == "pi":
        launch_path = _try_sandbox_pi(str(executable), spec, str(workspace), names).launch_path
    elif harness == "acp":
        executor = AcpExecutor(
            AcpAgentConfig(command=shlex.quote(str(executable))), cwd=str(workspace), os_env=spec
        )
        launch_path, _ = executor._sandbox_launch(tuple(names))
    elif harness == "goose":
        launch_path = GooseExecutor(
            goose_path=str(executable), cwd=str(workspace), os_env=spec
        )._sandbox_launch_path(names)
    elif harness == "kimi":
        launch_path = KimiExecutor(
            binary_path=str(executable), cwd=str(workspace), os_env=spec
        )._sandbox_launch_path(names)
    else:
        assert harness == "qwen"
        launch_path = QwenExecutor(
            qwen_path=str(executable), cwd=str(workspace), os_env=spec
        )._sandbox_launch_path(names)
    try:
        yield launch_path
    finally:
        if worker is not None:
            worker.close()
        elif launch_path != str(executable):
            Path(launch_path).unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("harness", "fault"),
    [
        pytest.param(harness, fault, id=f"{fault}-{harness}")
        for fault in ("cwd", "resolve", "unsupported", "backend", "launcher", "inactive")
        for harness in _HARNESSES
        if fault != "backend" or harness in ("claude-sdk", "codex")
        # Codex resolution rollback has a dedicated worker-preflight scenario.
        if (harness, fault) != ("codex", "resolve")
    ]
    + [pytest.param("codex", "backend-oserror", id="backend-oserror-codex")],
)
def test_requested_sandbox_never_falls_back_to_bare_worker(
    harness: str, fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", raising=False)
    executable = tmp_path / "bin" / "agent"
    executable.parent.mkdir()
    executable.touch()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    policy = sandbox.SandboxPolicy(
        backend_type="linux_bwrap",
        active=fault != "inactive",
        read_roots=[workspace],
        write_roots=[],
        write_files=[],
        allow_network=True,
    )
    resolve = Mock(return_value=policy)
    launcher = Mock(
        side_effect=(
            OSError("launcher unavailable")
            if fault == "launcher"
            else sandbox.create_exec_launcher
        )
    )
    implementation = Mock()
    implementation.prepare_launcher_policy.side_effect = lambda policy: policy
    implementation.wrap_launcher_argv.side_effect = lambda argv, *args, **kwargs: [
        "sandbox",
        *argv,
    ]
    backend = Mock(return_value=implementation)
    if fault == "backend":
        backend.side_effect = NotImplementedError("backend unavailable")
    elif fault == "backend-oserror":
        backend.side_effect = OSError("backend unavailable")
    if fault == "resolve":
        resolve.side_effect = OSError("sandbox unavailable")
    elif fault == "unsupported":
        resolve.side_effect = NotImplementedError("sandbox unavailable")
    monkeypatch.setattr(sandbox, "resolve_sandbox", resolve)
    monkeypatch.setattr(claude_sdk_executor, "resolve_sandbox", resolve)
    monkeypatch.setattr(codex_worker, "resolve_sandbox", resolve)
    monkeypatch.setattr(sandbox, "create_exec_launcher", launcher)
    monkeypatch.setattr(claude_sdk_executor, "create_exec_launcher", launcher)
    monkeypatch.setattr(codex_worker, "create_exec_launcher", launcher)
    monkeypatch.setattr(sandbox, "get_backend", backend)
    monkeypatch.setattr(claude_sdk_executor, "get_backend", backend)
    monkeypatch.setattr(codex_worker, "get_backend", backend)
    spec = OSEnvSpec(cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="linux_bwrap"))
    private_diagnostic = "/private/host/sandbox-cwd-sentinel"
    cwd_resolution = Mock(side_effect=OSError(f"cwd resolution failed: {private_diagnostic}"))
    with pytest.raises(sandbox.HarnessSandboxPreparationError) as failure:
        with monkeypatch.context() as launch_patch:
            if fault == "cwd":
                launch_patch.setattr(Path, "resolve", cwd_resolution)
            with _prepared_launch(harness, executable, workspace, spec):
                pytest.fail(f"{harness} accepted an unavailable requested sandbox")
    cause = failure.value.__cause__
    if fault == "inactive":
        assert isinstance(cause, OSError)
        assert "requires an active sandbox" in str(cause)
    else:
        if fault == "cwd":
            failed_operation = cwd_resolution
        elif fault == "launcher":
            failed_operation = launcher
        elif fault in ("backend", "backend-oserror"):
            failed_operation = backend
        else:
            failed_operation = resolve
        assert cause is failed_operation.side_effect
    assert str(failure.value) == str(cause)
    if fault == "cwd":
        cwd_resolution.assert_called_once()
        resolve.assert_not_called()
        backend.assert_not_called()
        launcher.assert_not_called()
        assert private_diagnostic not in failure.value.client_message
    elif fault == "launcher":
        launcher.assert_called_once()
    elif fault in ("backend", "backend-oserror"):
        backend.assert_called_once()
    else:
        launcher.assert_not_called()
        resolve.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["acp", "goose", "qwen", "codex"])
@pytest.mark.parametrize(
    "failure_type",
    [sandbox.HarnessSandboxPreparationError, sandbox.HarnessSandboxValidationError],
    ids=["preparation", "validation"],
)
async def test_run_turn_preserves_typed_sandbox_failure(
    harness: str,
    failure_type: type[Exception],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = failure_type("private sandbox diagnostic")
    executor: Executor
    if harness == "codex":

        class FailingAppSession:
            async def run_turn(self, **_kwargs: object) -> AsyncIterator[None]:
                raise failure
                yield  # pragma: no cover

            async def close(self) -> None:
                pass

        executor = CodexExecutor(
            cwd=str(tmp_path),
            codex_path="/test/codex",
            model="test-model",
            app_session_factory=Mock(return_value=FailingAppSession()),
        )
    else:
        if harness == "acp":
            executor = AcpExecutor(AcpAgentConfig(command="/test/agent"), cwd=str(tmp_path))
        elif harness == "goose":
            executor = GooseExecutor(goose_path="/test/goose", cwd=str(tmp_path))
        else:
            executor = QwenExecutor(qwen_path="/test/qwen", cwd=str(tmp_path))
        monkeypatch.setattr(executor, "_start_process", AsyncMock(side_effect=failure))

    try:
        with pytest.raises(failure_type) as raised:
            async for _event in executor.run_turn(
                [{"role": "user", "content": "hello", "session_id": "sandbox-failure"}], [], ""
            ):
                pytest.fail(f"{harness} converted a typed sandbox failure into an event")
        assert raised.value is failure
    finally:
        await executor.close()


# Goose/Kimi/Qwen's executor scenarios cover absent and explicit-none policies.
# Codex's worker scenario covers explicit none and preserves the caller's executable.
@pytest.mark.parametrize(
    ("harness", "declared"),
    [
        (harness, declared)
        for harness in _HARNESSES
        if harness not in ("goose", "kimi", "qwen")
        for declared in (False, True)
        if not (harness == "codex" and declared)
    ],
)
def test_unsandboxed_worker_requires_no_policy_or_explicit_none(
    harness: str, declared: bool, tmp_path: Path
) -> None:
    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")) if declared else None
    executable = tmp_path / "bin" / "agent"
    executable.parent.mkdir()
    executable.touch()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with _prepared_launch(harness, executable, workspace, spec) as path:
        assert path == str(executable)


@pytest.mark.parametrize("harness", [name for name in _HARNESSES if name != "claude-sdk"])
def test_relative_policy_cwd_preserves_non_claude_workspace(
    harness: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    executable = tmp_path / "install" / "bin" / "agent"
    executable.parent.mkdir(parents=True)
    executable.touch()
    spec = OSEnvSpec(
        cwd="./sub",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=["."],
            write_paths=[],
            allow_network=False,
        ),
    )
    resolve = Mock(
        return_value=sandbox.SandboxPolicy(
            backend_type="linux_bwrap",
            active=True,
            read_roots=[workspace.resolve()],
            write_roots=[],
            write_files=[],
            allow_network=False,
        )
    )
    launcher = Mock(return_value=str(executable))
    backend = Mock()
    backend.wrap_launcher_argv.side_effect = lambda argv, *args, **kwargs: ["sandbox", *argv]
    monkeypatch.setattr(sandbox, "resolve_sandbox", resolve)
    monkeypatch.setattr(codex_worker, "resolve_sandbox", resolve)
    monkeypatch.setattr(sandbox, "create_exec_launcher", launcher)
    monkeypatch.setattr(codex_worker, "create_exec_launcher", launcher)
    monkeypatch.setattr(codex_worker, "get_backend", Mock(return_value=backend))
    with _prepared_launch(harness, executable, workspace, spec):
        pass
    resolve.assert_called_once_with(spec, workspace.resolve())
    assert spec.cwd == "./sub"
    launcher.assert_called_once()
    assert launcher.call_args.kwargs["cwd"] == str(workspace.resolve())


@pytest.mark.parametrize(
    "harness,feature",
    [
        (harness, feature)
        for harness in _HARNESSES
        for feature in ("cwd_conflict", "egress_rules", "credential_proxy")
        if feature == "cwd_conflict" or harness != "codex"
        # Claude's successful-wrap scenario also exercises cwd conflicts.
        if (harness, feature) != ("claude-sdk", "cwd_conflict")
    ],
)
def test_harness_rejects_policy_features_it_cannot_honor(
    harness: str, feature: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "bin" / "agent"
    executable.parent.mkdir()
    executable.touch()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = OSEnvSpec(
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            egress_rules=["GET api.example.com/**"] if feature == "egress_rules" else None,
            credential_proxy=(
                CredentialProxySpec(entries=[]) if feature == "credential_proxy" else None
            ),
        ),
    )
    if feature == "cwd_conflict":
        spec.cwd = str(tmp_path / "another-workspace")
    launcher = Mock(side_effect=AssertionError("Unsupported policy reached launcher creation"))
    monkeypatch.setattr(sandbox, "create_exec_launcher", launcher)
    monkeypatch.setattr(codex_worker, "create_exec_launcher", launcher)
    message = "conflicts" if feature == "cwd_conflict" else feature
    with pytest.raises(ValueError, match=message):
        with _prepared_launch(harness, executable, workspace, spec):
            pytest.fail(f"{harness} silently ignored {feature}")
    launcher.assert_not_called()


@pytest.mark.parametrize("harness", ["copilot", "cursor", "hermes"])
@pytest.mark.parametrize("sandbox_type", [None, "linux_bwrap", "none"])
def test_harness_without_process_sandbox_rejects_requested_confinement(
    harness: str, sandbox_type: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner.copilot_executor import CopilotExecutor
    from omnigent.inner.cursor_executor import CursorExecutor
    from omnigent.inner.hermes_executor import HermesExecutor

    monkeypatch.setattr(HermesExecutor, "_setup_hermes_home", lambda self: None)
    constructors = {
        "copilot": lambda **kwargs: CopilotExecutor(
            github_token="test-token", github_host="github.example.test", **kwargs
        ),
        "cursor": CursorExecutor,
        "hermes": HermesExecutor,
    }
    spec = OSEnvSpec(
        cwd=str(tmp_path),
        sandbox=OSEnvSandboxSpec(type=sandbox_type) if sandbox_type is not None else None,
    )
    if sandbox_type == "none":
        assert constructors[harness](os_env=spec, cwd=str(tmp_path))._cwd == str(tmp_path)
        return
    with pytest.raises(ValueError, match="cannot enforce"):
        constructors[harness](os_env=spec)


@pytest.mark.parametrize(
    "harness,launch_state",
    [
        *((harness, "expected") for harness in _HARNESSES),
        ("launcher", "different_cwd"),
        ("launcher", "forged_marker"),
    ],
)
@pytest.mark.parametrize("writable_grant", [False, True])
def test_real_worker_keeps_declared_filesystem_and_network_boundary(
    harness: str,
    launch_state: str,
    writable_grant: bool,
    tmp_path: Path,
    active_sandbox_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    writable = tmp_path / "writable"
    home = tmp_path / "home"
    install = tmp_path / "install" / "node_modules" / "vendor" / "bin"
    for directory in (workspace, outside, writable, home, install):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    allowed = workspace / "allowed.txt"
    allowed.write_text("allowed")
    secret = outside / "canary.txt"
    secret.write_text("private canary")
    protected_home_file = home / "protected.txt"
    protected_home_file.write_text("unchanged host content")
    home_output = home / "written.txt"
    transcript = home / ".claude" / "projects" / "transcript.json"
    ssh_key = home / ".ssh" / "id_ed25519"
    for private_file in (transcript, ssh_key):
        private_file.parent.mkdir(parents=True)
        private_file.write_text("private canary")
    escape = workspace / "escape"
    escape.symlink_to(secret)
    target = outside / "written.txt"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    probe = f"""
import json, pathlib, socket, subprocess, sys
assert pathlib.Path.cwd() == pathlib.Path({str(workspace.resolve())!r})
assert pathlib.Path({str(allowed)!r}).read_text() == 'allowed'
for path in (
    {str(secret)!r}, {str(escape)!r}, {str(transcript)!r}, {str(ssh_key)!r},
    {str(protected_home_file)!r}
):
    try:
        pathlib.Path(path).read_bytes()
    except OSError:
        pass
    else:
        raise AssertionError('unexpected read grant: ' + path)
for path in ({str(target)!r}, {str(workspace / "written.txt")!r}):
    try:
        pathlib.Path(path).write_text('unexpected')
    except OSError:
        pass
    else:
        raise AssertionError('unexpected write grant: ' + path)
# Bubblewrap can synthesize writable HOME parents in private tmpfs.
# The parent checks that these operations never create or modify host files.
for path in ({str(home_output)!r}, {str(protected_home_file)!r}):
    try:
        pathlib.Path(path).write_text('shadow write')
    except OSError:
        pass
if {writable_grant!r}:
    pathlib.Path({str(writable / "allowed.txt")!r}).write_text('allowed write')
else:
    try:
        pathlib.Path({str(writable / "denied.txt")!r}).write_text('unexpected')
    except OSError:
        pass
    else:
        raise AssertionError('unexpected write grant to unlisted root')
child = subprocess.run(
    ['/bin/sh', '-c', {("cat " + shlex.quote(str(secret)))!r}], capture_output=True
)
assert child.returncode != 0, 'child process escaped read restriction'
try:
    connection = socket.create_connection(('127.0.0.1', {listener.getsockname()[1]}), timeout=1)
except OSError:
    pass
else:
    connection.close()
    raise AssertionError('direct network access was permitted')
print(json.dumps({{'contained': True}}))
"""
    executable = install / "agent"
    executable.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(probe)}\n"
    )
    executable.chmod(0o755)
    spec = OSEnvSpec(
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(
            type=active_sandbox_type,
            read_paths=[str(workspace)],
            write_paths=[str(writable)] if writable_grant else [],
            allow_network=False,
            cwd_hidden_scan_overflow="error",
        ),
    )
    launch_env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    if launch_state == "forged_marker":
        launch_env["OMNIGENT_LAUNCHER_SPAWN_WRAPPED"] = "1"
        launch_env["OMNIGENT_LAUNCHER_SPAWN_PRIVATE_TMPDIR"] = str(outside)
    try:
        with _prepared_launch(harness, executable, workspace, spec) as path:
            completed = subprocess.run(
                [path],
                cwd=outside if launch_state == "different_cwd" else workspace,
                env=launch_env,
                text=True,
                capture_output=True,
                timeout=30,
            )
    finally:
        listener.close()
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"contained": True}
    assert not target.exists()
    assert not home_output.exists()
    assert protected_home_file.read_text() == "unchanged host content"
    assert outside.is_dir()
    assert secret.read_text() == "private canary"
    if writable_grant:
        assert (writable / "allowed.txt").read_text() == "allowed write"
    assert not (writable / "denied.txt").exists()


def test_claude_sdk_missing_system_cli_rejects_requested_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_sdk_executor, "_find_system_claude", lambda: None)
    with pytest.raises(OSError, match="requires a CLI path"):
        claude_sdk_executor.ClaudeSDKExecutor(os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec()))


def test_installed_codex_native_commands_remain_inside_outer_sandbox(
    tmp_path: Path, active_sandbox_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise Codex's real command RPC with its nested sandbox disabled."""
    import shutil
    import time

    from tests._helpers.live_server import terminate_process
    from tests._helpers.stdio_jsonrpc import read_json_line, write_json_line

    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("requires an installed Codex CLI with command/exec support")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("private canary")
    allowed = workspace / "allowed.txt"
    allowed.write_text("allowed")
    output = workspace / "forbidden-write.txt"
    monkeypatch.setenv("HOME", str(home))
    spec = OSEnvSpec(
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(
            type=active_sandbox_type,
            read_paths=[str(workspace)],
            write_paths=[],
            allow_network=False,
        ),
    )
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home)}
    with contextlib.closing(
        prepare_codex_worker(
            codex_path=codex,
            cwd=workspace,
            codex_home=home,
            os_env=spec,
            spawn_env_names=list(env),
            worker_env=env,
        )
    ) as worker:
        with (
            (tmp_path / "codex-stderr.txt").open("w+") as stderr,
            subprocess.Popen(
                [worker.launch_path, "app-server"],
                cwd=tmp_path,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
            ) as process,
        ):
            assert process.stdin is not None and process.stdout is not None

            def exchange(
                request_id: int, method: str, params: dict[str, object]
            ) -> dict[str, object]:
                write_json_line(
                    process.stdin, {"id": request_id, "method": method, "params": params}
                )
                deadline = time.monotonic() + 30
                while True:
                    response = read_json_line(
                        process.stdout, timeout_s=max(0.01, deadline - time.monotonic())
                    )
                    if response.get("id") == request_id:
                        assert "error" not in response, response
                        result = response["result"]
                        assert isinstance(result, dict)
                        return result

            try:
                exchange(1, "initialize", {"clientInfo": {"name": "sandbox-test", "version": "1"}})
                write_json_line(process.stdin, {"method": "initialized", "params": {}})
                command = (
                    f"cat {shlex.quote(str(allowed))} && "
                    f"! cat {shlex.quote(str(secret))} && "
                    f"! sh -c {shlex.quote('echo denied > ' + shlex.quote(str(output)))}"
                )
                result = exchange(
                    2,
                    "command/exec",
                    {
                        "command": ["/bin/sh", "-c", command],
                        "cwd": str(workspace),
                        "sandboxPolicy": {"type": "dangerFullAccess"},
                        "timeoutMs": 10000,
                    },
                )
                assert result["exitCode"] == 0, result
                assert result["stdout"] == "allowed", result
                assert not output.exists()
            finally:
                terminate_process(process)


@pytest.mark.asyncio
async def test_codex_provider_discovery_runs_inside_requested_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active_sandbox_type: str
) -> None:
    from omnigent.harnesses.codex_native.app_server import prepare_codex_provider_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "install" / "bin" / "codex"
    executable.parent.mkdir(parents=True)
    home = executable.parent / "runtime-home"
    monkeypatch.setenv("HOME", str(home))
    canary = tmp_path / "private.txt"
    canary.write_text("private canary")
    report = home / "discovery.json"
    script = f"""
import json, os, pathlib, sys
assert 'app-server' in sys.argv[1:]
try:
    pathlib.Path({str(canary)!r}).read_text()
except OSError:
    denied = True
else:
    denied = False
pathlib.Path({str(report)!r}).write_text(json.dumps({{
    'cwd': os.getcwd(),
    'filtered': 'HOST_ONLY' not in os.environ,
    'denied': denied,
}}))
"""
    executable.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(script)} "$@"\n'
    )
    executable.chmod(0o755)
    spec = OSEnvSpec(
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(
            type=active_sandbox_type,
            read_paths=[str(workspace)],
            write_paths=[],
            allow_network=False,
        ),
    )
    with _prepared_launch("codex", executable, workspace, spec) as launch_path:
        overrides = await prepare_codex_provider_config(
            home,
            [],
            codex_path=launch_path,
            cwd=workspace,
            env={"PATH": os.environ["PATH"], "HOME": str(home), "HOST_ONLY": "parent-value"},
            source_home=home,
        )
    assert overrides == []
    assert json.loads(report.read_text()) == {
        "cwd": str(workspace.resolve()),
        "filtered": True,
        "denied": True,
    }


def test_pi_version_probe_runs_inside_requested_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active_sandbox_type: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    executable = tmp_path / "install" / "bin" / "pi"
    executable.parent.mkdir(parents=True)
    secret = tmp_path / "private.txt"
    secret.write_text("private canary")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_HOST_SECRET", "private value")
    script = f"""
import os, pathlib, sys
assert sys.argv[1:] == ['--version']
assert pathlib.Path.cwd() == pathlib.Path({str(workspace.resolve())!r})
assert 'FAKE_HOST_SECRET' not in os.environ
try:
    pathlib.Path({str(secret)!r}).read_text()
except OSError:
    pass
else:
    raise AssertionError('version probe read outside its sandbox')
print('0.80.4')
"""
    executable.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(script)} "$@"\n'
    )
    executable.chmod(0o755)
    executor = PiExecutor(
        pi_path=str(executable),
        cwd=str(workspace),
        os_env=OSEnvSpec(
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(
                type=active_sandbox_type,
                read_paths=[str(workspace)],
                write_paths=[],
                allow_network=False,
            ),
        ),
    )
    try:
        assert executor._pi_version == (0, 80, 4)
    finally:
        Path(executor._pi_launch_path).unlink(missing_ok=True)


def test_pi_version_probe_timeout_cleans_process_tree_and_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active_sandbox_type: str
) -> None:
    from omnigent.harnesses.pi_native.main import pi_version

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TMPDIR", raising=False)
    report = workspace / "probe.json"
    executable = tmp_path / "install" / "bin" / "pi"
    executable.parent.mkdir(parents=True)
    marker = str(report)
    script = f"""
import os, pathlib, subprocess, sys, time
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', {marker!r}])
pathlib.Path({str(report)!r}).write_text(os.environ['TMPDIR'])
time.sleep(60)
"""
    executable.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(script)} "$@" '
        f"{shlex.quote(marker)}\n"
    )
    executable.chmod(0o755)
    spec = OSEnvSpec(
        sandbox=OSEnvSandboxSpec(
            type=active_sandbox_type,
            read_paths=[str(workspace)],
            write_paths=[str(workspace)],
            allow_network=False,
        )
    )

    def probe_processes() -> list[psutil.Process]:
        return [
            process
            for process in psutil.process_iter(["cmdline"])
            if marker in (process.info["cmdline"] or [])
        ]

    with _prepared_launch("pi", executable, workspace, spec) as launcher:
        try:
            assert (
                pi_version(
                    launcher, env={"PATH": os.environ["PATH"], "HOME": str(home)}, cwd=workspace
                )
                is None
            )
            scratch = Path(report.read_text())
            assert not probe_processes(), "probe descendants survived their timeout"
            assert not scratch.exists()
            assert not scratch.parent.exists()
        finally:
            for process in probe_processes():
                with contextlib.suppress(psutil.NoSuchProcess):
                    process.kill()
