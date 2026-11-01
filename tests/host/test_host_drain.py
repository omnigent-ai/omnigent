"""Host-side launch fence (drain): receipt-time admission and the close-ack.

The fence must be lossless: a launch frame the server sent before
``host.set_drain`` is counted from the moment the receive loop reads it —
before it queues for the lifecycle lock or enters the shielded spawn thread —
so the host cannot acknowledge a drain while that spawn is still invisible.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    CAP_LAUNCH_DRAIN,
    HOST_CAPABILITIES,
    HOST_DRAINING_ERROR_CODE,
    HostDrainStatusFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostSetDrainFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.host.maintenance import HostMaintenanceJanitor
from omnigent.runner.identity import token_bound_runner_id

pytestmark = pytest.mark.asyncio

_WAIT_S = 10.0


@pytest.fixture(autouse=True)
def _no_real_zygote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn runners with plain ``Popen`` rather than forking a real zygote."""
    from omnigent.runner._zygote import ZYGOTE_ENABLED_ENV_VAR

    monkeypatch.setenv(ZYGOTE_ENABLED_ENV_VAR, "0")


@pytest.fixture(autouse=True)
def _no_real_host_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host loop from sweeping machine-global developer state."""

    class _NoOpJanitor:
        def start(self) -> None:
            return None

        def trigger(self, _reason: str) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    monkeypatch.setattr(HostMaintenanceJanitor, "for_host", lambda **_kwargs: _NoOpJanitor())


class _ScriptedTunnel:
    """Duck-typed tunnel: ``recv`` replays queued frames, ``send`` records."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []
        self.sent_event = asyncio.Event()

    async def send(self, data: str) -> None:
        self.sent.append(data)
        self.sent_event.set()

    async def recv(self) -> str:
        return await self.inbound.get()

    def push(self, frame: HostLaunchRunnerFrame | HostSetDrainFrame) -> None:
        self.inbound.put_nowait(encode_host_frame(frame))

    def decoded(self) -> list[object]:
        return [decode_host_frame(raw) for raw in self.sent]

    def drain_reports(self) -> list[HostDrainStatusFrame]:
        return [f for f in self.decoded() if isinstance(f, HostDrainStatusFrame)]

    def launch_results(self) -> list[HostLaunchRunnerResultFrame]:
        return [f for f in self.decoded() if isinstance(f, HostLaunchRunnerResultFrame)]

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async def _poll() -> None:
            while not predicate():
                self.sent_event.clear()
                await self.sent_event.wait()

        await asyncio.wait_for(_poll(), _WAIT_S)


