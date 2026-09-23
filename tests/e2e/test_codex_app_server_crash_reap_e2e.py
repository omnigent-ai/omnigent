"""Reconcile native Codex session and model-probe processes after owner-lock release.

Use the real npm Codex shebang launcher to verify that registry tags survive
exec and identify the process tree. Releasing the owner lock simulates host
death; the test does not crash a live host or make model calls."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import process_registry as registry
from omnigent.harnesses.codex_native.app_server import (
    CodexNativeAppServer,
    _allocate_loopback_port,
    _start_codex_model_discovery_process,
    _wait_for_discovery_listener,
)
from omnigent.harnesses.codex_native.process_registry import (
    codex_native_process_registry_path,
    codex_native_session_tag_cmdline_arg,
    reconcile_codex_native_process_registry,
)

# The crash-safe registry relies on flock + /proc-or-ps + os.killpg semantics,
# which are POSIX-only; the shebang-wrapper argv[0] rewrite is likewise a POSIX
# exec detail. The reported OSes are macOS and Linux.
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="codex crash reaping relies on POSIX flock/killpg + shebang exec semantics",
)


def _codex_cli() -> str:
    """Locate the ``codex`` CLI or skip.

    :returns: Absolute path to the ``codex`` launcher on PATH.
    """
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("native Codex crash-reap test requires the 'codex' CLI on PATH")
    return codex


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* is still alive.

    :param pid: Process id to probe.
    :returns: ``True`` while the process exists.
    """
    return registry._pid_alive(pid)


def _proc_state(pid: int) -> str:
    """Return the single-letter scheduler state of *pid*, or ``""`` if gone.

    ``Z`` marks a zombie: the process has exited and only awaits a parent
    ``wait()``. A crash orphan is reparented to init, which reaps zombies
    immediately, so a zombie here means "already reaped" for our purposes.

    :param pid: Process id to inspect.
    :returns: State char (``R``/``S``/``Z``/...), or ``""`` when the pid is gone.
    """
    if not Path("/proc").is_dir():
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()[:1]
    try:
        after_comm = (Path("/proc") / str(pid) / "stat").read_text().split(")", 1)[1]
    except (OSError, IndexError):
        return ""
    fields = after_comm.split()
    return fields[0] if fields else ""


def _tagged_pids(tag: str) -> set[int]:
    """Return every PID whose command line still carries *tag*.

    Identifies exactly the ``node`` shim and its Rust ``codex app-server`` child
    spawned by one launch, so the reap check is scoped to our own processes and
    never trips over unrelated codex instances.

    :param tag: The crash-reap session tag.
    :returns: Set of live pids carrying the tag marker.
    """
    marker = codex_native_session_tag_cmdline_arg(tag)
    found: set[int] = set()
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and marker in fields[1]:
                found.add(int(fields[0]))
        return found
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if marker in registry._process_cmdline(pid):
            found.add(pid)
    return found


def _reap_zombie_children() -> None:
    """Reap any exited direct children, the way init reaps a crash orphan.

    :returns: None.
    """
    with contextlib.suppress(ChildProcessError):
        while True:
            reaped_pid, _ = os.waitpid(-1, os.WNOHANG)
            if reaped_pid == 0:
                return


async def _wait_group_terminated(tag: str, timeout: float = 8.0) -> set[int]:
    """Wait for tagged processes to exit, allowing child watchers to reap zombies.

    Returns any tagged non-zombie PIDs still alive at the deadline."""
    deadline = time.monotonic() + timeout
    while True:
        still_live = {pid for pid in _tagged_pids(tag) if _proc_state(pid) not in ("", "Z")}
        if not still_live or time.monotonic() >= deadline:
            return still_live
        await asyncio.sleep(0.1)


def _kill_group(pid: int) -> None:
    """Best-effort SIGKILL of *pid*'s whole process group (test cleanup).

    :param pid: Any pid in the group to reap.
    :returns: None.
    """
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


