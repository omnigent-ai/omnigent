"""Fail-closed containment tests for the stdio Codex worker."""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import itertools
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner.codex_executor import _CodexAppServerSession
from omnigent.inner.codex_worker import (
    _BROKERED_AUTH_SECRET_ENV,
    CodexWorkerLaunch,
    prepare_codex_catalog_probe,
    prepare_codex_worker,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.model_signer import SignerReadiness
from omnigent.inner.sandbox import (
    SandboxPolicy,
    _launcher_inline_source,
    create_exec_launcher,
    with_additional_write_roots,
)
from tests.e2e._harness_probes import bwrap_namespace_unavailable


class _Pipe:
    async def read(self, size: int) -> bytes:
        return b""

    async def readline(self) -> bytes:
        return b""

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _active_policy(workspace: Path) -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="darwin_seatbelt",
        active=True,
        read_roots=[workspace],
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def test_active_sandbox_wrap_failure_is_not_downgraded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=_active_policy(tmp_path)),
    )
    monkeypatch.setattr(
        "omnigent.inner.codex_worker.get_backend",
        Mock(side_effect=OSError("seatbelt unavailable")),
    )

    with pytest.raises(OSError, match="seatbelt unavailable"):
        prepare_codex_worker(
            codex_path=str(codex),
            cwd=tmp_path,
            codex_home=codex_home,
            os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
            spawn_env_names=["PATH", "CODEX_HOME"],
        )


def test_active_sandbox_preflights_before_creating_launcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    backend = Mock()
    backend.wrap_launcher_argv.side_effect = OSError("cannot wrap")
    create_launcher = Mock()

    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=_active_policy(tmp_path)),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.get_backend", Mock(return_value=backend))
    monkeypatch.setattr("omnigent.inner.codex_worker.create_exec_launcher", create_launcher)

    with pytest.raises(OSError, match="cannot wrap"):
        prepare_codex_worker(
            codex_path=str(codex),
            cwd=tmp_path,
            codex_home=codex_home,
            os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
            spawn_env_names=["PATH", "CODEX_HOME"],
        )

    create_launcher.assert_not_called()


def test_successful_active_sandbox_returns_owned_launcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    launcher = tmp_path / "launcher"
    launcher.touch()
    backend = Mock()
    backend.wrap_launcher_argv.return_value = ["/usr/bin/sandbox-exec", str(codex)]
    captured: dict[str, SandboxPolicy] = {}

    def _create_launcher(target: str, policy: SandboxPolicy) -> str:
        assert target == str(codex)
        captured["policy"] = policy
        return str(launcher)

    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=_active_policy(tmp_path)),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.get_backend", Mock(return_value=backend))
    monkeypatch.setattr("omnigent.inner.codex_worker.create_exec_launcher", _create_launcher)

    worker = prepare_codex_worker(
        codex_path=str(codex),
        cwd=tmp_path,
        codex_home=codex_home,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
        spawn_env_names=["PATH", "CODEX_HOME"],
    )

    assert worker.launch_path == str(launcher)
    assert worker.sandboxed
    assert codex_home.resolve() in captured["policy"].write_roots
    read_roots = captured["policy"].read_roots
    assert read_roots is not None
    assert codex.resolve().parent in read_roots
    assert captured["policy"].spawn_env_allowlist == ["CODEX_HOME", "PATH"]
    assert captured["policy"].allow_network

    worker.close()
    worker.close()
    assert not launcher.exists()


