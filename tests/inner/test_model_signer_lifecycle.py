"""Bidirectional lifecycle tests for signer-backed Codex sessions."""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner.codex_executor import _CodexAppServerSession, _populate_codex_home_config
from omnigent.inner.codex_worker import CodexWorkerLaunch
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.model_signer import SignerReadiness, SignerStartError


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


class _EofPipe(_Pipe):
    async def read(self, size: int) -> bytes:
        return b""


class _Process:
    def __init__(self) -> None:
        self.stdin = None
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.returncode: int | None = None
        self.pid = 12345

    async def wait(self) -> int:
        return self.returncode or 0


class _TermIgnoringProcess(_Process):
    def __init__(self) -> None:
        super().__init__()
        self.killed = asyncio.Event()

    async def wait(self) -> int:
        await self.killed.wait()
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


class _BlockingSigner(_Signer):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.starting = asyncio.Event()

    async def start(self) -> SignerReadiness:
        self.order.append("signer-start")
        self.starting.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _BlockingCloseSigner(_Signer):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.closing = asyncio.Event()

    async def close(self) -> None:
        self.order.append("signer-close")
        self.closing.set()
        await asyncio.sleep(3600)


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
        readiness = kwargs["signer_readiness"]
        assert isinstance(readiness, SignerReadiness)
        worker_env = kwargs["worker_env"]
        assert isinstance(worker_env, dict)
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


async def test_signer_exit_before_worker_spawn_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
    spawn = AsyncMock()

    def _prepare(**kwargs: object) -> CodexWorkerLaunch:
        del kwargs
        signer.exited.set()
        return CodexWorkerLaunch("/private/sandbox-launcher", sandboxed=True)

    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())
    monkeypatch.setattr("omnigent.inner.codex_executor.prepare_codex_worker", _prepare)
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", spawn)
    session = _session(tmp_path, signer)

    with pytest.raises(SignerStartError, match="exited during worker startup"):
        await session.start()

    spawn.assert_not_awaited()
    assert signer.closed
    assert session._worker_launch is None


