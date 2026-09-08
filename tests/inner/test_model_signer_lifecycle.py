"""Bidirectional lifecycle tests for signer-backed Codex sessions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner.codex_executor import _CodexAppServerSession
from omnigent.inner.codex_worker import CodexWorkerLaunch
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.model_signer import SignerReadiness


class _Pipe:
    async def read(self, size: int) -> bytes:
        await asyncio.sleep(3600)
        return b""

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _Process:
    def __init__(self) -> None:
        self.stdin = None
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.returncode: int | None = None
        self.pid = 12345

    async def wait(self) -> int:
        return self.returncode or 0


class _Signer:
    def __init__(
        self,
        order: list[str],
        *,
        start_error: Exception | None = None,
    ) -> None:
        self.order = order
        self.start_error = start_error
        self.exited = asyncio.Event()
        self.closed = False

    async def start(self) -> SignerReadiness:
        self.order.append("signer-start")
        if self.start_error is not None:
            raise self.start_error
        return SignerReadiness(
            relay_port=43123,
            socket_path=Path("/private/signer/relay.sock"),
            ca_bundle_path=Path("/private/signer/ca.pem"),
            placeholder="oa_cred_session",
        )

    async def wait(self) -> int:
        await self.exited.wait()
        return 0

    async def close(self) -> None:
        self.order.append("signer-close")
        self.closed = True
        self.exited.set()


def _session(tmp_path: Path, signer: _Signer) -> _CodexAppServerSession:
    return _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
        signer_factory=lambda: signer,
    )


async def test_signer_preflights_before_codex_state_and_worker_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    signer = _Signer(order)
    process = _Process()

    def _populate(*args: object, **kwargs: object) -> None:
        order.append("populate-codex-home")

    def _prepare(**kwargs: object) -> CodexWorkerLaunch:
        order.append("prepare-worker")
        return CodexWorkerLaunch("/private/sandbox-launcher", sandboxed=True)

    async def _spawn(*args: object, **kwargs: object) -> _Process:
        order.append("spawn-worker")
        return process

    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", _populate)
    monkeypatch.setattr("omnigent.inner.codex_executor.prepare_codex_worker", _prepare)
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", _spawn)
    session = _session(tmp_path, signer)
    session._request = AsyncMock(return_value={"result": {}})

    await session.start()

    assert order[:4] == [
        "signer-start",
        "populate-codex-home",
        "prepare-worker",
        "spawn-worker",
    ]
    await session.close()


async def test_signer_preflight_failure_never_creates_codex_home_or_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([], start_error=RuntimeError("PROVIDER_AUTH_REQUIRED"))
    spawn = AsyncMock()
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", spawn)
    session = _session(tmp_path, signer)

    with pytest.raises(RuntimeError, match="PROVIDER_AUTH_REQUIRED"):
        await session.start()

    spawn.assert_not_awaited()
    assert session._codex_home_dir is None
    assert not list(tmp_path.glob(".codex-tmp/omnigent-codex-home-*"))


async def test_failure_after_signer_readiness_closes_signer_and_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
    spawn = AsyncMock()
    monkeypatch.setattr(
        "omnigent.inner.codex_executor._populate_codex_home_config",
        Mock(side_effect=OSError("config failed")),
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", spawn)
    session = _session(tmp_path, signer)

    with pytest.raises(OSError, match="config failed"):
        await session.start()

    assert signer.closed
    spawn.assert_not_awaited()
    assert session._signer is None
    assert session._codex_home_dir is None


async def test_retry_uses_a_fresh_signer_after_failed_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    failed = _Signer(order, start_error=RuntimeError("PROVIDER_AUTH_REQUIRED"))
    ready = _Signer(order)
    signers = iter((failed, ready))
    process = _Process()
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(return_value=CodexWorkerLaunch("/private/sandbox-launcher", sandboxed=True)),
    )
    monkeypatch.setattr(
        "omnigent.inner.codex_executor._create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
        signer_factory=lambda: next(signers),
    )
    session._request = AsyncMock(return_value={"result": {}})

    with pytest.raises(RuntimeError, match="PROVIDER_AUTH_REQUIRED"):
        await session.start()
    await session.start()

    assert failed.closed
    assert session._signer is ready
    assert order.count("signer-start") == 2
    await session.close()


async def test_signer_exit_terminates_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
    process = _Process()
    terminate = Mock()
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(return_value=CodexWorkerLaunch("/private/sandbox-launcher", sandboxed=True)),
    )
    monkeypatch.setattr(
        "omnigent.inner.codex_executor._create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())
    monkeypatch.setattr("omnigent.inner.codex_executor._terminate_process_tree", terminate)
    session = _session(tmp_path, signer)
    session._request = AsyncMock(return_value={"result": {}})
    await session.start()

    signer.exited.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    terminate.assert_called_once_with(process)
    await session.close()


async def test_runner_close_invalidates_signer_before_terminating_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    signer = _Signer(order)
    process = _Process()

    def _terminate(proc: object) -> None:
        assert proc is process
        order.append("worker-terminate")
        process.returncode = 0

    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(return_value=CodexWorkerLaunch("/private/sandbox-launcher", sandboxed=True)),
    )
    monkeypatch.setattr(
        "omnigent.inner.codex_executor._create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())
    monkeypatch.setattr("omnigent.inner.codex_executor._terminate_process_tree", _terminate)
    session = _session(tmp_path, signer)
    session._request = AsyncMock(return_value={"result": {}})
    await session.start()

    await session.close()

    assert order[-2:] == ["signer-close", "worker-terminate"]
