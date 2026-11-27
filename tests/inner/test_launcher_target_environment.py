"""Final-target environment handoff with every process and backend operation mocked."""

from __future__ import annotations

import ast
import base64
import builtins
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from omnigent.inner import sandbox as sb
from omnigent.runner.identity import RUNNER_AUTH_SECRET_ENV_VARS
from omnigent.sandbox.copy_on_write import SHARED_ENVIRONMENT_VAR

_SENTINEL = "inert-target-environment-value"
_TARGET = "/fixture/tools/bin/claude"


def _policy(active: bool) -> sb.SandboxPolicy:
    return sb.SandboxPolicy(
        backend_type="linux_bwrap" if active else "none",
        active=active,
        read_roots=[],
        write_roots=[],
        write_files=[],
        allow_network=False,
        spawn_env_allowlist=["PATH", "KEEP", "REMOVE", "HOME"],
    )


def _encoded(policy: sb.SandboxPolicy) -> str:
    return base64.urlsafe_b64encode(json.dumps(policy.to_jsonable()).encode()).decode()


def _payload(tmp_path: Path, *, inherit: bool = True) -> Path:
    path = tmp_path / "target-environment.json"
    path.touch(mode=0o600)
    path.write_text(
        json.dumps(
            {
                "inherit": inherit,
                "unset": ["REMOVE"],
                "variables": {"EXAMPLE_TARGET_ONLY": _SENTINEL, "HOME": "/caller-home"},
                "overrides": {"HOME": str(tmp_path / "owned-home")},
            }
        )
    )
    return path


@pytest.fixture
def inert_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events = []
    activation_environments = []
    environment: dict[str, str] = {}
    launcher_os = SimpleNamespace(**{**vars(os), "environ": environment})
    scratch = tmp_path / "launcher-scratch"
    scratch.mkdir(mode=0o700)
    backend = SimpleNamespace(
        prepare_launcher_policy=Mock(side_effect=lambda policy: policy),
        wrap_launcher_argv=Mock(side_effect=lambda argv, *_args, **_kwargs: ["inert-wrap", *argv]),
    )

    def activate(_policy):
        events.append("activate")
        activation_environments.append(dict(environment))

    def read(path, *args, **kwargs):
        events.append("payload-read")
        return builtins.open(path, *args, **kwargs)

    def spawn(*args, **kwargs):
        events.append("target-spawn")
        return SimpleNamespace(returncode=0)

    def stop_exec(*_args, **_kwargs):
        raise SystemExit("inert re-exec boundary")

    run = Mock(side_effect=spawn)
    cleanup = Mock(side_effect=lambda _path: events.append("cleanup"))
    execute = Mock(side_effect=stop_exec)
    monkeypatch.setattr(sb, "os", launcher_os)
    monkeypatch.setattr(sb, "get_backend", Mock(return_value=backend))
    monkeypatch.setattr(sb, "_validate_launcher_runtime", Mock())
    monkeypatch.setattr(sb, "activate_sandbox", Mock(side_effect=activate))
    monkeypatch.setattr(sb, "create_private_tmpdir", Mock(return_value=scratch))
    monkeypatch.setattr(sb, "cleanup_private_tmpdir", cleanup)
    monkeypatch.setattr(sb, "open", read, raising=False)
    monkeypatch.setattr(sb.subprocess, "run", run)
    monkeypatch.setattr(launcher_os, "execvp", execute)
    return SimpleNamespace(
        environment=environment,
        events=events,
        activation_environments=activation_environments,
        scratch=scratch,
        backend=backend,
        run=run,
        cleanup=cleanup,
        execute=execute,
    )


def _bootstrap() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "KEEP": "bootstrap",
        "REMOVE": "discard",
        "HOME": "/home",
    }


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("inherit", [False, True])
@pytest.mark.parametrize("launch_overrides", [False, True])
def test_target_environment_is_read_after_activation_for_final_spawn(
    tmp_path: Path, inert_launcher, active: bool, inherit: bool, launch_overrides: bool
) -> None:
    payload = _payload(tmp_path, inherit=inherit)
    bootstrap = _bootstrap()
    with patch.dict(inert_launcher.environment, bootstrap, clear=True):
        assert (
            sb.run_launcher(
                _encoded(_policy(active)),
                _TARGET,
                ["doctor"],
                cwd="/fixture/workspace",
                target_environment_path=str(payload),
                env_overrides={"KEEP": "snapshot"} if launch_overrides else None,
                _spawn_wrapped=active,
            )
            == 0
        )
        assert "EXAMPLE_TARGET_ONLY" not in inert_launcher.environment
    assert inert_launcher.events == ["activate", "payload-read", "target-spawn", "cleanup"]
    assert len(inert_launcher.activation_environments) == 1
    assert _SENTINEL not in inert_launcher.activation_environments[0].values()
    call = inert_launcher.run.call_args
    assert call.args == ([_TARGET, "doctor"],)
    assert call.kwargs["cwd"] == "/fixture/workspace"
    environment = call.kwargs["env"]
    assert environment["EXAMPLE_TARGET_ONLY"] == _SENTINEL
    assert environment["HOME"] == str(tmp_path / "owned-home")
    assert "REMOVE" not in environment
    assert (
        environment.get("KEEP") == ("snapshot" if launch_overrides else "bootstrap")
    ) is inherit
    assert _SENTINEL not in json.dumps(call.args)


