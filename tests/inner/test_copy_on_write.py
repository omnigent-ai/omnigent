"""Configuration, ownership, and lifecycle coverage for disposable write grants."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.bwrap_sandbox import BwrapSandboxBackend
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, WritePathSpec, parse_write_paths
from omnigent.inner.loader import _parse_os_env_sandbox_spec
from omnigent.inner.sandbox import (
    SandboxPolicy,
    resolve_sandbox,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
)
from omnigent.sandbox.copy_on_write import CopyOnWriteEnvironment, wrap_shared_namespace
from omnigent.spec.parser import _parse_os_env_sandbox


@pytest.mark.parametrize("parser", [_parse_os_env_sandbox_spec, _parse_os_env_sandbox])
def test_write_path_forms_and_roundtrip(parser) -> None:
    spec = parser(
        {
            "type": "linux_bwrap",
            "write_paths": [
                "./artifacts",
                {"path": "./dependencies", "copy_on_write": True},
                {"path": "./build"},
            ],
        }
    )
    assert spec.write_path_specs == [
        WritePathSpec("./artifacts"),
        WritePathSpec("./dependencies", True),
        WritePathSpec("./build"),
    ]
    assert parser(asdict(spec)).write_path_specs == spec.write_path_specs


@pytest.mark.parametrize(
    "raw",
    [
        ".",
        {},
        [None],
        [True],
        [42],
        [""],
        [{}],
        [{"path": ""}],
        [{"path": 123}],
        [{"path": ".", "copy_on_write": "true"}],
        [{"path": ".", "copy_on_write": 1}],
        [{"path": ".", "copy_on_write": None}],
        [{"path": ".", "copy_on_writ": True}],
    ],
)
@pytest.mark.parametrize(
    "parser", [parse_write_paths, _parse_os_env_sandbox_spec, _parse_os_env_sandbox]
)
def test_invalid_write_paths_fail_loud(parser, raw) -> None:
    value = raw if parser is parse_write_paths else {"type": "linux_bwrap", "write_paths": raw}
    with pytest.raises((ValueError, OmnigentError)):
        parser(value)


@pytest.mark.parametrize("backend", ["none", "darwin_seatbelt", "windows_jobobject"])
@pytest.mark.parametrize("parser", [_parse_os_env_sandbox_spec, _parse_os_env_sandbox])
def test_unsupported_backends_reject_cow(parser, backend: str) -> None:
    with pytest.raises((ValueError, OmnigentError), match="copy_on_write"):
        parser({"type": backend, "write_paths": [{"path": ".", "copy_on_write": True}]})


def test_programmatic_configuration_cannot_bypass_backend_check(tmp_path: Path) -> None:
    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none", write_paths=[WritePathSpec(".", True)]))
    with pytest.raises(ValueError, match="linux_bwrap"):
        resolve_sandbox(spec, tmp_path)


@pytest.fixture
def backend(monkeypatch) -> BwrapSandboxBackend:
    monkeypatch.setattr("omnigent.inner.bwrap_sandbox.sys.platform", "linux")
    monkeypatch.setattr("omnigent.inner.bwrap_sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    return BwrapSandboxBackend()


def test_persistent_parent_with_disposable_child(backend, tmp_path: Path) -> None:
    child = tmp_path / "deps"
    child.mkdir()
    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(write_paths=[".", WritePathSpec("deps", True)]))
    policy = backend.resolve(spec, tmp_path)
    assert policy.write_roots == [tmp_path, child]
    assert policy.copy_on_write_roots == [child]
    with pytest.raises(RuntimeError, match="prepared"):
        backend.wrap_launcher_argv(["/bin/sh"], policy, tmp_path)


@pytest.mark.parametrize(
    "grants",
    [
        [".", WritePathSpec(".", True)],
        [WritePathSpec(".", True), "deps"],
        [WritePathSpec(".", True), WritePathSpec("deps", True)],
        [WritePathSpec("missing", True)],
        [WritePathSpec("file", True)],
    ],
)
def test_ambiguous_or_invalid_roots_rejected(backend, tmp_path: Path, grants) -> None:
    (tmp_path / "deps").mkdir()
    (tmp_path / "file").write_text("original")
    with pytest.raises(ValueError):
        backend.resolve(OSEnvSpec(sandbox=OSEnvSandboxSpec(write_paths=grants)), tmp_path)


def test_file_grant_cannot_persist_inside_cow(backend, tmp_path: Path) -> None:
    spec = OSEnvSpec(
        sandbox=OSEnvSandboxSpec(
            write_paths=[WritePathSpec(".", True)],
            write_files=["file"],
        )
    )
    with pytest.raises(ValueError, match="write_files"):
        backend.resolve(spec, tmp_path)


def test_resolved_alias_conflict_is_rejected(backend, tmp_path: Path) -> None:
    (tmp_path / "deps").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "deps", target_is_directory=True)
    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(write_paths=["alias", WritePathSpec("deps", True)]))
    with pytest.raises(ValueError, match="overlaps"):
        backend.resolve(spec, tmp_path)


def _policy(root: Path) -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[root],
        write_files=[],
        allow_network=False,
        copy_on_write_roots=[root],
    )


def test_policy_transport_and_clones_preserve_cow(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.copy_on_write_namespace = (100, 200, 300)
    for copied in [
        SandboxPolicy.from_jsonable(policy.to_jsonable()),
        with_additional_write_roots(policy, [tmp_path / "runtime"]),
        with_additional_read_roots(policy, [tmp_path / "reference"]),
        with_additional_write_files(policy, [tmp_path / "runtime-file"]),
    ]:
        assert copied.copy_on_write_roots == [tmp_path]
        assert copied.copy_on_write_namespace == (100, 200, 300)
        assert copied.copy_on_write_roots is not policy.copy_on_write_roots


@pytest.mark.parametrize("namespace", [[1, 2], [True, 2, 3], [1, "2", 3], [0, 2, 3], "invalid"])
def test_invalid_namespace_transport_rejected(tmp_path: Path, namespace) -> None:
    payload = _policy(tmp_path).to_jsonable()
    payload["copy_on_write_namespace"] = namespace
    with pytest.raises(ValueError, match="namespace"):
        SandboxPolicy.from_jsonable(payload)


def test_shared_resource_survives_consumers_and_never_silently_resets(
    tmp_path, monkeypatch
) -> None:
    environment = CopyOnWriteEnvironment([tmp_path])
    process = Mock()
    process.poll.return_value = None

    def start() -> None:
        environment._process = process
        environment._namespace = (100, 200, 300)

    start_mock = Mock(side_effect=start)
    monkeypatch.setattr(environment, "_start", start_mock)
    first = _policy(tmp_path)
    second = replace(first)
    environment.prepare(first)
    environment.prepare(second)
    start_mock.assert_called_once()
    assert first.copy_on_write_namespace == second.copy_on_write_namespace
    process.poll.return_value = 1
    with pytest.raises(RuntimeError, match="lost"):
        environment.prepare(second)
    start_mock.assert_called_once()
    environment.close()
    environment.close()
    process.wait.assert_called_once()
    with pytest.raises(RuntimeError, match="closed"):
        environment.prepare(first)


def test_different_environment_cannot_attach(tmp_path: Path) -> None:
    environment = CopyOnWriteEnvironment([tmp_path])
    with pytest.raises(ValueError, match="match"):
        environment.prepare(_policy(tmp_path / "other"))


def test_unprepared_policy_never_runs_with_persistent_writes(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="prepared"):
        wrap_shared_namespace(["bwrap", "--", "sh"], _policy(tmp_path))


def test_missing_bubblewrap_is_actionable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("omnigent.sandbox.copy_on_write.shutil.which", lambda _: None)
    environment = CopyOnWriteEnvironment([tmp_path])
    with pytest.raises(OSError, match=r"Bubblewrap.*0\.11"):
        environment.prepare(_policy(tmp_path))
    environment.close()


@pytest.mark.parametrize("copy_on_write", [False, True])
def test_missing_bubblewrap_requirements_depend_on_grant(
    backend, tmp_path, monkeypatch, copy_on_write
) -> None:
    monkeypatch.setattr("omnigent.inner.bwrap_sandbox.shutil.which", lambda _: None)
    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(write_paths=[WritePathSpec(".", copy_on_write)]))
    with pytest.raises(OSError) as error:
        backend.resolve(spec, tmp_path)
    message = str(error.value)
    assert ("OverlayFS" in message) is copy_on_write
    assert ("0.11" in message) is copy_on_write


@pytest.mark.parametrize("grants", [["."], [WritePathSpec(".", False)]])
def test_persistent_grants_never_initialize_or_probe_cow(backend, tmp_path, monkeypatch, grants):
    from omnigent.inner.os_env import create_os_environment

    constructor = Mock(side_effect=AssertionError("Ordinary writes must not initialize COW"))
    monkeypatch.setattr("omnigent.inner.os_env.CopyOnWriteEnvironment", constructor)
    spec = OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(write_paths=grants))
    policy = backend.resolve(spec, tmp_path)
    environment = create_os_environment(spec, sandbox_policy=policy)
    assert environment is not None
    try:
        environment.prepare_sandbox(policy)
        argv = backend.wrap_launcher_argv(["/bin/sh"], policy, tmp_path)
        assert "omnigent.sandbox.copy_on_write" not in argv
        assert "--tmp-overlay" not in argv
        assert environment.copy_on_write_environment is None
        constructor.assert_not_called()
    finally:
        environment.close()


def test_unlaunchable_bubblewrap_reports_requirements(tmp_path, monkeypatch):
    monkeypatch.setattr("omnigent.sandbox.copy_on_write.shutil.which", lambda _: "bwrap")
    monkeypatch.setattr(
        "omnigent.sandbox.copy_on_write.subprocess.Popen",
        Mock(side_effect=PermissionError("launcher is not executable")),
    )
    environment = CopyOnWriteEnvironment([tmp_path])
    with pytest.raises(OSError, match=r"copy_on_write.*launcher is not executable"):
        environment.prepare(_policy(tmp_path))
    assert environment._process is None
    environment.close()


@pytest.fixture
def mount_process(monkeypatch):
    import io

    process = Mock()
    process.stdout = io.StringIO("")
    process.stdin = io.StringIO()
    process.poll.return_value = None
    process.mount_error = ""

    def launch(*args, **kwargs):
        process.argv = args[0]
        kwargs["stderr"].write(process.mount_error)
        return process

    monkeypatch.setattr("omnigent.sandbox.copy_on_write.shutil.which", lambda _: "bwrap")
    monkeypatch.setattr("omnigent.sandbox.copy_on_write.subprocess.Popen", launch)
    selector = Mock()
    selector.select.return_value = [1]
    selector_context = Mock()
    selector_context.__enter__ = Mock(return_value=selector)
    selector_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        "omnigent.sandbox.copy_on_write.selectors.DefaultSelector", lambda: selector_context
    )
    return process, selector


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize(
    "diagnostic",
    [
        "bwrap: Unknown option --tmp-overlay",
        "bwrap: No permissions to create a new namespace",
        "bwrap: mounting overlay: Operation not permitted",
        "bwrap: mounting overlay: No such device",
        "OSError: [Errno 95] Operation not supported: tmpfs user xattrs",
        "",
    ],
)
def test_failed_start_reaps_process(tmp_path, monkeypatch, mount_process, timeout, diagnostic):
    process, selector = mount_process
    process.mount_error = diagnostic
    selector.select.return_value = [] if timeout else [1]
    monkeypatch.setattr(
        "omnigent.sandbox.copy_on_write.os.uname", lambda: Mock(release="6.8.0-policy-blocked")
    )
    environment = CopyOnWriteEnvironment([tmp_path])
    with pytest.raises(OSError, match="Timed out" if timeout else "kernel") as error:
        environment.prepare(_policy(tmp_path))
    message = str(error.value)
    assert "6.8.0-policy-blocked" in message
    assert "Linux 6.6+" in message and "distro backports" in message
    assert "userxattr" in message and "AppArmor/SELinux/seccomp" in message
    assert "#copy-on-write-requirements" in message
    assert "Original directories will not be used for writes" in message
    if not timeout:
        assert (diagnostic or "without reporting a mount namespace") in message
    process.wait.assert_called_once()
    assert process.stdout.closed and process.stdin.closed
    assert environment._process is None


def test_keeper_checks_tmpfs_xattrs_before_reporting_ready(
    tmp_path, monkeypatch, mount_process, capsys
):
    import os

    from omnigent.sandbox.copy_on_write import _run_keeper

    process, _ = mount_process
    environment = CopyOnWriteEnvironment([tmp_path])
    with pytest.raises(OSError):
        environment.prepare(_policy(tmp_path))
    assert process.argv[-3:-1] == ["omnigent.sandbox.copy_on_write", "--keeper"]
    xattr = Mock(side_effect=OSError("tmpfs user xattrs unavailable"))
    monkeypatch.setattr(os, "setxattr", xattr, raising=False)
    with pytest.raises(OSError, match="tmpfs user xattrs unavailable"):
        _run_keeper(str(tmp_path))
    xattr.assert_called_once()
    assert capsys.readouterr().out == ""


def test_keeper_rejects_missing_xattr_support(tmp_path, monkeypatch, capsys):
    import os

    from omnigent.sandbox.copy_on_write import _run_keeper

    monkeypatch.delattr(os, "setxattr", raising=False)
    with pytest.raises(OSError, match="requires Linux extended attribute support"):
        _run_keeper(str(tmp_path))
    assert capsys.readouterr().out == ""


def test_keeper_entrypoint_reports_ready_and_exits_on_owner_eof(tmp_path):
    import os
    import subprocess
    import sys

    if not hasattr(os, "setxattr"):
        pytest.skip("Extended attributes required")
    result = subprocess.run(
        [sys.executable, "-u", "-m", "omnigent.sandbox.copy_on_write", "--keeper", str(tmp_path)],
        input="",
        capture_output=True,
        text=True,
        env={"PATH": os.defpath},
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0


def test_working_kernel_backport_is_accepted(tmp_path, monkeypatch, mount_process):
    import io

    process, _ = mount_process
    process.stdout = io.StringIO("123\n")
    monkeypatch.setattr(
        "omnigent.sandbox.copy_on_write.os.uname", lambda: Mock(release="5.4.0-backport")
    )
    import os

    original_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path).startswith("/proc/123/ns/"):
            return Mock(st_ino=456)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr("omnigent.sandbox.copy_on_write.os.stat", stat)
    environment = CopyOnWriteEnvironment([tmp_path])
    policy = _policy(tmp_path)
    try:
        environment.prepare(policy)
        assert policy.copy_on_write_namespace == (123, 456, 456)
    finally:
        environment.close()


def test_stale_namespace_closes_pinned_descriptors(monkeypatch) -> None:
    from omnigent.sandbox.copy_on_write import _enter

    monkeypatch.setattr("omnigent.sandbox.copy_on_write.os.open", Mock(side_effect=[10, 11, 12]))
    monkeypatch.setattr(
        "omnigent.sandbox.copy_on_write.os.fstat", Mock(return_value=Mock(st_ino=999))
    )
    close = Mock()
    monkeypatch.setattr("omnigent.sandbox.copy_on_write.os.close", close)
    execvp = Mock()
    monkeypatch.setattr("omnigent.sandbox.copy_on_write.os.execvp", execvp)
    with pytest.raises(RuntimeError, match="replaced"):
        _enter([100, 200, 300], ["bwrap"])
    assert [call.args[0] for call in close.call_args_list] == [10, 11, 12]
    execvp.assert_not_called()


@pytest.mark.parametrize("active", [False, True])
def test_framework_runtime_handle_never_reaches_tool_environment(tmp_path, active) -> None:
    from omnigent.inner.os_env import build_helper_env
    from omnigent.sandbox.copy_on_write import SHARED_ENVIRONMENT_VAR

    policy = _policy(tmp_path)
    policy.active = active
    policy.env_passthrough = [SHARED_ENVIRONMENT_VAR]
    assert SHARED_ENVIRONMENT_VAR not in build_helper_env(
        {SHARED_ENVIRONMENT_VAR: "handle"}, policy
    )


def test_framework_runtime_handle_matches_declared_roots(tmp_path, monkeypatch) -> None:
    from omnigent.sandbox.copy_on_write import (
        SHARED_ENVIRONMENT_VAR,
        attach_shared_environment,
        export_shared_environment,
    )

    policy = _policy(tmp_path)
    policy.copy_on_write_namespace = (100, 200, 300)
    monkeypatch.setenv(SHARED_ENVIRONMENT_VAR, export_shared_environment(policy))
    attached = _policy(tmp_path)
    attach_shared_environment(attached)
    assert attached.copy_on_write_namespace == policy.copy_on_write_namespace
    with pytest.raises(ValueError, match="match"):
        attach_shared_environment(_policy(tmp_path / "different"))


def test_terminal_cannot_override_copy_on_write_to_persistent_writes() -> None:
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.inner.terminal import build_terminal_os_env_spec

    parent = OSEnvSpec(
        sandbox=OSEnvSandboxSpec(type="linux_bwrap", write_paths=[WritePathSpec(".", True)])
    )
    terminal = TerminalEnvSpec(command="bash", os_env="inherit", allow_sandbox_override=True)
    with pytest.raises(ValueError, match="copy_on_write"):
        build_terminal_os_env_spec(terminal, parent_os_env_spec=parent, sandbox_override="none")


def test_fork_cannot_create_independent_copies_of_a_shared_overlay() -> None:
    with pytest.raises(ValueError, match="fork"):
        OSEnvSpec(
            fork=True,
            sandbox=OSEnvSandboxSpec(type="linux_bwrap", write_paths=[WritePathSpec(".", True)]),
        )


def test_keeper_timeout_stops_process_tree(monkeypatch):
    import subprocess

    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired("bwrap", 5), 0]
    kill = Mock()
    monkeypatch.setattr("omnigent.sandbox.copy_on_write.kill_tree", kill)
    CopyOnWriteEnvironment._stop(process)
    process.stdin.close.assert_called_once()
    kill.assert_called_once_with(process)
    process.stdout.close.assert_called_once()


@pytest.mark.parametrize("harness", ["openai-agents", "codex", "codex-native", "claude-sdk"])
@pytest.mark.parametrize("legacy", [False, True])
def test_agent_parser_rejects_unshared_harness(tmp_path, harness, legacy):
    import yaml

    from omnigent.inner.loader import _parse_agent_def
    from omnigent.spec.parser import parse

    config = {
        "spec_version": 1,
        "name": "cow-test",
        "executor": {"harness": harness},
        "os_env": {
            "sandbox": {
                "type": "linux_bwrap",
                "write_paths": [{"path": ".", "copy_on_write": True}],
            }
        },
    }
    if not legacy:
        config["executor"] = {"type": "omnigent", "config": {"harness": harness}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))

    def load():
        return _parse_agent_def(config) if legacy else parse(tmp_path)

    if harness == "openai-agents":
        assert load().os_env is not None
    else:
        with pytest.raises(ValueError, match="openai-agents"):
            load()