@pytest.fixture()
def _hermetic_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the codex-native state root (registry + owner locks) to tmp.

    ``OMNIGENT_CODEX_NATIVE_STATE_DIR`` is honoured by
    ``_codex_native_state_root``, which backs both
    ``codex_native_process_registry_path`` and the owner-lock directory, so the
    product's register/reconcile calls and this test share one throwaway root.

    :returns: The temporary state root path.
    """
    root = tmp_path / "codex-native-state"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(root))
    return root


async def test_model_probe_app_server_registered_for_crash_reaping(
    _hermetic_state_root: Path,
) -> None:
    """Facet 1: the model-probe app-server must be crash-reapable.

    Drives the real ``_start_codex_model_discovery_process`` (the probe spawn)
    and asserts the spawned ``codex app-server`` is registered in the crash-safe
    process registry, so a reconcile after a Host death can find and reap it.
    Before the fix the probe is never registered, so the registry has no entry
    for it and this assertion fails.
    """
    codex = _codex_cli()
    codex_home = _hermetic_state_root / "probe-home"
    codex_home.mkdir(mode=0o700)
    port = _allocate_loopback_port()
    listen_url = f"ws://127.0.0.1:{port}"
    env = {**os.environ, "CODEX_HOME": str(codex_home)}

    discovery = await _start_codex_model_discovery_process(
        codex_path=codex,
        listen_url=listen_url,
        env=env,
        cwd=codex_home,
    )
    pid = discovery.process.pid
    try:
        await _wait_for_discovery_listener(discovery, port)
        assert discovery.process.returncode is None

        registry_path = codex_native_process_registry_path()
        raw = registry_path.read_text(encoding="utf-8") if registry_path.exists() else ""
        assert f'"pid":{pid}' in raw or f'"pid": {pid}' in raw, (
            "model-probe codex app-server was not registered in the crash-safe "
            f"registry (pid={pid}); a Host that dies mid-probe would orphan it "
            f"forever. Registry contents: {raw!r}"
        )
    finally:
        _kill_group(pid)
        discovery.stderr_tail.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await discovery.stderr_tail


async def test_session_app_server_reaped_after_host_crash(
    _hermetic_state_root: Path,
) -> None:
    """Verify the real launcher preserves its tag, then release ownership and reap its tree."""
    codex = _codex_cli()
    root = _hermetic_state_root
    codex_home = root / "session-home"
    codex_home.mkdir(mode=0o700)
    bridge_dir = root / "bridge"
    bridge_dir.mkdir(mode=0o700)
    port = _allocate_loopback_port()

    server = CodexNativeAppServer(
        codex_path=codex,
        socket_path=root / "app.sock",
        codex_home=codex_home,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
        config_overrides=[],
        cwd=root,
        bridge_dir=bridge_dir,
        listen_url=f"ws://127.0.0.1:{port}",
        python_executable=sys.executable,
    )
    await server.start()
    assert server.proc is not None
    pid = server.proc.pid
    tag = server.process_registry_tag
    assert tag is not None

    reaped = False
    try:
        # The launcher registered a crash-safe entry for the running app-server.
        registry_path = codex_native_process_registry_path()
        raw = registry_path.read_text(encoding="utf-8")
        assert f'"pid":{pid}' in raw or f'"pid": {pid}' in raw, (
            f"session codex app-server (pid={pid}) was not registered. Registry: {raw!r}"
        )

        # The reap tag must be findable on the REAL running process. Through the
        # npm codex shebang wrapper the kernel rewrites argv[0], so an argv[0]
        # tag is lost here.
        running_cmdline = registry._process_cmdline(pid)
        assert registry._process_cmdline_has_tag(pid, tag), (
            "reap tag is not present in the running codex app-server command "
            "line, so reconcile can never prove the PID is ours and will drop "
            f"the entry without reaping it. tag={tag!r}; "
            f"running cmdline={running_cmdline!r}"
        )

        # The launch really is running: the node shim and its Rust app-server
        # child both carry the tag. This is what a reconcile must reap.
        live_before = _tagged_pids(tag)
        assert live_before, "expected the tagged codex app-server tree to be running"

        # Simulate the Host dying without graceful teardown: the OS releases the
        # launcher's owner flock. A crashing parent does NOT kill its children,
        # so the whole codex app-server tree keeps running as an orphan — this
        # is exactly the state a fresh Host must reap.
        assert server.process_owner_lock is not None
        server.process_owner_lock.close()
        server.process_owner_lock = None

        # A fresh Host reconciles the registry at boot.
        reconcile_codex_native_process_registry()

        # Init reaps the orphan's zombies; model that and confirm no live
        # (non-zombie) codex app-server from this launch survives.
        survivors = await _wait_group_terminated(tag)
        assert not survivors, (
            "orphaned codex app-server tree survived reconcile after the owning "
            "Host died — it should have been reaped by process group. Still "
            "alive (pid -> cmdline): " + repr({p: registry._process_cmdline(p) for p in survivors})
        )
        reaped = True
    finally:
        if not reaped:
            _kill_group(pid)
        _reap_zombie_children()
        with contextlib.suppress(Exception):
            await server.close()