def test_network_denied_worker_without_model_route_runs_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No signer and no egress rules leave a contained app-server no way to reach the model.

    Like the Claude CLI wrap, the worker then runs unwrapped and Codex's own
    sandbox mode keeps confining its tool commands.
    """
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    create_launcher = Mock()
    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=replace(_active_policy(tmp_path), allow_network=False)),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.create_exec_launcher", create_launcher)

    with caplog.at_level(logging.WARNING, logger="omnigent.inner.codex_worker"):
        worker = prepare_codex_worker(
            codex_path=str(codex),
            cwd=tmp_path,
            codex_home=codex_home,
            os_env=OSEnvSpec(
                sandbox=OSEnvSandboxSpec(
                    type="darwin_seatbelt", write_paths=["."], allow_network=False
                )
            ),
            spawn_env_names=["PATH", "CODEX_HOME"],
        )

    assert worker.launch_path == str(codex)
    assert not worker.sandboxed
    assert worker.native_tools_allowed is False
    create_launcher.assert_not_called()
    assert any("no model route" in record.getMessage() for record in caplog.records)
    worker.close()


@pytest.mark.parametrize("grant_skills", [True, False])
def test_worker_grants_only_selected_skills_directory_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, grant_skills: bool
) -> None:
    """Skill access is explicit and never widens the private home's write grant."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    skills_dir = tmp_path / "session-a-skills"
    skills_dir.mkdir()
    unrelated_skills = tmp_path / "session-b-skills"
    unrelated_skills.mkdir()
    launcher = tmp_path / "launcher"
    launcher.touch()
    backend = Mock()
    backend.wrap_launcher_argv.return_value = ["/usr/bin/sandbox-exec", str(codex)]
    create_launcher = Mock(return_value=str(launcher))
    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=_active_policy(workspace)),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.get_backend", Mock(return_value=backend))
    monkeypatch.setattr("omnigent.inner.codex_worker.create_exec_launcher", create_launcher)

    worker = prepare_codex_worker(
        codex_path=str(codex),
        cwd=workspace,
        codex_home=codex_home,
        skills_dir=skills_dir if grant_skills else None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
        spawn_env_names=["PATH", "CODEX_HOME"],
    )
    try:
        for policy in (
            backend.wrap_launcher_argv.call_args.args[1],
            create_launcher.call_args.args[1],
        ):
            assert policy.read_roots is not None
            assert (
                any(skills_dir.resolve().is_relative_to(root) for root in policy.read_roots)
                is grant_skills
            )
            assert not any(
                unrelated_skills.resolve().is_relative_to(root)
                for root in [*policy.read_roots, *policy.write_roots]
            )
            assert policy.write_roots == [codex_home.resolve()]
    finally:
        worker.close()


def test_brokered_catalog_probe_is_network_denied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    launcher = tmp_path / "launcher"
    launcher.touch()
    backend = Mock()
    backend.wrap_launcher_argv.return_value = [
        "/usr/bin/sandbox-exec",
        str(codex),
        "debug",
        "models",
        "--bundled",
    ]
    captured: dict[str, SandboxPolicy] = {}

    def _create_launcher(target: str, policy: SandboxPolicy) -> str:
        assert target == str(codex)
        captured["policy"] = policy
        return str(launcher)

    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=_active_policy(tmp_path)),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.get_backend", Mock(return_value=backend))
    monkeypatch.setattr("omnigent.inner.codex_worker.create_exec_launcher", _create_launcher)

    probe = prepare_codex_catalog_probe(
        codex_path=str(codex),
        cwd=tmp_path,
        codex_home=codex_home,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
        spawn_env_names=["PATH", "HOME", "CODEX_HOME"],
    )

    policy = captured["policy"]
    assert not policy.allow_network
    assert policy.egress_relay_port is None
    assert policy.egress_socket_path is None
    assert policy.spawn_env_allowlist == ["CODEX_HOME", "HOME", "PATH"]
    assert codex_home.resolve() in policy.write_roots

    probe.close()
    assert not launcher.exists()


