"""Sandbox helpers outlive individual async callers and keep an owned shutdown."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner import os_env
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="requires Linux and bwrap to exercise parent-thread lifetime",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _environment(tmp_path: Path, name: str = "workspace", *, active: bool = True) -> Any:
    workspace = tmp_path / name
    workspace.mkdir()
    (workspace / "file.txt").write_text(name)
    environment = os_env.create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(
                type="linux_bwrap" if active else "none",
                read_paths=[str(_REPO_ROOT)],
                write_paths=[str(workspace)],
            ),
        )
    )
    assert environment is not None
    return environment


def _record_starts(client: Any, monkeypatch: pytest.MonkeyPatch) -> list[threading.Thread]:
    owners: list[threading.Thread] = []
    original = client._start_unchecked_locked

    def start() -> None:
        owners.append(threading.current_thread())
        original()

    monkeypatch.setattr(client, "_start_unchecked_locked", start)
    return owners


async def _wait_for(event: threading.Event) -> None:
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(0.005)


async def _assert_survives_request_thread(process: Any) -> None:
    for _ in range(10):
        await asyncio.sleep(0.02)
        assert process.poll() is None, (
            "the helper died after the thread serving its async request returned"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["async-bwrap", "main-prestarted-bwrap", "async-none"])
async def test_helper_survives_a_completed_public_read(tmp_path: Path, case: str) -> None:
    environment = _environment(tmp_path, active=case != "async-none")
    try:
        if case == "main-prestarted-bwrap":
            assert (
                environment._helper.request({"op": "read", "path": "file.txt"})["content"]
                == "workspace"
            )
        result = await environment.read("file.txt", offset=1, limit=None)
        assert result["content"] == "workspace"
        process = environment._helper._proc
        assert process is not None
        await _assert_survives_request_thread(process)
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_close_stops_the_helper_before_joining_its_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    client = environment._helper
    owners = _record_starts(client, monkeypatch)
    stopped_while_owner_alive: list[bool] = []
    original = client._stop_locked

    def stop() -> None:
        stopped_while_owner_alive.append(bool(owners) and owners[0].is_alive())
        original()

    monkeypatch.setattr(client, "_stop_locked", stop)
    try:
        assert (await environment.read("file.txt"))["content"] == "workspace"
        process = client._proc
        assert process is not None
        environment.close()
        assert process.poll() is not None
        assert stopped_while_owner_alive == [True]
        assert len(owners) == 1 and not owners[0].is_alive()
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_clients_have_independent_helper_lifetimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _environment(tmp_path, "first"), _environment(tmp_path, "second")
    owners_a = _record_starts(first._helper, monkeypatch)
    owners_b = _record_starts(second._helper, monkeypatch)
    try:
        a, b = await asyncio.gather(first.read("file.txt"), second.read("file.txt"))
        assert (a["content"], b["content"]) == ("first", "second")
        process_a, process_b = first._helper._proc, second._helper._proc
        assert process_a is not None and process_b is not None
        assert process_a.pid != process_b.pid and owners_a[0] is not owners_b[0]
        first.close()
        assert process_a.poll() is not None and not owners_a[0].is_alive()
        await _assert_survives_request_thread(process_b)
        assert (await second.read("file.txt"))["content"] == "second"
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_restart_uses_the_same_live_owner_after_its_exact_helper_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    owners = _record_starts(environment._helper, monkeypatch)
    try:
        assert (await environment.read("file.txt"))["content"] == "workspace"
        first = environment._helper._proc
        first_tmpdir = environment._helper._tmpdir
        assert first is not None and first_tmpdir is not None and first_tmpdir.is_dir()
        first.terminate()
        async with asyncio.timeout(5):
            while first.poll() is None:
                await asyncio.sleep(0.01)
        assert (await environment.read("file.txt"))["content"] == "workspace"
        second = environment._helper._proc
        assert second is not None and second.pid != first.pid
        assert environment._helper._tmpdir != first_tmpdir
        assert not first_tmpdir.exists(), "the exited helper's private tmpdir leaked"
        assert len(owners) == 2 and owners[0] is owners[1]
        assert owners[0].is_alive()
        await _assert_survives_request_thread(second)
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_concurrent_public_reads_share_one_serialized_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    owners = _record_starts(environment._helper, monkeypatch)
    try:
        results = await asyncio.gather(*(environment.read("file.txt") for _ in range(8)))
        assert [result["content"] for result in results] == ["workspace"] * 8
        assert len(owners) == 1 and owners[0].is_alive()
        process = environment._helper._proc
        assert process is not None
        await _assert_survives_request_thread(process)
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_cancelled_async_caller_does_not_retire_the_owned_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    client = environment._helper
    owners = _record_starts(client, monkeypatch)
    waiting, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = client._start_unchecked_locked

    class HeldReply:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

        def readline(self) -> str:
            line = self.stream.readline()
            assert json.loads(line)["content"] == "workspace"
            waiting.set()
            try:
                assert release.wait(5)
                return line
            finally:
                finished.set()

    def start() -> None:
        original()
        assert client._proc is not None and client._proc.stdout is not None
        client._proc.stdout = HeldReply(client._proc.stdout)

    monkeypatch.setattr(client, "_start_unchecked_locked", start)
    pending = asyncio.create_task(environment.read("file.txt"))
    try:
        await _wait_for(waiting)
        process = client._proc
        assert process is not None and process.poll() is None
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert owners[0].is_alive() and process.poll() is None
        release.set()
        await _wait_for(finished)
        await _assert_survives_request_thread(process)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        environment.close()
    assert not owners[0].is_alive()


@pytest.mark.asyncio
async def test_startup_failure_reclaims_its_process_and_can_retry_on_the_same_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    client = environment._helper
    owners: list[threading.Thread] = []
    started: list[Any] = []
    original = client._start_unchecked_locked

    def start() -> None:
        owners.append(threading.current_thread())
        original()
        assert client._proc is not None
        started.append(client._proc)
        if len(started) == 1:
            raise OSError("startup failed after its exact helper was created")

    monkeypatch.setattr(client, "_start_unchecked_locked", start)
    try:
        with pytest.raises(OSError, match="startup failed"):
            await environment.read("file.txt")
        assert started[0].poll() is not None and client._proc is None
        assert (await environment.read("file.txt"))["content"] == "workspace"
        assert len(owners) == 2 and owners[0] is owners[1] and owners[0].is_alive()
        await _assert_survives_request_thread(started[1])
    finally:
        environment.close()
