"""Codex-native MCP orphan e2e: shutdown and crash-leftover cleanup.

Codex starts each stdio MCP server in its **own POSIX process group**, so the
process-group kill Omnigent used for a ``codex app-server`` never reached
them, and once the app server was gone they were re-parented to init where no
walk from it could find them. Every one still carries the app server's
**session** id, because :class:`CodexNativeAppServer` spawns it with
``start_new_session``.

Both tests drive the production :class:`CodexNativeAppServer` end to end
(``start()``, a real thread, real stdio MCP children, the real
``omnigent serve-mcp`` child production injects) against the **real**
``codex app-server`` binary, then exercise a production cleanup path:

* ``close()`` on an app server that cannot act on ``SIGTERM`` (stopped with
  ``SIGSTOP``, the shape of a wedged Codex), so the close escalates to
  ``SIGKILL`` and Codex never reaps its own children. This is what the idle
  pane reaper and session archive run. The descendant census in
  :func:`omnigent.inner._proc.kill_tree` reaches these children while the
  app server is still alive to be walked; the session sweep after its exit
  is the backstop for children the census could not see.
* the crash-leftover janitor, :func:`reconcile_codex_native_process_registry`,
  after the app server died without cleanup and its owner is gone. Nothing
  can be walked here: only the session id still finds the children.

The stub MCP servers answer the MCP handshake, keep running after their stdin
closes and ignore ``SIGTERM``, the behaviour of the wrappers that piled up on
the reporting host. A wrapper alive after cleanup is therefore a genuine
orphan, not one mid-exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
)
from omnigent.harnesses.codex_native.bridge import write_mcp_bridge_config
from omnigent.harnesses.codex_native.process_registry import (
    codex_native_process_registry_path,
    reconcile_codex_native_process_registry,
)
from omnigent.inner import _proc

# codex app-server --listen (used here) landed in 0.139.0.
_CODEX_MIN_VERSION = (0, 139, 0)

# Seconds to wait for the app server to launch the MCP wrappers, for a correct
# cleanup to reap them, and for close() to return. MCP children inherit the app
# server's stderr pipe, and asyncio's ``Process.wait()`` completes only once
# every pipe is closed, so a close that leaves children behind never returns.
_SPAWN_DEADLINE = 30.0
_REAP_DEADLINE = 10.0
_CLOSE_DEADLINE = 30.0

# Healthy stdio MCP stub: answers the MCP handshake, then (like the wrappers
# that piled up) keeps running after its stdin closes and ignores SIGTERM.
_MCP_STUB_SOURCE = textwrap.dedent(
    """\
    import json, os, signal, sys, time

    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    name = sys.argv[1]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": name, "version": "0.0.1"},
            }
            out = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
            sys.stdout.write(out + "\\n")
            sys.stdout.flush()
        elif method == "tools/list" and msg_id is not None:
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": []}}) + "\\n"
            )
            sys.stdout.flush()
        elif msg_id is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}) + "\\n")
            sys.stdout.flush()
    time.sleep(600)
    """
)

# Wedged stdio MCP stub: never answers initialize, as a wrapper stuck in a
# cold start does; it ignores SIGTERM too.
_MCP_HUNG_SOURCE = textwrap.dedent(
    """\
    import signal, sys, time

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while sys.stdin.readline():
        pass
    time.sleep(600)
    """
)

_STUB_NAMES = ("stub_alpha", "stub_beta", "stub_wedged")


def _codex_path() -> str | None:
    """The Codex CLI to drive, or ``None`` when none is usable."""
    path = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if path is None:
        return None
    proc = subprocess.run([path, "--version"], text=True, capture_output=True, check=False)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", f"{proc.stdout}\n{proc.stderr}")
    if proc.returncode != 0 or match is None:
        return None
    if tuple(int(part) for part in match.groups()) < _CODEX_MIN_VERSION:
        return None
    return path


def _free_port() -> int:
    """Reserve an ephemeral loopback port for the app server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _procs_matching(marker: str) -> list[psutil.Process]:
    """Live processes whose command line mentions *marker*."""
    found: list[psutil.Process] = []
    for proc in psutil.process_iter(["cmdline"]):
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            if any(marker in part for part in proc.info.get("cmdline") or []):
                found.append(proc)
    return found


def _still_alive(proc: psutil.Process) -> bool:
    """Whether *proc* is a live, non-zombie process."""
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    return False


async def _survivors(children: list[psutil.Process]) -> list[int]:
    """Pids of *children* still alive once the reap deadline has passed."""
    deadline = time.monotonic() + _REAP_DEADLINE
    while time.monotonic() < deadline:
        if not any(_still_alive(child) for child in children):
            return []
        await asyncio.sleep(0.5)
    return [child.pid for child in children if _still_alive(child)]