def test_non_signer_egress_rules_route_only_through_owned_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    launcher = tmp_path / "launcher"
    launcher.touch()
    egress_tmpdir = tmp_path / "egress"
    handle = Mock(
        relay_port=43124,
        socket_path=egress_tmpdir / ".egress.sock",
        ca_bundle_path=egress_tmpdir / "ca-bundle.pem",
    )
    backend = Mock()
    backend.wrap_launcher_argv.return_value = ["/usr/bin/sandbox-exec", str(codex)]
    captured: dict[str, SandboxPolicy] = {}

    def _create_launcher(target: str, policy: SandboxPolicy) -> str:
        captured["policy"] = policy
        return str(launcher)

    def _create_tmpdir() -> Path:
        egress_tmpdir.mkdir()
        return egress_tmpdir

    start_proxy = Mock(return_value=handle)
    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=_active_policy(tmp_path)),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.get_backend", Mock(return_value=backend))
    monkeypatch.setattr("omnigent.inner.codex_worker.create_exec_launcher", _create_launcher)
    monkeypatch.setattr("omnigent.inner.codex_worker.create_private_tmpdir", _create_tmpdir)
    monkeypatch.setattr("omnigent.inner.codex_worker.start_egress_proxy", start_proxy)
    worker_env = {"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)}

    worker = prepare_codex_worker(
        codex_path=str(codex),
        cwd=tmp_path,
        codex_home=codex_home,
        os_env=OSEnvSpec(
            sandbox=OSEnvSandboxSpec(
                type="darwin_seatbelt",
                egress_rules=["POST api.example.com/v1/responses"],
                egress_allow_private_destinations=True,
            )
        ),
        spawn_env_names=list(worker_env),
        worker_env=worker_env,
    )

    policy = captured["policy"]
    assert not policy.allow_network
    assert policy.egress_relay_port == handle.relay_port
    assert policy.egress_socket_path == str(handle.socket_path)
    assert egress_tmpdir in policy.write_roots
    assert worker_env["HTTPS_PROXY"] == "http://127.0.0.1:43124"
    assert worker_env["SSL_CERT_FILE"] == str(handle.ca_bundle_path)
    assert {"HTTPS_PROXY", "SSL_CERT_FILE"} <= set(policy.spawn_env_allowlist or [])
    start_proxy.assert_called_once_with(
        rules=["POST api.example.com/v1/responses"],
        tmpdir=egress_tmpdir,
        allow_private_destinations=True,
        require_auth=False,
    )

    worker.close()
    worker.close()
    handle.stop.assert_called_once_with()
    assert not egress_tmpdir.exists()


def test_non_signer_egress_setup_rolls_back_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    egress_tmpdir = tmp_path / "egress"
    handle = Mock(
        relay_port=43124,
        socket_path=egress_tmpdir / ".egress.sock",
        ca_bundle_path=egress_tmpdir / "ca-bundle.pem",
    )
    backend = Mock()
    backend.wrap_launcher_argv.return_value = ["/usr/bin/sandbox-exec", str(codex)]

    def _create_tmpdir() -> Path:
        egress_tmpdir.mkdir()
        return egress_tmpdir

    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=_active_policy(tmp_path)),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.get_backend", Mock(return_value=backend))
    monkeypatch.setattr(
        "omnigent.inner.codex_worker.create_exec_launcher",
        Mock(side_effect=OSError("launcher failed")),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.create_private_tmpdir", _create_tmpdir)
    monkeypatch.setattr(
        "omnigent.inner.codex_worker.start_egress_proxy",
        Mock(return_value=handle),
    )

    with pytest.raises(OSError, match="launcher failed"):
        prepare_codex_worker(
            codex_path=str(codex),
            cwd=tmp_path,
            codex_home=codex_home,
            os_env=OSEnvSpec(
                sandbox=OSEnvSandboxSpec(
                    type="darwin_seatbelt",
                    egress_rules=["POST api.example.com/v1/responses"],
                )
            ),
            spawn_env_names=["PATH", "CODEX_HOME"],
            worker_env={"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)},
        )

    handle.stop.assert_called_once_with()
    assert not egress_tmpdir.exists()


def test_signer_readiness_adds_only_relay_and_public_ca(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    signer_dir = tmp_path / "signer"
    signer_dir.mkdir()
    socket_path = signer_dir / "relay.sock"
    socket_path.touch()
    ca_bundle = signer_dir / "ca-bundle.pem"
    ca_bundle.write_text("PUBLIC CA", encoding="utf-8")
    readiness = SignerReadiness(
        relay_port=43123,
        socket_path=socket_path,
        ca_bundle_path=ca_bundle,
        placeholder="oa_cred_session",
    )
    launcher = tmp_path / "launcher"
    launcher.touch()
    backend = Mock()
    backend.wrap_launcher_argv.return_value = ["/usr/bin/sandbox-exec", str(codex)]
    captured: dict[str, SandboxPolicy] = {}

    def _create_launcher(target: str, policy: SandboxPolicy) -> str:
        captured["policy"] = policy
        return str(launcher)

    monkeypatch.setattr(
        "omnigent.inner.codex_worker.resolve_sandbox",
        Mock(return_value=_active_policy(tmp_path)),
    )
    monkeypatch.setattr("omnigent.inner.codex_worker.get_backend", Mock(return_value=backend))
    monkeypatch.setattr("omnigent.inner.codex_worker.create_exec_launcher", _create_launcher)
    secret_env = {
        name: f"host-secret-{index}" for index, name in enumerate(_BROKERED_AUTH_SECRET_ENV)
    }
    worker_env = {
        "PATH": os.environ["PATH"],
        "CODEX_HOME": str(codex_home),
        **secret_env,
    }

    worker = prepare_codex_worker(
        codex_path=str(codex),
        cwd=tmp_path,
        codex_home=codex_home,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
        spawn_env_names=list(worker_env),
        signer_readiness=readiness,
        worker_env=worker_env,
    )

    policy = captured["policy"]
    assert policy.egress_relay_port == readiness.relay_port
    assert policy.egress_socket_path == str(readiness.socket_path)
    assert policy.allow_network is False
    assert policy.read_roots is not None
    assert signer_dir in policy.read_roots
    assert {path.name for path in signer_dir.iterdir()} == {"ca-bundle.pem", "relay.sock"}
    assert worker_env["HTTPS_PROXY"] == "http://127.0.0.1:43123"
    assert worker_env["OPENAI_API_KEY"] == readiness.placeholder
    assert worker_env["SSL_CERT_FILE"] == str(readiness.ca_bundle_path)
    assert all(worker_env.get(name) != value for name, value in secret_env.items())
    assert str(readiness.socket_path) not in worker_env.values()
    assert "token" not in str(policy.to_jsonable()).lower()
    worker.close()


def test_signer_readiness_rejects_ordinary_egress_rules(tmp_path: Path) -> None:
    readiness = SignerReadiness(
        relay_port=43123,
        socket_path=Path("/private/signer/relay.sock"),
        ca_bundle_path=Path("/private/signer/ca-bundle.pem"),
        placeholder="oa_cred_session",
    )

    with pytest.raises(
        ValueError,
        match=r"does not support os_env\.sandbox\.egress_rules",
    ):
        prepare_codex_worker(
            codex_path=str(tmp_path / "codex"),
            cwd=tmp_path,
            codex_home=tmp_path / "codex-home",
            os_env=OSEnvSpec(
                sandbox=OSEnvSandboxSpec(
                    type="darwin_seatbelt",
                    egress_rules=["GET api.github.com/repos/company/**"],
                )
            ),
            spawn_env_names=[],
            signer_readiness=readiness,
            worker_env={},
        )


def test_signer_readiness_rejects_unwrapped_worker(tmp_path: Path) -> None:
    readiness = SignerReadiness(
        relay_port=43123,
        socket_path=Path("/private/signer/relay.sock"),
        ca_bundle_path=Path("/private/signer/ca-bundle.pem"),
        placeholder="oa_cred_session",
    )

    with pytest.raises(OSError, match="requires an active sandbox"):
        prepare_codex_worker(
            codex_path=str(tmp_path / "codex"),
            cwd=tmp_path,
            codex_home=tmp_path / "codex-home",
            os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")),
            spawn_env_names=[],
            signer_readiness=readiness,
            worker_env={},
        )


def test_launcher_scratch_clone_preserves_exact_relay(tmp_path: Path) -> None:
    policy = _active_policy(tmp_path)
    policy.egress_relay_port = 43123
    policy.egress_socket_path = "/private/signer/relay.sock"

    cloned = with_additional_write_roots(policy, [tmp_path / "launcher-scratch"])

    assert cloned.egress_relay_port == 43123
    assert cloned.egress_socket_path == "/private/signer/relay.sock"


def test_explicit_none_sandbox_keeps_direct_worker_path(tmp_path: Path) -> None:
    codex = tmp_path / "codex"

    worker = prepare_codex_worker(
        codex_path=str(codex),
        cwd=tmp_path,
        codex_home=tmp_path / "codex-home",
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")),
        spawn_env_names=[],
    )

    assert worker.launch_path == str(codex)
    assert not worker.sandboxed
    worker.close()


async def test_session_containment_failure_prevents_worker_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spawn = AsyncMock()
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(side_effect=OSError("containment failed")),
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", spawn)
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())

    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
    )

    with pytest.raises(OSError, match="containment failed"):
        await session.start()

    spawn.assert_not_awaited()
    assert session._codex_home_dir is None


async def test_session_spawns_owned_launcher_and_releases_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = Mock(launch_path="/private/sandbox-launcher", sandboxed=True)
    process = Mock(
        stdin=None,
        stdout=_Pipe(),
        stderr=_Pipe(),
        returncode=0,
        pid=123,
    )
    process.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(return_value=worker),
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", spawn)
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())

    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
    )
    session._request = AsyncMock(return_value={"result": {}})

    await session.start()

    assert spawn.await_args is not None
    assert Path(spawn.await_args.args[1]).name == "_liveness_exec.py"
    assert "/private/sandbox-launcher" in spawn.await_args.args
    assert spawn.await_args.kwargs["pass_fds"]
    assert session._containment_confirmed
    await session.close()
    worker.close.assert_called_once_with()