def _make_host(monkeypatch: pytest.MonkeyPatch) -> HostProcess:
    host = HostProcess(
        identity=HostIdentity(host_id="host_test_drain", name="drain-laptop"),
        server_url="http://localhost:8000",
    )
    host._auth_token_factory = lambda: "host-bootstrap-bearer"
    host._auth_token_factory_resolved = True

    async def _discover() -> None:
        host._capabilities_initialized = True

    async def _idle(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(host, "_initialize_capabilities", _discover)
    monkeypatch.setattr(host, "_harness_readiness_loop", _idle)
    monkeypatch.setattr(host, "_prewarm_model_options", _idle)
    return host


class _BlockingSpawn:
    """Stand-in for ``_spawn_runner_proc`` that parks inside the spawn thread."""

    def __init__(self, tmp_path: Path) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.procs: list[subprocess.Popen[bytes]] = []
        self._tmp_path = tmp_path

    def __call__(
        self, _env: dict[str, str], _slug: str, _workspace: Path
    ) -> tuple[subprocess.Popen[bytes], Path]:
        proc = subprocess.Popen(
            ["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.procs.append(proc)
        self.entered.set()
        self.release.wait(_WAIT_S)
        log_path = self._tmp_path / f"runner-{len(self.procs)}.log"
        log_path.touch()
        return proc, log_path

    def cleanup(self) -> None:
        self.release.set()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


async def _stop(host: HostProcess, serve: asyncio.Task[None]) -> None:
    serve.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await serve
    await host._quiesce_frame_tasks()
    for task in list(host._drain_report_tasks):
        task.cancel()
    await host._drain_runner_stop_tasks()
    host._cleanup_runners()
    for task in list(host._watcher_tasks):
        task.cancel()


def _launch_frame(workspace: Path, token: str = "drain-token") -> HostLaunchRunnerFrame:
    return HostLaunchRunnerFrame(
        request_id=f"req-{token}",
        binding_token=token,
        workspace=str(workspace),
        session_id=f"conv-{token}",
    )


async def test_hello_advertises_launch_drain_capability() -> None:
    """A host that can be fenced says so; the server never assumes it."""
    assert CAP_LAUNCH_DRAIN in HOST_CAPABILITIES


async def test_launch_frame_after_drain_is_refused_at_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch that arrives behind ``host.set_drain`` never reaches spawn."""
    host = _make_host(monkeypatch)
    spawned: list[object] = []
    monkeypatch.setattr(host, "_spawn_runner_proc", lambda *a: spawned.append(a))
    tunnel = _ScriptedTunnel()
    serve = asyncio.create_task(host._serve_frames(tunnel))  # type: ignore[arg-type]
    try:
        tunnel.push(HostSetDrainFrame(generation=1, draining=True))
        tunnel.push(_launch_frame(tmp_path))
        await tunnel.wait_for(lambda: bool(tunnel.launch_results()))
        await tunnel.wait_for(lambda: bool(tunnel.drain_reports()))
    finally:
        await _stop(host, serve)

    (result,) = tunnel.launch_results()
    assert result.status == "failed"
    assert result.error_code == HOST_DRAINING_ERROR_CODE
    assert spawned == [], "a fenced launch must not spawn"
    report = tunnel.drain_reports()[0]
    assert report == HostDrainStatusFrame(
        generation=1, draining=True, pending_launches=0, running_runners=0, complete=True
    )
    assert isinstance(tunnel.decoded()[0], HostHelloFrame)


async def test_drain_ack_waits_for_shielded_spawn_delivered_before_drain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The race the fence closes: launch frame, then drain, spawn still in its thread.

    The runner is registered in ``_runners`` only after the spawn thread
    returns; before this fence a drain could read "no runners" and be declared
    safe while that spawn was invisible. The ack must name the drain generation
    and arrive only after the runner is registered.
    """
    host = _make_host(monkeypatch)
    spawn = _BlockingSpawn(tmp_path)
    monkeypatch.setattr(host, "_spawn_runner_proc", spawn)
    tunnel = _ScriptedTunnel()
    serve = asyncio.create_task(host._serve_frames(tunnel))  # type: ignore[arg-type]
    runner_id = token_bound_runner_id("drain-token")
    try:
        tunnel.push(_launch_frame(tmp_path))
        tunnel.push(HostSetDrainFrame(generation=7, draining=True))
        await tunnel.wait_for(lambda: bool(tunnel.drain_reports()))
        await asyncio.wait_for(asyncio.to_thread(spawn.entered.wait, _WAIT_S), _WAIT_S)

        first = tunnel.drain_reports()[0]
        assert first.generation == 7
        assert first.pending_launches == 1, "the in-flight spawn must be visible"
        assert first.complete is False
        assert runner_id not in host._runners
        # Still inside the shielded thread: re-asking must not produce an ack.
        tunnel.push(HostSetDrainFrame(generation=7, draining=True))
        await tunnel.wait_for(lambda: len(tunnel.drain_reports()) >= 2)
        assert not any(r.complete for r in tunnel.drain_reports())

        spawn.release.set()
        await tunnel.wait_for(lambda: any(r.complete for r in tunnel.drain_reports()))
        assert runner_id in host._runners, "ack sent before the spawned runner registered"
    finally:
        spawn.cleanup()
        await _stop(host, serve)

    (result,) = tunnel.launch_results()
    assert result.status == "launched", "a launch admitted before the drain is honoured"
    ack = next(r for r in tunnel.drain_reports() if r.complete)
    assert ack == HostDrainStatusFrame(
        generation=7, draining=True, pending_launches=0, running_runners=1, complete=True
    )
    frames = tunnel.decoded()
    assert frames.index(result) < frames.index(ack), "the result must reach the server first"


async def test_cancelled_spawn_keeps_drain_open_until_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A spawn abandoned by cancellation still counts until its teardown ends."""
    host = _make_host(monkeypatch)
    spawn = _BlockingSpawn(tmp_path)
    monkeypatch.setattr(host, "_spawn_runner_proc", spawn)
    tunnel = _ScriptedTunnel()
    host._ws = tunnel  # type: ignore[assignment]
    frame = _launch_frame(tmp_path, token="cancel-token")
    launch = asyncio.create_task(host._dispatch_host_frame(tunnel, frame))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(asyncio.to_thread(spawn.entered.wait, _WAIT_S), _WAIT_S)
        host._apply_set_drain(HostSetDrainFrame(generation=2, draining=True))
        launch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await launch
        assert host._abandoned_spawn_tasks, "abandoned spawn teardown was not tracked"
        assert host._drain_status().pending_launches == 1
        assert host._drain_status().complete is False

        spawn.release.set()
        await host._drain_runner_stop_tasks()
        await asyncio.wait_for(
            tunnel.wait_for(lambda: any(r.complete for r in tunnel.drain_reports())), _WAIT_S
        )
    finally:
        spawn.cleanup()
        await host._drain_runner_stop_tasks()
    assert host._drain_status().complete is True
    assert not host._runners


async def test_undrain_lifts_fence_and_stale_generation_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Undrain re-opens launches; a late frame from an older generation cannot re-fence."""
    host = _make_host(monkeypatch)
    host._apply_set_drain(HostSetDrainFrame(generation=3, draining=True))
    assert host._admit_launch_at_receipt() is None

    host._apply_set_drain(HostSetDrainFrame(generation=4, draining=False))
    token = host._admit_launch_at_receipt()
    assert token is not None
    host._settle_launch_admission(token)

    # An out-of-order drain for generation 3 must not re-raise the fence.
    host._apply_set_drain(HostSetDrainFrame(generation=3, draining=True))
    status = host._drain_status()
    assert (status.generation, status.draining, status.complete) == (4, False, False)


async def test_drain_state_survives_reconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fence belongs to the process, not the tunnel: a new connection keeps it."""
    host = _make_host(monkeypatch)
    first = _ScriptedTunnel()
    serve = asyncio.create_task(host._serve_frames(first))  # type: ignore[arg-type]
    try:
        first.push(HostSetDrainFrame(generation=5, draining=True))
        await first.wait_for(lambda: bool(first.drain_reports()))
    finally:
        await _stop(host, serve)

    second = _ScriptedTunnel()
    monkeypatch.setattr(host, "_spawn_runner_proc", lambda *a: pytest.fail("spawned"))
    serve = asyncio.create_task(host._serve_frames(second))  # type: ignore[arg-type]
    try:
        second.push(_launch_frame(tmp_path, token="after-reconnect"))
        await second.wait_for(lambda: bool(second.launch_results()))
    finally:
        await _stop(host, serve)
    assert second.launch_results()[0].error_code == HOST_DRAINING_ERROR_CODE
