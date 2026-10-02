"""E2E: crash-safe reaping of native Codex ``app-server`` children.

The Host records every ``codex app-server`` it spawns in a crash-safe registry so
that, when a Host dies without tearing its children down, the next Host reaps the
orphans it left behind (``reconcile_codex_native_process_registry``). Two gaps in
that guarantee are covered here against the real ``codex`` CLI:

* The *model-probe* app-server started for model discovery
  (``_start_codex_model_discovery_process``) must be registered as well, or a
  Host that dies mid-probe strands a ``node ... codex app-server`` tree that no
  later reconcile can see.
* A stock npm/Homebrew ``codex`` is a ``#!/usr/bin/env node`` shim, so the kernel
  rewrites ``argv[0]`` and a reap tag carried there never reaches the process
  command line. The tag must travel as a real argument so reconciliation can
  still prove a PID is ours.

Releasing the owner lock stands in for the Host dying; no live Host is crashed
and no model calls are made. Run with::

    OMNIGENT_E2E_CODEX_NATIVE=1 \\
        .venv/bin/python -m pytest tests/e2e/test_codex_app_server_crash_reap_e2e.py -v
"""

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
    _stop_codex_model_discovery_process,
    _wait_for_discovery_listener,
)
from omnigent.harnesses.codex_native.process_registry import (
    CodexNativeProcessEntry,
    codex_native_process_registry_path,
    codex_native_session_tag_cmdline_arg,
    reconcile_codex_native_process_registry,
)
from omnigent.inner.codex_executor import _clean_codex_env

# flock, /proc-or-ps and killpg semantics and the shebang argv[0] rewrite are
# POSIX-only; the reported platforms are macOS and Linux.
pytestmark = pytest.mark.skipif(
    os.name != "posix"
    or os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1"
    or shutil.which("codex") is None,
    reason="codex crash-reap e2e needs POSIX, `codex` on PATH and OMNIGENT_E2E_CODEX_NATIVE=1",
)


def _codex_cli() -> str:
    codex = shutil.which("codex")
    assert codex is not None
    return codex


def _registry_entry(pid: int) -> CodexNativeProcessEntry | None:
    for entry in registry._read_registry(codex_native_process_registry_path()):
        if entry.pid == pid:
            return entry
    return None


def _proc_state(pid: int) -> str:
    """Return the scheduler state letter of *pid* (``Z`` = zombie), or ``""`` if gone."""
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
    """Return every PID whose command line carries *tag*: the node shim and its Rust child."""
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


async def _wait_for_exit(process: asyncio.subprocess.Process, timeout: float = 10.0) -> bool:
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except TimeoutError:
        return False
    return True


async def _wait_group_terminated(tag: str, timeout: float = 8.0) -> set[int]:
    """Return the tagged PIDs still running (neither exited nor zombie) at the deadline."""
    deadline = time.monotonic() + timeout
    while True:
        still_live = {pid for pid in _tagged_pids(tag) if _proc_state(pid) not in ("", "Z")}
        if not still_live or time.monotonic() >= deadline:
            return still_live
        await asyncio.sleep(0.1)


def _kill_group(pid: int) -> None:
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


@pytest.fixture()
def _hermetic_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the registry and owner-lock directory at a throwaway state root."""
    root = tmp_path / "codex-native-state"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(root))
    return root


async def test_model_probe_app_server_reaped_after_host_crash(
    _hermetic_state_root: Path,
) -> None:
    """A model-probe app-server stranded by a dead Host is reaped by the next reconcile.

    Drives the real probe spawn, drops the owner lock the way a Host crash does,
    then runs the reconcile a fresh Host performs at boot. Unfixed, the probe is
    never registered, so reconciliation cannot see it and the orphan survives.
    """
    codex_home = _hermetic_state_root / "probe-home"
    codex_home.mkdir(mode=0o700)
    port = _allocate_loopback_port()
    env = _clean_codex_env()
    env["CODEX_HOME"] = str(codex_home)

    discovery = await _start_codex_model_discovery_process(
        codex_path=_codex_cli(),
        listen_url=f"ws://127.0.0.1:{port}",
        env=env,
        cwd=codex_home,
    )
    pid = discovery.process.pid
    try:
        await _wait_for_discovery_listener(discovery, port)
        assert discovery.process.returncode is None

        # The owner flock dies with a crashed Host. Here it is held by this very
        # process, so drop the lock file; reconcile treats a missing file as released.
        entry = _registry_entry(pid)
        if entry is not None and entry.owner_lock_path:
            Path(entry.owner_lock_path).unlink(missing_ok=True)
        reconcile_codex_native_process_registry()

        assert await _wait_for_exit(discovery.process), (
            f"model-probe codex app-server (pid {pid}) survived reconciliation after "
            f"its Host died; crash-safe registry entry at spawn: {entry!r}"
        )
        assert _registry_entry(pid) is None
    finally:
        await _stop_codex_model_discovery_process(discovery)


async def test_session_app_server_reaped_after_host_crash(
    _hermetic_state_root: Path,
) -> None:
    """A session app-server keeps its reap tag through the node shim and is reaped."""
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
        entry = _registry_entry(pid)
        assert entry is not None, f"session codex app-server (pid {pid}) was not registered"
        assert entry.process_start_identity is not None, (
            "session app-server was registered without a process birth identity"
        )

        # Through the npm shim the kernel rewrites argv[0], so a tag carried there
        # is lost; the tag must be visible on the real running process.
        running_cmdline = registry._process_cmdline(pid)
        assert registry._process_cmdline_has_tag(pid, tag), (
            "reap tag is missing from the running codex app-server command line, so "
            f"reconcile cannot prove the PID is ours. tag={tag!r}; cmdline={running_cmdline!r}"
        )
        assert _tagged_pids(tag), "expected the tagged codex app-server tree to be running"

        # The Host dies without teardown: the OS releases its owner flock while the
        # app-server tree keeps running as an orphan for the next Host to reap.
        assert server.process_owner_lock is not None
        server.process_owner_lock.close()
        server.process_owner_lock = None

        reconcile_codex_native_process_registry()

        survivors = await _wait_group_terminated(tag)
        assert not survivors, (
            "orphaned codex app-server tree survived reconcile after its Host died; "
            "still alive (pid -> cmdline): "
            + repr({p: registry._process_cmdline(p) for p in survivors})
        )
        assert _registry_entry(pid) is None
        reaped = True
    finally:
        if not reaped:
            _kill_group(pid)
        with contextlib.suppress(Exception):
            await server.close()