async def test_spawn_failure_releases_launcher_and_private_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = Mock(launch_path="/private/sandbox-launcher", sandboxed=True)
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(return_value=worker),
    )
    monkeypatch.setattr(
        "omnigent.inner.codex_executor._create_subprocess_exec",
        AsyncMock(side_effect=OSError("spawn failed")),
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
    )

    with pytest.raises(OSError, match="spawn failed"):
        await session.start()

    worker.close.assert_called_once_with()
    assert session._codex_home_dir is None
    assert session._worker_launch is None


@pytest.mark.parametrize("hooks_staged_as", ["symlink", "copy"])
async def test_session_disables_native_tools_for_unwrapped_no_route_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hooks_staged_as: str,
) -> None:
    """A worker that fell back to running unwrapped keeps no native tools or inherited commands."""
    process = Mock(stdin=None, stdout=_Pipe(), stderr=_Pipe(), returncode=0, pid=123)
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(
            return_value=CodexWorkerLaunch(
                "/bin/echo", sandboxed=False, native_tools_allowed=False
            )
        ),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", spawn)
    user_hooks = tmp_path / "user-hooks.json"
    user_hooks.write_text("{}")

    def _inherit_user_config(target_dir: Path, *_args: object, **_kwargs: object) -> None:
        (target_dir / "config.toml").write_text(
            'model = "gpt-5.4-mini"\nnotify = ["touch", "notified"]\n'
            '[mcp_servers.probe]\ncommand = "touch"\nargs = ["mcp-started"]\n'
        )
        if hooks_staged_as == "symlink":
            (target_dir / "hooks.json").symlink_to(user_hooks)
        else:
            shutil.copy2(user_hooks, target_dir / "hooks.json")

    monkeypatch.setattr(
        "omnigent.inner.codex_executor._populate_codex_home_config", _inherit_user_config
    )
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        os_env=OSEnvSpec(
            sandbox=OSEnvSandboxSpec(type="linux_bwrap", write_paths=["."], allow_network=False)
        ),
    )
    session._request = AsyncMock(return_value={"result": {}})

    await session.start()

    assert session._disable_native_tools is True
    assert not session._containment_confirmed
    assert session._codex_home_dir is not None
    effective = (session._codex_home_dir / "config.toml").read_text()
    assert "mcp_servers" not in effective and "notify" not in effective, effective
    assert 'model = "gpt-5.4-mini"' in effective
    assert not (session._codex_home_dir / "hooks.json").exists()
    overrides = set(itertools.pairwise(spawn.await_args.args))
    for feature in (
        "features.unified_exec=false",
        "features.browser_use=false",
        'web_search="disabled"',
    ):
        assert ("-c", feature) in overrides, spawn.await_args.args
    await session.close()


