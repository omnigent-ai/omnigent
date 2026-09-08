"""Fail-closed containment tests for the stdio Codex worker."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner.codex_executor import _CodexAppServerSession
from omnigent.inner.codex_worker import prepare_codex_worker
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import SandboxPolicy


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
    assert not captured["policy"].allow_network

    worker.close()
    worker.close()
    assert not launcher.exists()


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
    assert spawn.await_args.args[0] == "/private/sandbox-launcher"
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
                allow_network=False,
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