async def _start_app_server_with_mcp_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codex_path: str
) -> tuple[CodexNativeAppServer, list[psutil.Process]]:
    """
    Start a production app server whose thread has launched its MCP children.

    Process registry and owner locks live under *tmp_path*; the user Codex home
    production copies into the private one holds only the stub MCP config
    (production adds its own ``serve-mcp`` server, which exits by itself once
    its stdin closes and so is not one of the returned children).
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(state_dir))
    user_codex_home = tmp_path / "user_codex_home"
    user_codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(user_codex_home))
    stub = tmp_path / "mcp_stub.py"
    stub.write_text(_MCP_STUB_SOURCE)
    hung_stub = tmp_path / "mcp_stub_hung.py"
    hung_stub.write_text(_MCP_HUNG_SOURCE)
    config_lines: list[str] = []
    for name in _STUB_NAMES:
        script = hung_stub if name == "stub_wedged" else stub
        config_lines += [
            f"[mcp_servers.{name}]",
            f'command = "{sys.executable}"',
            f'args = ["{script}", "{name}"]',
            "",
        ]
    (user_codex_home / "config.toml").write_text("\n".join(config_lines))

    bridge_dir = tmp_path / "bridge"
    write_mcp_bridge_config(bridge_dir)
    server = CodexNativeAppServer(
        codex_path=codex_path,
        socket_path=tmp_path / "unused.sock",
        codex_home=tmp_path / "codex_home",
        env=dict(os.environ),
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=bridge_dir,
        python_executable=sys.executable,
        listen_url=f"ws://127.0.0.1:{_free_port()}",
        session_id="mcp-orphan-e2e",
    )
    await server.start()
    assert server.proc is not None
    client = CodexAppServerClient(ws_url=server.listen_url, client_name="mcp-orphan-e2e")
    await client.connect()
    try:
        # A real thread makes Codex launch the configured stdio MCP servers.
        await client.request("thread/start", {})
    finally:
        with contextlib.suppress(Exception):
            await client.close()

    deadline = time.monotonic() + _SPAWN_DEADLINE
    stubs: list[psutil.Process] = []
    while time.monotonic() < deadline:
        stubs = _procs_matching(str(tmp_path / "mcp_stub"))
        if len(stubs) >= len(_STUB_NAMES):
            break
        await asyncio.sleep(0.5)
    assert len(stubs) >= len(_STUB_NAMES), (
        f"codex launched {len(stubs)} of {len(_STUB_NAMES)} stubs"
    )

    # The reported shape: each MCP child in its own process group, yet all of
    # them still in the app server's session.
    app_server_pgid = os.getpgid(server.proc.pid)
    for child in stubs:
        assert os.getpgid(child.pid) != app_server_pgid
        assert os.getsid(child.pid) == server.proc.pid
    return server, stubs


def _skip_unless_runnable() -> str:
    if os.name != "posix":
        pytest.skip("POSIX sessions are the mechanism under test")
    codex_path = _codex_path()
    if codex_path is None:
        pytest.skip("codex CLI >= 0.139.0 is required for app-server --listen")
    return codex_path


async def _wait_dead(pid: int) -> None:
    """Poll until *pid* is gone; pipe-bound ``Process.wait()`` cannot be relied on."""
    deadline = time.monotonic() + _REAP_DEADLINE
    while _proc.process_alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not _proc.process_alive(pid)


async def _kill_leftovers(server: CodexNativeAppServer, children: list[psutil.Process]) -> None:
    for child in children:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            child.kill()
    if server.proc is not None and server.proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(server.proc.pid), signal.SIGCONT)
            os.killpg(os.getpgid(server.proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(server.close(), timeout=_REAP_DEADLINE)


async def test_closing_a_wedged_app_server_reaps_its_mcp_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``close()`` leaves no MCP child behind, even when Codex could not clean up.

    The app server is stopped with ``SIGSTOP`` first, so it cannot act on
    ``SIGTERM`` and ``close()`` has to escalate to ``SIGKILL``: Codex dies
    without reaping its MCP servers, exactly the shutdown that used to strand
    them.
    """
    codex_path = _skip_unless_runnable()
    server, children = await _start_app_server_with_mcp_children(tmp_path, monkeypatch, codex_path)
    try:
        assert server.proc is not None
        app_server_pid = server.proc.pid
        os.killpg(os.getpgid(app_server_pid), signal.SIGSTOP)
        started = time.monotonic()
        try:
            await asyncio.wait_for(server.close(), timeout=_CLOSE_DEADLINE)
        except TimeoutError:
            pytest.fail(
                "app-server close() did not return: the MCP children it left running "
                "hold its stderr pipe, so its Process.wait() never completes"
            )
        assert not _proc.process_alive(app_server_pid)
        assert time.monotonic() - started >= 4.0, "expected the SIGKILL escalation path"

        survivors = await _survivors(children)
        assert not survivors, (
            f"app-server close() left MCP children running: pids {survivors} "
            "(each in its own process group, outside the app server's group)"
        )
        assert not codex_native_process_registry_path().exists() or (
            codex_native_process_registry_path().read_text().strip() in ("", "[]")
        )
    finally:
        await _kill_leftovers(server, children)


async def test_janitor_reaps_mcp_children_of_a_crashed_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The crash-leftover janitor reaps the MCP children a dead app server left.

    The app server is killed outright (a crash, an OOM kill, an operator's
    cleanup script) and its owner lock released as a dead runner's would be.
    The janitor then finds the registry entry's pid gone and must still remove
    what the entry's session left behind.
    """
    codex_path = _skip_unless_runnable()
    server, children = await _start_app_server_with_mcp_children(tmp_path, monkeypatch, codex_path)
    try:
        assert server.proc is not None
        assert server.process_owner_lock is not None
        os.killpg(os.getpgid(server.proc.pid), signal.SIGKILL)
        await _wait_dead(server.proc.pid)
        server.process_owner_lock.close()
        server.process_owner_lock = None
        assert any(_still_alive(child) for child in children), "children died with the app server"

        reconcile_codex_native_process_registry()

        survivors = await _survivors(children)
        assert not survivors, (
            f"janitor left the dead app server's MCP children running: pids {survivors}"
        )
        assert codex_native_process_registry_path().read_text().strip() in ("", "[]")
    finally:
        await _kill_leftovers(server, children)
