"""The host's workspace helper: start, generations, leases and cleanup.

Each test runs the real proxy, process and HTTP/2 code against the fake
helper in ``tests/_helpers/fake_workspace_helper.py``, a separate process on
a socket pair as the real helper is. The fake's event log shows what the
helper saw; ``start <pid>`` lines identify each helper process. Route parity
through the helper lives in ``test_workspace_service.py``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from pyqwest import Client
from pyqwest.testing import ASGITransport

from omnigent.api.workspace.v1alpha1.workspace_connect import (
    WorkspaceServiceASGIApplication,
    WorkspaceServiceClient,
)
from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    GetCapabilitiesRequest,
    ListDirectoryRequest,
    SearchFilesRequest,
    SearchTextRequest,
    StatPathRequest,
)
from omnigent.host.workspace_helper import process as helper_process
from omnigent.host.workspace_helper import proxy as helper_proxy
from omnigent.host.workspace_helper.process import (
    HelperUnavailable,
    find_helper_binary,
    start_helper,
)
from omnigent.host.workspace_helper.proxy import WorkspaceHelperProxy
from omnigent.host.workspace_service import WorkspaceService
from omnigent.workspace_fs import WorkspaceReader
from tests._helpers.fake_workspace_helper import relax_helper_deadlines, write_fake_helper
from tests._helpers.polling import poll_until_async

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.name != "posix", reason="the helper inherits a POSIX socket fd"),
]


# How long the fake may take to start and answer first on a loaded machine.
_FAKE_START_S = 60


class _Events:
    """The fake helper's event log."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def lines(self) -> list[str]:
        return self.path.read_text().splitlines() if self.path.exists() else []

    def pids(self) -> list[int]:
        return [int(line.split()[1]) for line in self.lines() if line.startswith("start ")]

    def matching(self, prefix: str) -> list[str]:
        return [line for line in self.lines() if line.startswith(prefix)]