@pytest.mark.parametrize("backend_name", ["linux_bwrap", "darwin_seatbelt"])
def test_reexec_transports_payload_path_without_reading_values(
    tmp_path: Path, inert_launcher, backend_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    policy = _policy(True)
    policy.backend_type = backend_name
    payload = _payload(tmp_path)
    for active in (False, True):
        source_policy = _policy(active)
        source_policy.backend_type = backend_name
        path = Path(
            sb.create_exec_launcher(
                _TARGET, source_policy, cwd="/fixture", target_environment_path=str(payload)
            )
        )
        try:
            source = path.read_text()
            assert str(payload) in source
            assert _SENTINEL not in source
            assert path.stat().st_mode & 0o777 == 0o700
        finally:
            path.unlink()
    inert_launcher.run.assert_not_called()
    inert_launcher.execute.assert_not_called()
    with patch.dict(inert_launcher.environment, _bootstrap(), clear=True):
        with pytest.raises(SystemExit, match="inert re-exec boundary"):
            sb.run_launcher(
                _encoded(policy),
                _TARGET,
                ["doctor"],
                cwd="/fixture/workspace",
                target_environment_path=str(payload),
                process_lifetime="inert-lifetime",
            )
    inert_launcher.run.assert_not_called()
    assert "payload-read" not in inert_launcher.events
    wrapped_call = inert_launcher.backend.wrap_launcher_argv.call_args
    assert wrapped_call.kwargs == {"target": _TARGET}
    argv = wrapped_call.args[0]
    inline = argv[argv.index("-c") + 1]
    parsed = ast.parse(inline)
    call = next(
        node
        for node in ast.walk(parsed)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_launcher"
    )
    keywords = {item.arg: ast.literal_eval(item.value) for item in call.keywords}
    assert keywords["target_environment_path"] == str(payload)
    assert keywords["process_lifetime"] == "inert-lifetime"
    assert keywords["cwd"] == "/fixture/workspace"
    assert keywords["_spawn_wrapped"] is True
    assert _SENTINEL not in json.dumps(inert_launcher.execute.call_args.args)
    assert _SENTINEL not in caplog.text
    inert_launcher.cleanup.assert_called_once_with(inert_launcher.scratch)


@pytest.mark.parametrize("active", [False, True])
def test_default_launcher_keeps_inherited_environment_call(inert_launcher, active: bool) -> None:
    with patch.dict(inert_launcher.environment, _bootstrap(), clear=True):
        assert (
            sb.run_launcher(
                _encoded(_policy(active)),
                _TARGET,
                ["doctor"],
                cwd="/fixture/workspace",
                _spawn_wrapped=active,
            )
            == 0
        )
    assert inert_launcher.run.call_args.args == ([_TARGET, "doctor"],)
    assert inert_launcher.run.call_args.kwargs == {"cwd": "/fixture/workspace"}
    assert "payload-read" not in inert_launcher.events
    source = sb._launcher_inline_source(_TARGET, _policy(False), cwd="/fixture")
    assert "target_environment_path" not in source


@pytest.mark.parametrize(
    ("fault", "active"),
    [
        pytest.param(fault, False, id=fault)
        for fault in (
            "missing",
            "malformed",
            "shape",
            "inherit",
            "unset",
            "unset-member",
            "variables",
            "variables-value",
            "overrides",
            "overrides-value",
            "oversize",
        )
    ]
    + [pytest.param("missing", True, id="missing-active")],
)
def test_invalid_target_payload_never_starts_target(
    tmp_path: Path, inert_launcher, fault: str, active: bool, caplog: pytest.LogCaptureFixture
) -> None:
    path = _payload(tmp_path)
    if fault == "missing":
        path.unlink()
    elif fault == "malformed":
        path.write_text("{ " + _SENTINEL)
    elif fault == "shape":
        path.write_text(json.dumps({"unexpected": _SENTINEL}))
    elif fault == "oversize":
        path.write_bytes(b" " * (1024 * 1024 + 1))
    else:
        data = json.loads(path.read_text())
        if fault == "unset-member":
            data["unset"].append(1)
        elif fault in {"variables-value", "overrides-value"}:
            data[fault.removesuffix("-value")]["EXAMPLE_INVALID"] = [_SENTINEL]
        else:
            data[fault] = "wrong-type-" + _SENTINEL
        path.write_text(json.dumps(data))
    with patch.dict(inert_launcher.environment, _bootstrap(), clear=True):
        with pytest.raises((ValueError, FileNotFoundError)) as error:
            sb.run_launcher(
                _encoded(_policy(active)),
                _TARGET,
                ["doctor"],
                target_environment_path=str(path),
                _spawn_wrapped=active,
            )
    inert_launcher.run.assert_not_called()
    inert_launcher.execute.assert_not_called()
    inert_launcher.cleanup.assert_called_once_with(inert_launcher.scratch if active else None)
    if "payload-read" in inert_launcher.events:
        assert inert_launcher.events.index("activate") < inert_launcher.events.index(
            "payload-read"
        )
    assert _SENTINEL not in inert_launcher.activation_environments[0].values()
    assert _SENTINEL not in str(error.value)
    assert _SENTINEL not in caplog.text


def test_relative_target_payload_path_is_refused_before_launcher_creation(
    inert_launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = Mock()
    monkeypatch.setattr(sb.tempfile, "mkstemp", create)
    with pytest.raises(ValueError, match="payload path must be absolute"):
        sb.create_exec_launcher(_TARGET, _policy(False), target_environment_path="relative.json")
    create.assert_not_called()


def test_target_failure_cleans_owned_launcher_scratch(tmp_path: Path, inert_launcher) -> None:
    path = _payload(tmp_path)
    inert_launcher.run.side_effect = OSError("inert target failure")
    with patch.dict(inert_launcher.environment, _bootstrap(), clear=True):
        with pytest.raises(OSError):
            sb.run_launcher(
                _encoded(_policy(True)),
                _TARGET,
                ["doctor"],
                target_environment_path=str(path),
                _spawn_wrapped=True,
            )
    inert_launcher.cleanup.assert_called_once_with(inert_launcher.scratch)
    assert _SENTINEL not in inert_launcher.activation_environments[0].values()


@pytest.mark.parametrize("active", [False, True])
def test_target_payload_cannot_restore_runner_secrets(
    tmp_path: Path, inert_launcher, active: bool
) -> None:
    path = _payload(tmp_path)
    data = json.loads(path.read_text())
    data["variables"].update(dict.fromkeys(RUNNER_AUTH_SECRET_ENV_VARS, _SENTINEL))
    data["variables"][SHARED_ENVIRONMENT_VAR] = _SENTINEL
    data["variables"][sb.HOST_SANDBOX_BACKEND_ENV] = "inert-backend-hint"
    path.write_text(json.dumps(data))
    with patch.dict(inert_launcher.environment, _bootstrap(), clear=True):
        sb.run_launcher(
            _encoded(_policy(active)),
            _TARGET,
            ["doctor"],
            target_environment_path=str(path),
            _spawn_wrapped=active,
        )
    environment = inert_launcher.run.call_args.kwargs["env"]
    assert not set(RUNNER_AUTH_SECRET_ENV_VARS).intersection(environment)
    assert _SENTINEL not in inert_launcher.activation_environments[0].values()
    assert SHARED_ENVIRONMENT_VAR not in environment
    assert (sb.HOST_SANDBOX_BACKEND_ENV in environment) is not active


@pytest.mark.parametrize("with_environment", [False, True])
def test_process_lifetime_is_acquired_only_after_containment_and_inherited_by_target(
    tmp_path: Path, inert_launcher, with_environment: bool
) -> None:
    import fcntl

    from omnigent.inner.process_lifetime import ProcessLifetimeLease, encode_process_leases

    paths = [tmp_path / "lease.lock", tmp_path / "terminal.owner"]
    for path in paths:
        path.touch(mode=0o600)
    lease = ProcessLifetimeLease(
        paths[0],
        (paths[0].stat().st_dev, paths[0].stat().st_ino),
        paths[1],
        (paths[1].stat().st_dev, paths[1].stat().st_ino),
    )
    inherited = []

    def target(_command, *, cwd, env, pass_fds):
        assert cwd == "/fixture/workspace"
        assert inert_launcher.events == [
            "activate",
            *(["payload-read"] if with_environment else []),
        ]
        assert len(pass_fds) == 2
        for path, descriptor in zip(paths, pass_fds, strict=True):
            assert os.fstat(descriptor).st_ino == path.stat().st_ino
            with path.open() as independent, pytest.raises(BlockingIOError):
                fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited.extend(pass_fds)
        assert env["HOME"] == (str(tmp_path / "owned-home") if with_environment else "/home")
        return SimpleNamespace(returncode=0)

    inert_launcher.run.side_effect = target
    payload = _payload(tmp_path) if with_environment else None
    with patch.dict(inert_launcher.environment, _bootstrap(), clear=True):
        assert (
            sb.run_launcher(
                _encoded(_policy(True)),
                _TARGET,
                ["doctor"],
                cwd="/fixture/workspace",
                target_environment_path=str(payload) if payload is not None else None,
                process_lifetime=encode_process_leases([lease]),
                _spawn_wrapped=True,
            )
            == 0
        )
    assert _SENTINEL not in inert_launcher.activation_environments[0].values()
    for descriptor in inherited:
        with pytest.raises(OSError):
            os.fstat(descriptor)