async def test_native_shell_tool_is_disabled_even_without_dynamic_tools() -> None:
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd="/tmp/workspace",
        env={},
        tool_executor=None,
    )
    session.start = AsyncMock()
    session._proc = Mock()
    session._disable_native_tools = True
    session._request = AsyncMock(
        side_effect=[
            {"result": {"thread": {"id": "thread-1"}}},
            {"result": {"turn": {"id": "turn-1"}}},
        ]
    )

    async def _complete_turn() -> None:
        await asyncio.sleep(0)
        session._events.put_nowait(
            {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}
        )

    completion = asyncio.create_task(_complete_turn())
    _ = [
        event
        async for event in session.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            model="gpt-5.4-mini",
            cwd="/tmp/workspace",
            sandbox="workspace-write",
        )
    ]
    await completion

    thread_params = session._request.await_args_list[0].args[1]
    assert thread_params["config"]["features.shell_tool"] is False
    assert thread_params["config"]["features.unified_exec"] is False
    assert thread_params["sandbox"] == "workspace-write"


async def test_nested_codex_sandbox_is_disabled_only_after_confirmation() -> None:
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd="/tmp/workspace",
        env={},
        tool_executor=None,
    )
    session.start = AsyncMock()
    session._proc = Mock()
    session._containment_confirmed = True
    session._request = AsyncMock(
        side_effect=[
            {"result": {"thread": {"id": "thread-1"}}},
            {"result": {"turn": {"id": "turn-1"}}},
        ]
    )

    async def _complete_turn() -> None:
        await asyncio.sleep(0)
        session._events.put_nowait(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1"}},
            }
        )

    completion = asyncio.create_task(_complete_turn())
    _ = [
        event
        async for event in session.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            model="gpt-5.4-mini",
            cwd="/tmp/workspace",
            sandbox="workspace-write",
        )
    ]
    await completion

    thread_params = session._request.await_args_list[0].args[1]
    assert thread_params["sandbox"] == "danger-full-access"


