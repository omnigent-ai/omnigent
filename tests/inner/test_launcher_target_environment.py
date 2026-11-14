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
    scratch = tmp_path / "launcher-scratch"
    scratch.mkdir(mode=0o700)
    backend = SimpleNamespace(
        prepare_launcher_policy=Mock(side_effect=lambda policy: policy),
        wrap_launcher_argv=Mock(side_effect=lambda argv, *_args, **_kwargs: ["inert-wrap", *argv]),
    )

    def activate(_policy):
        events.append("activate")
        assert _SENTINEL not in os.environ.values()

    def read(path, *args, **kwargs):
        events.append("payload-read")
        assert "activate" in events
        return builtins.open(path, *args, **kwargs)

    def spawn(*args, **kwargs):
        events.append("target-spawn")
        return SimpleNamespace(returncode=0)

    def stop_exec(*_args, **_kwargs):
        raise SystemExit("inert re-exec boundary")

    run = Mock(side_effect=spawn)
    cleanup = Mock(side_effect=lambda _path: events.append("cleanup"))
    execute = Mock(side_effect=stop_exec)
    monkeypatch.setattr(sb, "get_backend", Mock(return_value=backend))
    monkeypatch.setattr(sb, "activate_sandbox", Mock(side_effect=activate))
    monkeypatch.setattr(sb, "create_private_tmpdir", Mock(return_value=scratch))
    monkeypatch.setattr(sb, "cleanup_private_tmpdir", cleanup)
    monkeypatch.setattr(sb, "open", read, raising=False)
    monkeypatch.setattr(sb.subprocess, "run", run)
    monkeypatch.setattr(sb.os, "execvp", execute)
    return SimpleNamespace(
        events=events, scratch=scratch, backend=backend, run=run, cleanup=cleanup, execute=execute
    )


def _bootstrap(*, wrapped: bool = False) -> dict[str, str]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "KEEP": "bootstrap",
        "REMOVE": "discard",
        "HOME": "/home",
    }
    if wrapped:
        environment[sb._LAUNCHER_WRAPPED_ENV] = "1"
    return environment


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("inherit", [False, True])
def test_target_environment_is_read_after_activation_for_final_spawn(
    tmp_path: Path, inert_launcher, active: bool, inherit: bool
) -> None:
    payload = _payload(tmp_path, inherit=inherit)
    bootstrap = _bootstrap(wrapped=active)
    with patch.dict(os.environ, bootstrap, clear=True):
        assert (
            sb.run_launcher(
                _encoded(_policy(active)),
                _TARGET,
                ["doctor"],
                target_environment_path=str(payload),
            )
            == 0
        )
        assert "EXAMPLE_TARGET_ONLY" not in os.environ
    assert inert_launcher.events == ["activate", "payload-read", "target-spawn", "cleanup"]
    call = inert_launcher.run.call_args
    assert call.args == ([_TARGET, "doctor"],)
    environment = call.kwargs["env"]
    assert environment["EXAMPLE_TARGET_ONLY"] == _SENTINEL
    assert environment["HOME"] == str(tmp_path / "owned-home")
    assert "REMOVE" not in environment
    assert (environment.get("KEEP") == "bootstrap") is inherit
    assert _SENTINEL not in json.dumps(call.args)


@pytest.mark.parametrize("backend_name", ["linux_bwrap", "darwin_seatbelt"])
def test_reexec_transports_payload_path_without_reading_values(
    tmp_path: Path, inert_launcher, backend_name: str
) -> None:
    policy = _policy(True)
    policy.backend_type = backend_name
    payload = _payload(tmp_path)
    with patch.dict(os.environ, _bootstrap(), clear=True):
        with pytest.raises(SystemExit, match="inert re-exec boundary"):
            sb.run_launcher(
                _encoded(policy), _TARGET, ["doctor"], target_environment_path=str(payload)
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
    assert _SENTINEL not in json.dumps(inert_launcher.execute.call_args.args)
    inert_launcher.cleanup.assert_called_once_with(inert_launcher.scratch)


@pytest.mark.parametrize("active", [False, True])
def test_default_launcher_keeps_inherited_environment_call(inert_launcher, active: bool) -> None:
    with patch.dict(os.environ, _bootstrap(wrapped=active), clear=True):
        assert sb.run_launcher(_encoded(_policy(active)), _TARGET, ["doctor"]) == 0
    assert inert_launcher.run.call_args.args == ([_TARGET, "doctor"],)
    assert inert_launcher.run.call_args.kwargs == {}
    assert "payload-read" not in inert_launcher.events
    source = sb._launcher_inline_source(_TARGET, _policy(False), cwd="/fixture")
    assert "target_environment_path" not in source


@pytest.mark.parametrize("active", [False, True])
def test_launcher_source_contains_only_payload_path(
    tmp_path: Path, inert_launcher, active: bool, caplog: pytest.LogCaptureFixture
) -> None:
    payload = _payload(tmp_path)
    path = Path(
        sb.create_exec_launcher(
            _TARGET, _policy(active), cwd="/fixture", target_environment_path=str(payload)
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
    assert _SENTINEL not in caplog.text


@pytest.mark.parametrize(
    "fault",
    ["missing", "malformed", "shape", "inherit", "unset", "variables", "overrides", "oversize"],
)
def test_invalid_target_payload_never_starts_target(
    tmp_path: Path, inert_launcher, fault: str, caplog: pytest.LogCaptureFixture
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
        data[fault] = "wrong-type-" + _SENTINEL
        path.write_text(json.dumps(data))
    with patch.dict(os.environ, _bootstrap(), clear=True):
        with pytest.raises((ValueError, FileNotFoundError)) as error:
            sb.run_launcher(
                _encoded(_policy(False)), _TARGET, ["doctor"], target_environment_path=str(path)
            )
    inert_launcher.run.assert_not_called()
    inert_launcher.cleanup.assert_called_once_with(None)
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


@pytest.mark.parametrize("fault", ["payload", "spawn"])
def test_target_failure_cleans_owned_launcher_scratch(
    tmp_path: Path, inert_launcher, fault: str
) -> None:
    path = _payload(tmp_path)
    if fault == "payload":
        path.unlink()
    else:
        inert_launcher.run.side_effect = OSError("inert target failure")
    with patch.dict(os.environ, _bootstrap(wrapped=True), clear=True):
        with pytest.raises(OSError):
            sb.run_launcher(
                _encoded(_policy(True)), _TARGET, ["doctor"], target_environment_path=str(path)
            )
    inert_launcher.cleanup.assert_called_once_with(inert_launcher.scratch)


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
    with patch.dict(os.environ, _bootstrap(wrapped=active), clear=True):
        sb.run_launcher(
            _encoded(_policy(active)), _TARGET, ["doctor"], target_environment_path=str(path)
        )
    environment = inert_launcher.run.call_args.kwargs["env"]
    assert not set(RUNNER_AUTH_SECRET_ENV_VARS).intersection(environment)
    assert SHARED_ENVIRONMENT_VAR not in environment
    assert (sb.HOST_SANDBOX_BACKEND_ENV in environment) is not active
