"""Optional host extension loading, lifecycle, and child ownership."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.host.connect import HostProcess, run_host_process
from omnigent.host.extension import HostExtension, load_host_extension
from omnigent.host.frames import HostLaunchRunnerFrame
from omnigent.host.identity import HostIdentity
from omnigent.host.maintenance import HostMaintenanceJanitor
from omnigent.runner.identity import token_bound_runner_id


class ExampleExtension(HostExtension):
    def __init__(self, *, fail_start: bool = False, fail_stop: bool = False) -> None:
        self.pids: set[int] = set()
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.started = False
        self.stopped = False
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def owned_pids(self) -> set[int]:
        return set(self.pids)

    async def start(self) -> None:
        self.start_calls += 1
        self.started = True
        if self.fail_start:
            raise RuntimeError("synthetic startup failure")

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("synthetic shutdown failure")


def _host(extension: HostExtension) -> HostProcess:
    return HostProcess(
        identity=HostIdentity(host_id="host_extension_test", name="extension-test"),
        server_url="http://localhost:8000",
        interactive_shells=["bash"],
        host_extension=extension,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_prepare", [False, True])
async def test_runner_launch_preparation_precedes_spawn_and_cannot_block_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_prepare: bool
) -> None:
    calls: list[object] = []

    class PreparingExtension(ExampleExtension):
        def before_runner_spawn(self, **kwargs: object) -> None:
            calls.append(kwargs)
            if fail_prepare:
                raise RuntimeError("synthetic preparation failure")

    host = _host(PreparingExtension())
    proc = SimpleNamespace(pid=1234, poll=lambda: None)
    frame = HostLaunchRunnerFrame(
        request_id="request_synthetic",
        binding_token="binding_synthetic",
        workspace=str(tmp_path),
        session_id="conv_synthetic",
        harness="codex-native",
    )

    def spawn(_env: dict[str, str], _slug: str, _workspace: Path) -> object:
        calls.append("spawn")
        return proc, tmp_path / "runner.log"

    monkeypatch.setattr("omnigent.host.connect.harness_is_configured", lambda _harness: True)
    monkeypatch.setattr(host, "_current_auth_token", lambda **_kwargs: None)
    monkeypatch.setattr(host, "_spawn_runner_proc", spawn)
    monkeypatch.setattr(host, "_watch_runner", AsyncMock())
    monkeypatch.setattr(host, "_watch_runner_connect", AsyncMock())

    result = await host._handle_launch(frame)
    assert result.status == "launched"
    assert result.runner_id == token_bound_runner_id(frame.binding_token)
    assert calls == [
        {
            "session_id": "conv_synthetic",
            "harness": "codex-native",
            "workspace": tmp_path,
            "runner_id": result.runner_id,
            "server_url": "http://localhost:8000",
        },
        "spawn",
    ]


@pytest.fixture
def isolated_host_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the lifecycle with no real runner, maintenance, or server I/O."""
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE", "0")

    class NoOpJanitor:
        def start(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    monkeypatch.setattr(HostMaintenanceJanitor, "for_host", lambda **_kwargs: NoOpJanitor())
    monkeypatch.setattr(HostProcess, "_ensure_model_options_prewarm", lambda _self: None)
    monkeypatch.setattr(HostProcess, "_start_capability_discovery", lambda _self: None)
    monkeypatch.setattr(HostProcess, "_reap_orphans_once", lambda _self, _pids=None: 0)


async def _run_until_connect(host: HostProcess, monkeypatch: pytest.MonkeyPatch) -> None:
    connected = False

    async def connect() -> None:
        nonlocal connected
        connected = True
        raise KeyboardInterrupt

    monkeypatch.setattr(host, "_connect_and_serve", connect)
    await host.run()
    assert connected


def test_host_extension_requires_explicit_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_HOST_EXTENSION", raising=False)
    monkeypatch.setattr(
        "omnigent.host.extension.importlib.metadata.entry_points",
        lambda **_kwargs: pytest.fail("entry points should not be read without opt-in"),
    )
    assert load_host_extension() is None


def test_selected_installed_host_extension_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_EXTENSION", "example")
    monkeypatch.setattr(
        "omnigent.host.extension.importlib.metadata.entry_points",
        lambda **_kwargs: [SimpleNamespace(name="example", load=lambda: ExampleExtension)],
    )
    assert isinstance(load_host_extension(), ExampleExtension)


def test_run_host_process_loads_selected_extension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The selected entry point reaches host construction without launching a host."""
    extension = ExampleExtension()
    monkeypatch.setenv("OMNIGENT_HOST_EXTENSION", "example")
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE", "0")
    monkeypatch.setattr(
        "omnigent.host.extension.importlib.metadata.entry_points",
        lambda **_kwargs: [SimpleNamespace(name="example", load=lambda: lambda: extension)],
    )
    monkeypatch.setattr(
        "omnigent.host.connect.configure_process_logging",
        lambda *_args, **_kwargs: tmp_path / "host.log",
    )
    monkeypatch.setattr("omnigent.runtime.telemetry.init", lambda *_args: None)
    monkeypatch.setattr(
        "omnigent.host.connect.load_or_create_host_identity",
        lambda _path: HostIdentity(host_id="host_extension_test", name="extension-test"),
    )
    monkeypatch.setattr("omnigent.host.connect._runner_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr("omnigent.cli_diagnostics.current_cli_log_path", lambda: None)
    for name in ("configure_host_git", "configure_host_gh", "start_host_gh_refresh"):
        monkeypatch.setattr(f"omnigent.git_credential_github.{name}", lambda *_args: None)
    monkeypatch.setattr(
        "omnigent.host.databricks_credential.configure_host_databricks", lambda *_args: None
    )
    monkeypatch.setattr("omnigent.host.connect._generate_ucode_configs", lambda: None)
    observed: list[HostExtension | None] = []

    async def record_run(host: HostProcess) -> None:
        observed.append(host._host_extension)

    monkeypatch.setattr(HostProcess, "run", record_run)
    run_host_process("http://localhost:8000", config_path=tmp_path / "config.yaml")

    assert observed == [extension]


@pytest.mark.parametrize("count", [0, 2])
def test_selected_host_extension_requires_one_match(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_EXTENSION", "example")
    entry = SimpleNamespace(name="example", load=lambda: ExampleExtension)
    monkeypatch.setattr(
        "omnigent.host.extension.importlib.metadata.entry_points",
        lambda **_kwargs: [entry] * count,
    )
    assert load_host_extension() is None


@pytest.mark.parametrize("failure", ["load", "construct", "type"])
def test_selected_host_extension_rejects_invalid_entry_point(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_EXTENSION", "example")

    def load() -> object:
        if failure == "load":
            raise RuntimeError("synthetic load failure")
        if failure == "construct":

            def fail_construct() -> None:
                raise RuntimeError("synthetic construction failure")

            return fail_construct
        return lambda: object()

    monkeypatch.setattr(
        "omnigent.host.extension.importlib.metadata.entry_points",
        lambda **_kwargs: [SimpleNamespace(name="example", load=load)],
    )
    assert load_host_extension() is None


@pytest.mark.asyncio
async def test_host_connects_after_extension_start_failure(
    monkeypatch: pytest.MonkeyPatch, isolated_host_run: None
) -> None:
    extension = ExampleExtension(fail_start=True)
    host = _host(extension)

    await _run_until_connect(host, monkeypatch)

    assert extension.start_calls == 1
    assert extension.stop_calls == 1
    assert host._host_extension is None


@pytest.mark.asyncio
async def test_host_connects_after_extension_cancelled_start(
    monkeypatch: pytest.MonkeyPatch, isolated_host_run: None
) -> None:
    class CancelledStartExtension(ExampleExtension):
        async def start(self) -> None:
            self.start_calls += 1
            self.started = True
            raise asyncio.CancelledError

    extension = CancelledStartExtension()
    host = _host(extension)

    await _run_until_connect(host, monkeypatch)

    assert extension.start_calls == 1
    assert extension.stop_calls == 1
    assert host._host_extension is None


@pytest.mark.asyncio
async def test_cancelling_host_during_extension_start_still_stops_extension(
    monkeypatch: pytest.MonkeyPatch, isolated_host_run: None
) -> None:
    monkeypatch.setattr("omnigent.host.connect._HOST_EXTENSION_START_TIMEOUT_S", 30.0)
    start_entered = asyncio.Event()
    connected = False

    class WaitingStartExtension(ExampleExtension):
        async def start(self) -> None:
            self.start_calls += 1
            start_entered.set()
            await asyncio.Event().wait()

    async def connect() -> None:
        nonlocal connected
        connected = True

    extension = WaitingStartExtension()
    host = _host(extension)
    monkeypatch.setattr(host, "_connect_and_serve", connect)
    run_task = asyncio.create_task(host.run())
    try:
        await asyncio.wait_for(start_entered.wait(), timeout=1.0)
        run_task.cancel()
        await asyncio.wait_for(run_task, timeout=1.0)
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.wait_for(run_task, timeout=1.0)

    assert not connected
    assert extension.start_calls == 1
    assert extension.stop_calls == 1
    assert host._host_extension is None


@pytest.mark.asyncio
async def test_host_connects_within_start_budget_when_failed_start_cleanup_hangs(
    monkeypatch: pytest.MonkeyPatch, isolated_host_run: None
) -> None:
    release_stop = asyncio.Event()
    stop_started = asyncio.Event()
    connected = asyncio.Event()

    class SlowCleanupExtension(ExampleExtension):
        async def stop(self) -> None:
            self.stop_calls += 1
            stop_started.set()
            await release_stop.wait()
            self.stopped = True

    async def connect() -> None:
        connected.set()
        await asyncio.Event().wait()

    extension = SlowCleanupExtension(fail_start=True)
    host = _host(extension)
    monkeypatch.setattr(host, "_connect_and_serve", connect)
    run_task = asyncio.create_task(host.run())
    try:
        await asyncio.wait_for(connected.wait(), timeout=1.0)
        await asyncio.wait_for(stop_started.wait(), timeout=1.0)
        assert not extension.stopped
        assert extension.stop_calls == 1
        assert host._host_extension is extension
    finally:
        release_stop.set()
        run_task.cancel()
        await asyncio.wait_for(run_task, timeout=1.0)
    assert extension.stopped
    assert host._host_extension is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stop", [False, True])
async def test_host_shutdown_stops_extension_once(
    monkeypatch: pytest.MonkeyPatch, isolated_host_run: None, fail_stop: bool
) -> None:
    extension = ExampleExtension(fail_stop=fail_stop)
    host = _host(extension)

    await _run_until_connect(host, monkeypatch)

    assert extension.start_calls == 1
    assert extension.stop_calls == 1
    assert host._host_extension is None


@pytest.mark.asyncio
async def test_slow_extension_start_does_not_block_host_connection(
    monkeypatch: pytest.MonkeyPatch, isolated_host_run: None
) -> None:
    monkeypatch.setattr("omnigent.host.connect._HOST_EXTENSION_START_TIMEOUT_S", 0.01)
    started = asyncio.Event()

    class SlowStartExtension(ExampleExtension):
        async def start(self) -> None:
            self.start_calls += 1
            started.set()
            await asyncio.Event().wait()

    extension = SlowStartExtension()
    host = _host(extension)

    await asyncio.wait_for(_run_until_connect(host, monkeypatch), timeout=1.0)

    assert started.is_set()
    assert extension.stop_calls == 1
    assert host._host_extension is None


@pytest.mark.asyncio
async def test_timed_out_start_retains_child_ownership_until_callback_finishes(
    monkeypatch: pytest.MonkeyPatch, isolated_host_run: None
) -> None:
    monkeypatch.setattr("omnigent.host.connect._HOST_EXTENSION_START_TIMEOUT_S", 0.01)
    release_start = asyncio.Event()
    connected = asyncio.Event()

    class SlowStartExtension(ExampleExtension):
        async def start(self) -> None:
            self.start_calls += 1
            while not release_start.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await release_start.wait()

    async def connect() -> None:
        connected.set()
        await asyncio.Event().wait()

    extension = SlowStartExtension()
    owned_pid = 424242
    extension.pids.add(owned_pid)
    host = _host(extension)
    monkeypatch.setattr(host, "_connect_and_serve", connect)
    run_task = asyncio.create_task(host.run())
    start_task = None
    try:
        await asyncio.wait_for(connected.wait(), timeout=1.0)
        start_task = host._host_extension_start_task
        assert start_task is not None and not start_task.done()
        assert host._host_extension is extension
        assert owned_pid in host._tracked_runner_pids()

        run_task.cancel()
        await asyncio.wait_for(run_task, timeout=1.0)
        assert extension.stop_calls == 1
        assert host._host_extension is extension
        assert owned_pid in host._tracked_runner_pids()
    finally:
        release_start.set()
        if not run_task.done():
            run_task.cancel()
            await asyncio.wait_for(run_task, timeout=1.0)
        if start_task is not None:
            await asyncio.wait_for(start_task, timeout=1.0)
        await asyncio.sleep(0)
    assert host._host_extension is None
    assert owned_pid not in host._tracked_runner_pids()


@pytest.mark.asyncio
async def test_stop_timeout_releases_host_lock_even_if_cancellation_is_suppressed(
    monkeypatch: pytest.MonkeyPatch, isolated_host_run: None
) -> None:
    monkeypatch.setattr("omnigent.host.connect._HOST_EXTENSION_STOP_TIMEOUT_S", 0.01)
    release_stop = asyncio.Event()

    class SlowStopExtension(ExampleExtension):
        async def stop(self) -> None:
            self.stop_calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_stop.wait()

    class RecordingLock:
        target = "extension-test"

        def __init__(self) -> None:
            self.released = False

        def acquire(self) -> bool:
            return True

        def still_owner(self) -> bool:
            return True

        def release(self) -> None:
            self.released = True

    extension = SlowStopExtension()
    host = _host(extension)
    lock = RecordingLock()
    host._lifecycle_lock = lock  # type: ignore[assignment]
    try:
        await asyncio.wait_for(_run_until_connect(host, monkeypatch), timeout=1.0)
        assert lock.released
        assert extension.stop_calls == 1
        assert host._host_extension is extension
    finally:
        release_stop.set()
        stop_task = host._host_extension_stop_task
        if stop_task is not None:
            await asyncio.wait_for(stop_task, timeout=1.0)
        await asyncio.sleep(0)
    assert host._host_extension is None


@pytest.mark.posix_only
@pytest.mark.parametrize("ownership_raises", [False, True])
def test_extension_child_retains_its_exit_status(
    monkeypatch: pytest.MonkeyPatch, ownership_raises: bool
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE", "0")

    class BrokenOwnershipExtension(ExampleExtension):
        @property
        def owned_pids(self) -> set[int]:
            raise RuntimeError("synthetic ownership failure")

    extension = BrokenOwnershipExtension() if ownership_raises else ExampleExtension()
    host = _host(extension)
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(42)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.read() == b""
        extension.pids.add(process.pid)
        if not ownership_raises:
            assert process.pid in host._tracked_runner_pids()
        assert host._reap_orphans_once([process.pid]) == 0
        assert process.wait(timeout=5) == 42
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