@pytest.fixture(autouse=True)
def _slow_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    relax_helper_deadlines(monkeypatch)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.subprocess.Process]:
    """Every helper process started, even ones that never got to log."""
    processes: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def record(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(helper_process.asyncio, "create_subprocess_exec", record)
    return processes


def _reaped(processes: list[asyncio.subprocess.Process]) -> bool:
    return bool(processes) and all(
        process.returncode is not None and not _alive(process.pid) for process in processes
    )


@pytest.fixture
def events(tmp_path: Path) -> _Events:
    return _Events(tmp_path / "events.log")


@pytest.fixture
def fake_helper(tmp_path: Path, events: _Events) -> Callable[..., str]:
    """Write a fake helper binary with the given switches, logging to ``events``."""

    def write(**env: str) -> str:
        directory = tmp_path / f"helper-{len(list(tmp_path.glob('helper-*')))}"
        directory.mkdir()
        return str(write_fake_helper(directory, FAKE_HELPER_EVENTS=str(events.path), **env))

    return write


@pytest.fixture
async def proxies() -> AsyncIterator[list[WorkspaceHelperProxy]]:
    """Proxies a test creates; each is closed afterwards even if the test fails."""
    created: list[WorkspaceHelperProxy] = []
    yield created
    for proxy in created:
        await proxy.close()


def _roots(tmp_path: Path, count: int) -> list[Path]:
    roots = []
    for index in range(count):
        root = tmp_path / f"root{index}"
        root.mkdir()
        (root / "a.txt").write_text("a")
        roots.append(root)
    return roots


def _alive(pid: int) -> bool:
    """Whether ``pid`` exists, zombies included: only a reaped child is gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def _helper_tasks() -> list[asyncio.Task[object]]:
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("workspace-helper")]


async def _answering_root(proxy: WorkspaceHelperProxy, root: Path) -> str:
    """Which workspace the helper reads for ``root``: the fake answers its name."""
    async with proxy.call(root) as call:
        response = await call.client.stat_path(StatPathRequest(path="."), headers=call.headers)
    return response.entry.name


def _service(proxy: WorkspaceHelperProxy | None, root: Path) -> WorkspaceServiceClient:
    reader = WorkspaceReader(root)
    app = WorkspaceServiceASGIApplication(WorkspaceService(lambda _ctx: reader, proxy))
    return WorkspaceServiceClient("http://workspace.test", http_client=Client(ASGITransport(app)))


async def test_find_helper_binary_honors_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_WORKSPACE_HELPER", "off")
    assert find_helper_binary() is None
    monkeypatch.setenv("OMNIGENT_WORKSPACE_HELPER", "/opt/example/workspace-helper")
    assert find_helper_binary() == "/opt/example/workspace-helper"


async def test_calls_name_their_own_workspace(
    tmp_path: Path, fake_helper: Callable[..., str], proxies: list[WorkspaceHelperProxy]
) -> None:
    """The header pins each call to the workspace of its own root."""
    first, second = _roots(tmp_path, 2)
    proxy = WorkspaceHelperProxy(fake_helper())
    proxies.append(proxy)

    answers = await asyncio.gather(*(_answering_root(proxy, root) for root in (first, second)))

    assert answers == ["root0", "root1"]


async def test_restart_does_not_reuse_workspace_ids(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    """A new helper numbers workspaces afresh; old ids must not reach it."""
    first, second = _roots(tmp_path, 2)
    proxy = WorkspaceHelperProxy(fake_helper())
    proxies.append(proxy)
    assert await _answering_root(proxy, first) == "root0"  # w1 in the first helper
    assert await _answering_root(proxy, second) == "root1"  # w2
    os.kill(events.pids()[0], signal.SIGKILL)
    await poll_until_async(lambda: not _alive(events.pids()[0]), timeout_s=_FAKE_START_S)

    # Reverse order: the new helper names root1 w1 and root0 w2.
    assert await _answering_root(proxy, second) == "root1"
    assert await _answering_root(proxy, first) == "root0"
    assert len(events.pids()) == 2
    assert events.matching("open w1")[-1].endswith("root1")


async def test_open_completing_after_restart_is_discarded(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    """An open that answers after its helper was retired fails its call instead of being used."""
    (root,) = _roots(tmp_path, 1)
    proxy = WorkspaceHelperProxy(fake_helper(FAKE_HELPER_OPEN_DELAY="0.3"))
    proxies.append(proxy)
    calling = asyncio.ensure_future(_answering_root(proxy, root))
    await poll_until_async(lambda: bool(events.matching("opening")), timeout_s=_FAKE_START_S)
    generation = proxy._generation
    assert generation is not None

    proxy._retire(generation, "test")

    with pytest.raises(HelperUnavailable):
        await calling
    assert await _answering_root(proxy, root) == "root0"
    assert len(events.pids()) == 2


async def test_capabilities_refresh_after_restart(
    fake_helper: Callable[..., str], events: _Events, proxies: list[WorkspaceHelperProxy]
) -> None:
    binaries = iter(
        [
            fake_helper(FAKE_HELPER_CAPABILITIES="ListDirectory"),
            fake_helper(FAKE_HELPER_CAPABILITIES="ListDirectory,SearchText"),
        ]
    )
    proxy = WorkspaceHelperProxy("unused", start=lambda _binary: start_helper(next(binaries)))
    proxies.append(proxy)
    assert await proxy.capabilities() == {"ListDirectory"}
    os.kill(events.pids()[0], signal.SIGKILL)
    await poll_until_async(lambda: not _alive(events.pids()[0]), timeout_s=_FAKE_START_S)

    assert await proxy.capabilities() == {"ListDirectory", "SearchText"}


async def test_more_than_sixteen_roots_keep_active_streams(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    """Opening past the limit closes idle workspaces, never one a stream still reads."""
    roots = _roots(tmp_path, helper_proxy.MAX_OPEN_WORKSPACES + 4)
    for name in ("b.txt", "c.txt", "d.txt"):
        (roots[0] / name).write_text(name)
    gate = tmp_path / "finish-search"
    proxy = WorkspaceHelperProxy(fake_helper(FAKE_HELPER_TEXT_GATE=str(gate)))
    proxies.append(proxy)
    streamed: list[str] = []

    async def search() -> None:
        async with proxy.call(roots[0]) as call:
            request = SearchTextRequest(query="q")
            async for message in call.client.search_text(request, headers=call.headers):
                streamed.extend(match.path for match in message.matches)

    searching = asyncio.ensure_future(search())
    await poll_until_async(lambda: streamed == ["a.txt"], timeout_s=_FAKE_START_S)
    for root in roots[1:]:
        assert await _answering_root(proxy, root) == root.name
    excess = len(roots) - helper_proxy.MAX_OPEN_WORKSPACES
    await poll_until_async(
        lambda: len(events.matching("close")) == excess, timeout_s=_FAKE_START_S
    )

    assert not searching.done()
    assert "close w1" not in events.lines()
    gate.touch()
    await searching
    assert streamed == ["a.txt", "b.txt", "c.txt", "d.txt"]


async def test_concurrent_opens_of_one_root_share_one_open(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    (root,) = _roots(tmp_path, 1)
    proxy = WorkspaceHelperProxy(fake_helper(FAKE_HELPER_OPEN_DELAY="0.2"))
    proxies.append(proxy)

    answers = await asyncio.gather(*(_answering_root(proxy, root) for _ in range(5)))

    assert answers == ["root0"] * 5
    assert len(events.matching("open ")) == 1


async def test_lease_released_on_error_and_cancellation(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    """A failed or cancelled call gives its workspace back, so it can be closed."""
    roots = _roots(tmp_path, helper_proxy.MAX_OPEN_WORKSPACES + 1)
    proxy = WorkspaceHelperProxy(fake_helper(FAKE_HELPER_TEXT_DELAY="5"))
    proxies.append(proxy)
    with pytest.raises(RuntimeError):
        async with proxy.call(roots[0]):
            raise RuntimeError("the call failed")

    async def search() -> None:
        async with proxy.call(roots[0]) as call:
            request = SearchTextRequest(query="q")
            async for _message in call.client.search_text(request, headers=call.headers):
                pass

    searching = asyncio.ensure_future(search())
    await asyncio.sleep(0.2)
    searching.cancel()
    # connectrpc reports a cancelled call as CANCELED.
    with pytest.raises((asyncio.CancelledError, ConnectError)):
        await searching
    for root in roots[1:]:
        await _answering_root(proxy, root)

    await poll_until_async(lambda: bool(events.matching("close w1")), timeout_s=_FAKE_START_S)


async def test_hung_close_workspace_still_terminates(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    """A helper that never answers CloseWorkspace and ignores TERM still stops, bounded."""
    (root,) = _roots(tmp_path, 1)
    proxy = WorkspaceHelperProxy(
        fake_helper(FAKE_HELPER_HANG_CLOSE="1", FAKE_HELPER_IGNORE_TERM="1")
    )
    proxies.append(proxy)
    await _answering_root(proxy, root)
    started = time.monotonic()

    await proxy.close()

    assert not _alive(events.pids()[0])
    bound = (
        helper_process._CLOSE_WORKSPACES_TIMEOUT_S
        + helper_process._TRANSPORT_CLOSE_TIMEOUT_S
        + helper_process._TERMINATE_TIMEOUT_S
        + helper_process._KILL_TIMEOUT_S
    )
    assert time.monotonic() - started < bound + 1


async def test_cancelled_startup_leaves_nothing_behind(
    fake_helper: Callable[..., str], spawned: list[asyncio.subprocess.Process]
) -> None:
    starting = asyncio.ensure_future(start_helper(fake_helper(FAKE_HELPER_READY="never")))
    await poll_until_async(lambda: bool(spawned), timeout_s=_FAKE_START_S)

    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert _reaped(spawned)


async def test_shutdown_during_startup_leaves_nothing_behind(
    fake_helper: Callable[..., str], spawned: list[asyncio.subprocess.Process]
) -> None:
    proxy = WorkspaceHelperProxy(fake_helper(FAKE_HELPER_READY="never"))
    asking = asyncio.ensure_future(proxy.capabilities())
    await poll_until_async(lambda: bool(spawned), timeout_s=_FAKE_START_S)

    await proxy.close()

    with pytest.raises(asyncio.CancelledError):
        await asking
    assert _reaped(spawned)
    assert not proxy.enabled


async def test_readiness_timeout_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    fake_helper: Callable[..., str],
    spawned: list[asyncio.subprocess.Process],
) -> None:
    monkeypatch.setattr(helper_process, "_READY_TIMEOUT_S", 0.5)

    with pytest.raises(HelperUnavailable, match="ready in time"):
        await start_helper(fake_helper(FAKE_HELPER_READY="never"))

    assert _reaped(spawned)


async def test_repeated_start_failures_turn_the_helper_off(
    monkeypatch: pytest.MonkeyPatch,
    fake_helper: Callable[..., str],
    spawned: list[asyncio.subprocess.Process],
    proxies: list[WorkspaceHelperProxy],
) -> None:
    """After three failed starts in a row the host stops trying, and serves from Python."""
    monkeypatch.setattr(helper_process, "_READY_TIMEOUT_S", 0.5)
    proxy = WorkspaceHelperProxy(fake_helper(FAKE_HELPER_READY="never"))
    proxies.append(proxy)

    for _ in range(helper_proxy.MAX_START_FAILURES + 1):
        assert await proxy.capabilities() == frozenset()

    assert len(spawned) == helper_proxy.MAX_START_FAILURES
    assert _reaped(spawned)
    assert not proxy.enabled


async def test_another_protocol_turns_the_helper_off(
    fake_helper: Callable[..., str],
    spawned: list[asyncio.subprocess.Process],
    proxies: list[WorkspaceHelperProxy],
) -> None:
    proxy = WorkspaceHelperProxy(fake_helper(FAKE_HELPER_PROTOCOL="2"))
    proxies.append(proxy)

    assert await proxy.capabilities() == frozenset()

    assert not proxy.enabled
    assert _reaped(spawned)


async def test_crash_mid_stream_cleans_up_and_respawns(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    (root,) = _roots(tmp_path, 1)
    proxy = WorkspaceHelperProxy(fake_helper())
    proxies.append(proxy)
    client = _service(proxy, root)

    with pytest.raises(ConnectError) as crashed:
        [message async for message in client.search_text(SearchTextRequest(query="crash"))]

    assert crashed.value.code == Code.UNAVAILABLE
    await poll_until_async(lambda: not _alive(events.pids()[0]), timeout_s=_FAKE_START_S)
    answers = [m async for m in client.search_text(SearchTextRequest(query="q"))]
    assert [match.path for message in answers for match in message.matches] == ["a.txt"]
    assert len(events.pids()) == 2


async def test_idle_stop_cleans_up(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    (root,) = _roots(tmp_path, 1)
    proxy = WorkspaceHelperProxy(fake_helper(), idle_stop_s=0.3)
    proxies.append(proxy)
    await _answering_root(proxy, root)

    await poll_until_async(lambda: not _alive(events.pids()[0]), timeout_s=_FAKE_START_S)

    assert events.matching("close w1")
    assert await _answering_root(proxy, root) == "root0"
    assert len(events.pids()) == 2


async def test_daemon_shutdown_cleans_up(
    tmp_path: Path, fake_helper: Callable[..., str], events: _Events
) -> None:
    """Shutdown leaves no helper process, file descriptor or task behind."""
    roots = _roots(tmp_path, 3)
    fds_before = _open_fds()
    proxy = WorkspaceHelperProxy(fake_helper())
    for root in roots:
        await _answering_root(proxy, root)

    await proxy.close()

    assert not _alive(events.pids()[0])
    assert len(events.matching("close")) == len(roots)
    assert _helper_tasks() == []
    assert _open_fds() == fds_before
    assert not proxy.enabled


async def test_capabilities_list_only_what_clients_may_use(
    tmp_path: Path, fake_helper: Callable[..., str], proxies: list[WorkspaceHelperProxy]
) -> None:
    """The helper's private names stay private, and unserved methods are UNIMPLEMENTED."""
    (root,) = _roots(tmp_path, 1)
    proxy = WorkspaceHelperProxy(fake_helper())
    proxies.append(proxy)
    client = _service(proxy, root)

    response = await client.get_capabilities(GetCapabilitiesRequest())

    assert set(response.capabilities) == {
        "ListDirectory",
        "ReadFile",
        "StatPath",
        "SearchText",
        "gitignore",
    }
    with pytest.raises(ConnectError) as unserved:
        await client.search_files(SearchFilesRequest(query="a"))
    assert unserved.value.code == Code.UNIMPLEMENTED


async def test_refused_caller_starts_no_helper(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
) -> None:
    proxy = WorkspaceHelperProxy(fake_helper())
    proxies.append(proxy)

    def refuse(_ctx: object) -> WorkspaceReader:
        raise ConnectError(Code.PERMISSION_DENIED, "not yours")

    app = WorkspaceServiceASGIApplication(WorkspaceService(refuse, proxy))
    client = WorkspaceServiceClient(
        "http://workspace.test", http_client=Client(ASGITransport(app))
    )

    with pytest.raises(ConnectError) as refused:
        await client.get_capabilities(GetCapabilitiesRequest())
    with pytest.raises(ConnectError):
        await client.stat_path(StatPathRequest(path="."))

    assert refused.value.code == Code.PERMISSION_DENIED
    assert events.pids() == []


@pytest.mark.parametrize("binary", [None, "/nonexistent/workspace-helper"])
async def test_baseline_methods_fall_back_to_the_python_reader(
    tmp_path: Path, binary: str | None, proxies: list[WorkspaceHelperProxy]
) -> None:
    """Without a helper that starts, ListDirectory still answers, and helper methods don't."""
    (root,) = _roots(tmp_path, 1)
    proxy = WorkspaceHelperProxy(binary)
    proxies.append(proxy)
    client = _service(proxy, root)

    listing = await client.list_directory(ListDirectoryRequest())

    assert [entry.name for entry in listing.entries] == ["a.txt"]
    capabilities = await client.get_capabilities(GetCapabilitiesRequest())
    assert set(capabilities.capabilities) == {"ListDirectory", "ReadFile"}
    with pytest.raises(ConnectError) as unserved:
        await client.stat_path(StatPathRequest(path="."))
    assert unserved.value.code == Code.UNIMPLEMENTED


async def test_listing_falls_back_when_the_helper_dies(
    tmp_path: Path,
    fake_helper: Callable[..., str],
    events: _Events,
    proxies: list[WorkspaceHelperProxy],
    caplog: pytest.LogCaptureFixture,
) -> None:
    (root,) = _roots(tmp_path, 1)
    proxy = WorkspaceHelperProxy(fake_helper(FAKE_HELPER_OPEN_DELAY="5"))
    proxies.append(proxy)
    client = _service(proxy, root)
    listing = asyncio.ensure_future(client.list_directory(ListDirectoryRequest()))
    await poll_until_async(lambda: bool(events.matching("opening")), timeout_s=_FAKE_START_S)

    os.kill(events.pids()[0], signal.SIGKILL)

    assert [entry.name for entry in (await listing).entries] == ["a.txt"]
    assert "couldn't answer ListDirectory, so the host's reader did" in caplog.text