async def test_signer_exit_during_worker_spawn_tears_worker_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
    process = _Process()
    terminate = Mock()

    async def _spawn(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        signer.exited.set()
        await asyncio.sleep(0)
        return process

    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(return_value=CodexWorkerLaunch("/private/sandbox-launcher", sandboxed=True)),
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", _spawn)
    monkeypatch.setattr("omnigent.inner.codex_executor._terminate_process_tree", terminate)
    session = _session(tmp_path, signer)

    with pytest.raises(SignerStartError, match="exited during worker startup"):
        await session.start()

    terminate.assert_called_with(process)
    assert signer.closed
    assert session._proc is None
    assert session._worker_launch is None


async def test_signer_backed_home_excludes_host_credential_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
    process = _Process()
    populate = Mock()
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", populate)
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(return_value=CodexWorkerLaunch("/private/sandbox-launcher", sandboxed=True)),
    )
    monkeypatch.setattr(
        "omnigent.inner.codex_executor._create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    session = _session(tmp_path, signer)
    session._request = AsyncMock(return_value={"result": {}})

    await session.start()

    assert populate.call_args.kwargs["include_credentials"] is False
    assert populate.call_args.kwargs["minimal_config"] is True
    await session.close()


async def test_signer_backed_home_is_private_and_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
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
    session = _session(tmp_path, signer)
    session._request = AsyncMock(return_value={"result": {}})

    await session.start()

    codex_home = session._codex_home_dir
    assert codex_home is not None
    assert not codex_home.is_relative_to(tmp_path)
    assert stat.S_IMODE(codex_home.stat().st_mode) == 0o700
    await session.close()
    assert not codex_home.exists()


async def test_signer_backed_home_rejects_symlink_temp_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real-temp"
    real_root.mkdir(mode=0o700)
    symlink_root = tmp_path / "temp-link"
    symlink_root.symlink_to(real_root, target_is_directory=True)
    signer = _Signer([])
    monkeypatch.setattr("tempfile.gettempdir", lambda: os.fspath(symlink_root))
    session = _session(tmp_path, signer)

    with pytest.raises(OSError, match="unsafe signer session temp root"):
        await session.start()

    assert signer.closed
    assert not list(real_root.iterdir())


def test_credential_exclusion_keeps_config_but_not_host_auth(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "auth.json").write_text('{"token":"host-secret"}', encoding="utf-8")
    (source / ".credentials.json").write_text('{"token":"mcp-secret"}', encoding="utf-8")
    (source / "memories_1.sqlite").write_bytes(b"host-state")
    (source / "config.toml").write_text(
        'model_provider = "host"\n'
        "[model_providers.host]\n"
        'base_url = "https://untrusted.example"\n'
        'experimental_bearer_token = "host-secret"\n',
        encoding="utf-8",
    )

    _populate_codex_home_config(target, source, include_credentials=False)

    assert not (target / "auth.json").exists()
    assert not (target / ".credentials.json").exists()
    assert not (target / "memories_1.sqlite").exists()
    assert not (target / "config.toml").exists()


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


async def test_signer_exit_escalates_to_kill_for_term_ignoring_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
    process = _TermIgnoringProcess()
    terminate = Mock()

    def _kill(proc: object) -> None:
        assert proc is process
        process.returncode = -9
        process.killed.set()

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
    monkeypatch.setattr("omnigent.inner.codex_executor._kill_process_tree", _kill)
    monkeypatch.setattr("omnigent.inner.codex_executor._WORKER_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    session = _session(tmp_path, signer)
    session._request = AsyncMock(return_value={"result": {}})
    await session.start()

    signer.exited.set()
    assert session._signer_watch_task is not None
    await session._signer_watch_task

    terminate.assert_called_once_with(process)
    assert process.returncode == -9
    await session.close()


async def test_runner_close_terminates_worker_without_waiting_for_signer(
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

    assert order[-2:] == ["worker-terminate", "signer-close"]


async def test_cancelled_close_contains_worker_and_retains_incomplete_signer_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _BlockingCloseSigner([])
    process = _Process()
    terminate = Mock(side_effect=lambda proc: setattr(proc, "returncode", 0))
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
    monkeypatch.setattr("omnigent.inner.codex_executor._SIGNER_CLOSE_TIMEOUT_SECONDS", 0.01)
    session = _session(tmp_path, signer)
    session._request = AsyncMock(return_value={"result": {}})
    await session.start()

    close_task = asyncio.create_task(session.close())
    await signer.closing.wait()
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    terminate.assert_called_once_with(process)
    assert not session.cleaned
    assert session._proc is None
    assert session._signer is signer


async def test_cancelled_start_reclaims_worker_prepared_in_background_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
    worker = Mock(launch_path="/private/sandbox-launcher", sandboxed=True)
    started = threading.Event()
    release = threading.Event()

    def _prepare(**kwargs: object) -> Mock:
        del kwargs
        started.set()
        assert release.wait(timeout=5)
        return worker

    spawn = AsyncMock()
    monkeypatch.setattr("omnigent.inner.codex_executor.prepare_codex_worker", _prepare)
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", spawn)
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())
    session = _session(tmp_path, signer)

    start_task = asyncio.create_task(session.start())
    assert await asyncio.to_thread(started.wait, 5)
    start_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    worker.close.assert_called_once_with()
    spawn.assert_not_awaited()
    assert session.cleaned


async def test_concurrent_close_during_spawn_reaps_returned_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = _Signer([])
    worker = Mock(launch_path="/private/sandbox-launcher", sandboxed=True)
    process = _Process()
    spawning = asyncio.Event()
    release = asyncio.Event()
    terminate = Mock(side_effect=lambda proc: setattr(proc, "returncode", 0))

    async def _spawn(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        spawning.set()
        await release.wait()
        return process

    monkeypatch.setattr(
        "omnigent.inner.codex_executor.prepare_codex_worker",
        Mock(return_value=worker),
    )
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", _spawn)
    monkeypatch.setattr("omnigent.inner.codex_executor._populate_codex_home_config", Mock())
    monkeypatch.setattr("omnigent.inner.codex_executor._terminate_process_tree", terminate)
    session = _session(tmp_path, signer)

    start_task = asyncio.create_task(session.start())
    await spawning.wait()
    close_task = asyncio.create_task(session.close())
    release.set()
    with pytest.raises(RuntimeError, match="closed during worker spawn"):
        await start_task
    await close_task

    terminate.assert_called_once_with(process)
    assert session._proc is None
    assert session.cleaned


async def test_cancelled_start_closes_partially_started_signer(tmp_path: Path) -> None:
    signer = _BlockingSigner([])
    session = _session(tmp_path, signer)

    start = asyncio.create_task(session.start())
    await signer.starting.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert signer.closed
    assert session._signer is None


async def test_worker_stdout_eof_invalidates_signer(tmp_path: Path) -> None:
    signer = _Signer([])
    process = _Process()
    process.stdout = _EofPipe()
    session = _session(tmp_path, signer)
    session._signer = signer
    session._proc = process

    await session._reader_loop()

    assert signer.closed
    assert session._signer is None