def _egress_filtered_bwrap_spec(workspace: Path, **overrides: object) -> OSEnvSpec:
    """The reporter's network-denied sandbox plus an egress rule for the loopback model host."""
    sandbox: dict[str, object] = {
        "type": "linux_bwrap",
        "write_paths": ["."],
        "allow_network": False,
        "egress_rules": ["* 127.0.0.1/**"],
        "egress_allow_private_destinations": True,
        **overrides,
    }
    return OSEnvSpec(
        type="caller_process", cwd=str(workspace), sandbox=OSEnvSandboxSpec(**sandbox)
    )


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="linux_bwrap requires Linux with bubblewrap installed",
)
def test_spawn_time_wrap_keeps_egress_socket_unmasked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher's bwrap argv must not hide the relay socket of its own egress tmpdir.

    Runs the generated launcher program with ``os.execvp`` intercepted, so the
    exact mount plan the worker would be spawned with is inspected without
    needing user namespaces.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    launchers: list[tuple[str, SandboxPolicy]] = []

    def _record_launcher(target: str, policy: SandboxPolicy, **kwargs: object) -> str:
        launchers.append((target, policy))
        return create_exec_launcher(target, policy, **kwargs)

    with contextlib.ExitStack() as cleanup:
        egress_tmpdir = Path(tempfile.mkdtemp(prefix="omnigent-osenv-"))
        cleanup.callback(shutil.rmtree, egress_tmpdir, ignore_errors=True)
        relay_socket = cleanup.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        relay_socket.bind(str(egress_tmpdir / ".egress.sock"))
        handle = Mock(
            relay_port=43124,
            socket_path=egress_tmpdir / ".egress.sock",
            ca_bundle_path=egress_tmpdir / "ca-bundle.pem",
        )
        monkeypatch.setattr(
            "omnigent.inner.codex_worker.create_private_tmpdir", lambda: egress_tmpdir
        )
        monkeypatch.setattr(
            "omnigent.inner.codex_worker.start_egress_proxy", Mock(return_value=handle)
        )
        monkeypatch.setattr("omnigent.inner.codex_worker.create_exec_launcher", _record_launcher)
        worker_env = {"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)}
        worker = prepare_codex_worker(
            codex_path=str(codex),
            cwd=workspace,
            codex_home=codex_home,
            os_env=_egress_filtered_bwrap_spec(workspace),
            spawn_env_names=list(worker_env),
            worker_env=worker_env,
        )
        cleanup.callback(worker.close)
        ((target, policy),) = launchers
        program = (
            "import json, os, sys\n"
            "def _capture(file, argv):\n"
            "    print('WRAP ' + json.dumps(argv), flush=True)\n"
            "    raise SystemExit(0)\n"
            "os.execvp = _capture\n"
            f"exec({_launcher_inline_source(target, policy, cwd=str(workspace))!r})\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program, "app-server"],
            cwd=workspace,
            env=worker_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    wrap_lines = [line for line in completed.stdout.splitlines() if line.startswith("WRAP ")]
    assert wrap_lines, completed.stdout
    argv = json.loads(wrap_lines[-1][len("WRAP ") :])
    socket_masks: list[list[str]] = []
    for index, token in enumerate(argv):
        if (
            token == "--tmpfs"
            and index + 1 < len(argv)
            and argv[index + 1].startswith(str(egress_tmpdir))
        ):
            socket_masks.append(argv[index : index + 2])
        if (
            token == "--bind-try"
            and index + 2 < len(argv)
            and argv[index + 1] == "/dev/null"
            and argv[index + 2].startswith(str(egress_tmpdir))
        ):
            socket_masks.append(argv[index : index + 3])
    assert not socket_masks, (
        f"the spawn-time wrap masks the worker's own egress relay socket: {socket_masks}"
    )


@pytest.mark.skipif(
    bwrap_namespace_unavailable() is not None,
    reason=f"cannot execute bwrap namespaces here: {bwrap_namespace_unavailable()}",
)
def test_real_bwrap_worker_reaches_egress_relay_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An egress-filtered worker must see the relay's Unix socket, not a masked stub.

    The egress tmpdir is a framework-owned write root; if the spawn-time
    dotfile mask hides its ``.egress.sock`` the in-namespace relay cannot
    reach the proxy and every model request dies with a connection reset.
    """
    with contextlib.ExitStack() as cleanup:
        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
        cleanup.callback(upstream.server_close)
        cleanup.callback(upstream.shutdown)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        upstream_port = upstream.server_address[1]
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("workspace\n")
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        codex = tmp_path / "codex"
        codex.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json, os, stat, urllib.request
                sock = os.path.join(os.environ["EGRESS_TMPDIR"], ".egress.sock")
                report = {{"is_socket": stat.S_ISSOCK(os.lstat(sock).st_mode)}}
                proxy = os.environ["HTTP_PROXY"]
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({{"http": proxy, "https": proxy}})
                )
                try:
                    with opener.open("http://127.0.0.1:{upstream_port}/", timeout=15) as resp:
                        report["status"] = resp.status
                except Exception as exc:
                    report["error"] = f"{{type(exc).__name__}}: {{exc}}"
                print(json.dumps(report))
                """
            ),
            encoding="utf-8",
        )
        codex.chmod(0o755)
        egress_tmpdir = Path(tempfile.mkdtemp(prefix="omnigent-osenv-"))
        cleanup.callback(shutil.rmtree, egress_tmpdir, ignore_errors=True)
        monkeypatch.setattr(
            "omnigent.inner.codex_worker.create_private_tmpdir", lambda: egress_tmpdir
        )
        worker_env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "CODEX_HOME": str(codex_home),
            "EGRESS_TMPDIR": str(egress_tmpdir),
        }
        worker = prepare_codex_worker(
            codex_path=str(codex),
            cwd=workspace,
            codex_home=codex_home,
            os_env=_egress_filtered_bwrap_spec(
                workspace,
                read_paths=[str(Path(__file__).resolve().parents[2] / "omnigent"), sys.prefix],
                cwd_hidden_scan_overflow="error",
            ),
            spawn_env_names=list(worker_env),
            worker_env=worker_env,
        )
        cleanup.callback(worker.close)
        completed = subprocess.run(
            [worker.launch_path, "app-server"],
            cwd=workspace,
            env=worker_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report.get("is_socket") is True, (
        f"the relay socket is not a socket inside the worker sandbox: {report}; "
        f"stderr={completed.stderr.strip()!r}"
    )
    assert report.get("status") == 200, (
        f"request through the egress relay failed: {report}; stderr={completed.stderr.strip()!r}"
    )


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_real_seatbelt_worker_cannot_write_outside_grants(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    probe_name = f".omnigent-codex-worker-probe-{uuid.uuid4().hex}"
    forbidden = Path.home() / probe_name
    codex.write_text(
        f'#!/bin/sh\nif touch "$HOME/{probe_name}" 2>/dev/null; then exit 91; fi\nexit 0\n',
        encoding="utf-8",
    )
    codex.chmod(0o755)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    worker = prepare_codex_worker(
        codex_path=str(codex),
        cwd=tmp_path,
        codex_home=codex_home,
        os_env=OSEnvSpec(
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(
                type="darwin_seatbelt",
                # Network stays allowed so the worker is wrapped; the no-route
                # fallback is covered separately.
                allow_network=True,
                cwd_hidden_scan_overflow="error",
            ),
        ),
        spawn_env_names=["HOME", "PATH"],
    )

    try:
        completed = subprocess.run(
            [worker.launch_path, "app-server"],
            cwd=tmp_path,
            env={"HOME": str(Path.home()), "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        escaped = forbidden.exists()
    finally:
        worker.close()
        forbidden.unlink(missing_ok=True)

    assert completed.returncode == 0, completed.stderr
    assert not escaped


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_real_seatbelt_worker_reads_only_public_signer_state_and_relay_network(
    tmp_path: Path,
) -> None:
    unique = uuid.uuid4().hex
    signer_public = Path(tempfile.mkdtemp(prefix="osp-", dir="/tmp")).resolve()
    signer_private = Path(tempfile.mkdtemp(prefix="osr-", dir="/tmp")).resolve()
    socket_path = signer_public / "relay.sock"
    unrelated_socket_path = signer_public / "unrelated.sock"
    ca_bundle = signer_public / "ca-bundle.pem"
    ca_bundle.write_text("PUBLIC CA", encoding="utf-8")
    private_marker = signer_private / "bearer-token"
    private_marker.write_text("PRIVATE SIGNER TOKEN", encoding="utf-8")
    host_marker = Path.home() / f".omnigent-host-credential-{unique}"
    host_marker.write_text("HOST CREDENTIAL", encoding="utf-8")

    unix_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_listener.bind(str(socket_path))
    unix_listener.listen(1)
    unrelated_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unrelated_listener.bind(str(unrelated_socket_path))
    unrelated_listener.listen(1)
    host_socket_candidates = [
        value
        for value in (
            "/var/run/docker.sock",
            os.environ.get("SSH_AUTH_SOCK"),
            os.environ.get("DATABRICKS_SDK_SERVICE"),
        )
        if value and Path(value).exists()
    ]
    direct_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    direct_listener.bind(("127.0.0.1", 0))
    direct_listener.listen(1)
    direct_port = int(direct_listener.getsockname()[1])
    relay_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    relay_probe.bind(("127.0.0.1", 0))
    relay_port = int(relay_probe.getsockname()[1])
    relay_probe.close()

    codex = tmp_path / "codex"
    codex.write_text(
        "#!/usr/bin/python3\n"
        "import os, pathlib, socket, sys\n"
        f"for path in ({str(private_marker)!r}, {str(host_marker)!r}):\n"
        "    try:\n"
        "        pathlib.Path(path).read_bytes()\n"
        "    except OSError:\n"
        "        pass\n"
        "    else:\n"
        "        sys.exit(91)\n"
        f"assert {unrelated_socket_path.name!r} in os.listdir({str(signer_public)!r})\n"
        f"for path in {[str(unrelated_socket_path), *host_socket_candidates]!r}:\n"
        "    denied = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "    try:\n"
        "        denied.connect(path)\n"
        "    except OSError:\n"
        "        pass\n"
        "    else:\n"
        "        sys.exit(93)\n"
        "    finally:\n"
        "        denied.close()\n"
        f"relay = socket.create_connection(('127.0.0.1', {relay_port}), timeout=3)\n"
        "relay.close()\n"
        "try:\n"
        f"    direct = socket.create_connection(('127.0.0.1', {direct_port}), timeout=1)\n"
        "except OSError:\n"
        "    sys.exit(0)\n"
        "direct.close()\n"
        "sys.exit(92)\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    readiness = SignerReadiness(
        relay_port=relay_port,
        socket_path=socket_path,
        ca_bundle_path=ca_bundle,
        placeholder="oa_cred_session",
    )
    worker_env = {"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)}
    worker = prepare_codex_worker(
        codex_path=str(codex),
        cwd=tmp_path,
        codex_home=codex_home,
        os_env=OSEnvSpec(
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(
                type="darwin_seatbelt",
                allow_network=False,
                cwd_hidden_scan_overflow="error",
            ),
        ),
        spawn_env_names=list(worker_env),
        signer_readiness=readiness,
        worker_env=worker_env,
    )

    try:
        completed = subprocess.run(
            [worker.launch_path, "app-server"],
            cwd=tmp_path,
            env=worker_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        worker.close()
        unix_listener.close()
        unrelated_listener.close()
        direct_listener.close()
        host_marker.unlink(missing_ok=True)
        private_marker.unlink(missing_ok=True)
        socket_path.unlink(missing_ok=True)
        unrelated_socket_path.unlink(missing_ok=True)
        ca_bundle.unlink(missing_ok=True)
        signer_public.rmdir()
        signer_private.rmdir()

    assert completed.returncode == 0, completed.stderr
