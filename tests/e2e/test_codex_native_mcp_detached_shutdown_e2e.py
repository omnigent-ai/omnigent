"""Codex-native detached-MCP shutdown e2e.

User journey: a persistent host runs a native Codex session whose Codex app
server launches several stdio MCP servers, and one of them is slow/wedged at
startup. Codex puts each stdio MCP wrapper in its **own POSIX process group**
(``rmcp-client``'s ``stdio_server_launcher``). When the session ends, Omnigent's
``CodexNativeAppServer.close`` tears the app server down through
``_proc.terminate_tree`` then, if the parent stays alive, ``_proc.kill_tree``.
The wedged MCP stalls Codex's graceful shutdown past ``close()``'s 5s grace, so
Omnigent escalates to SIGKILL and Codex dies before it can reap its own MCP
children. Both teardown functions take a process-group fast path that signals
only the app server's *own* group and never walks the detached MCP child
groups, so the healthy MCP wrappers survive the app server's death, are
reparented to init, and accumulate across the host's lifetime.

This drives the **real** ``codex app-server`` binary with real stdio MCP stubs
and the **real** ``_proc`` teardown functions the production ``close()`` path
uses, then asserts the healthy MCP wrappers are gone once the app server has
exited (the report: "Check that the other wrappers exit").

Against the current code this FAILS: graceful shutdown stalls on the wedged MCP,
``close()`` escalates to SIGKILL, and the detached healthy wrappers survive.
Once shutdown walks the MCP child groups it passes. The stubs keep running after
their stdin closes, so a wrapper still alive here is a genuine orphan, not one
mid-exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
from omnigent.inner import _proc

# codex app-server --listen (used here) landed in 0.139.0.
_CODEX_MIN_VERSION = (0, 139, 0)

# Seconds to wait for the app server to launch the MCP wrappers, and to wait
# for a correct shutdown to reap them.
_SPAWN_DEADLINE = 30.0
_REAP_DEADLINE = 10.0

# Grace before close() escalates SIGTERM -> SIGKILL, matching
# CodexNativeAppServer.close.
_CLOSE_GRACE = 5.0

# Healthy stdio MCP stub: answers the MCP handshake, then (like many real MCP
# servers) keeps running after its stdin closes, so survival past app-server
# shutdown is unambiguous rather than a mid-exit race.
_MCP_STUB_SOURCE = textwrap.dedent(
    """\
    import json, os, sys, time

    def log(rec):
        with open(sys.argv[2], "a") as fh:
            fh.write(json.dumps(rec) + "\\n")
            fh.flush()

    name = sys.argv[1]
    log({"event": "start", "name": name, "pid": os.getpid(), "pgid": os.getpgid(0)})
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

# Wedged stdio MCP stub: never answers initialize, so Codex's graceful shutdown
# stalls waiting on it and close() escalates to SIGKILL.
_MCP_HUNG_SOURCE = textwrap.dedent(
    """\
    import json, os, sys, time

    def log(rec):
        with open(sys.argv[2], "a") as fh:
            fh.write(json.dumps(rec) + "\\n")
            fh.flush()

    name = sys.argv[1]
    log({"event": "start-hung", "name": name, "pid": os.getpid(), "pgid": os.getpgid(0)})
    while sys.stdin.readline():
        pass
    time.sleep(600)
    """
)

_HEALTHY_NAMES = ("stub_alpha", "stub_beta")
_HUNG_NAME = "stub_wedged"


def _codex_supports_listen(codex_path: str) -> bool:
    """Whether the Codex CLI is new enough for ``app-server --listen``."""
    proc = subprocess.run([codex_path, "--version"], text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return False
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", f"{proc.stdout}\n{proc.stderr}")
    if not match:
        return False
    return tuple(int(part) for part in match.groups()) >= _CODEX_MIN_VERSION


def _free_port() -> int:
    """Reserve an ephemeral loopback port for the app server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wrapper_procs(marker: Path) -> list[psutil.Process]:
    """Return live processes whose command line references ``marker``."""
    found: list[psutil.Process] = []
    for proc in psutil.process_iter(["cmdline"]):
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            cmdline = proc.info.get("cmdline") or []
            if any(str(marker) in part for part in cmdline):
                found.append(proc)
    return found


def _still_alive(proc: psutil.Process) -> bool:
    """Whether ``proc`` is a live, non-zombie process."""
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    return False


async def _wait_ready(proc: asyncio.subprocess.Process, ws_url: str) -> CodexAppServerClient:
    """Connect an initialized app-server client, or fail if it never comes up."""
    deadline = time.monotonic() + 30.0
    last: Exception | None = None
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            raise RuntimeError(f"codex app-server exited early rc={proc.returncode}")
        client = CodexAppServerClient(ws_url=ws_url)
        try:
            await client.connect()
            return client
        except Exception as exc:  # retry until the socket is up
            last = exc
            with contextlib.suppress(Exception):
                await client.close()
            await asyncio.sleep(0.5)
    raise RuntimeError(f"codex app-server never became ready: {last}")


async def test_codex_app_server_close_reaps_detached_mcp_processes(tmp_path: Path) -> None:
    """Closing a Codex app server must terminate its detached MCP wrappers."""
    if os.name != "posix":
        pytest.skip("detached process-group reaping is POSIX-specific")
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the native app-server shutdown e2e")
    if not _codex_supports_listen(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for app-server --listen")

    healthy_stub = tmp_path / "mcp_stub.py"
    healthy_stub.write_text(_MCP_STUB_SOURCE)
    hung_stub = tmp_path / "mcp_stub_hung.py"
    hung_stub.write_text(_MCP_HUNG_SOURCE)
    stub_log = tmp_path / "mcp_stub.log"

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    config_lines: list[str] = [""]
    for name in _HEALTHY_NAMES:
        config_lines += [
            f"[mcp_servers.{name}]",
            f'command = "{sys.executable}"',
            f'args = ["{healthy_stub}", "{name}", "{stub_log}"]',
            "",
        ]
    config_lines += [
        f"[mcp_servers.{_HUNG_NAME}]",
        f'command = "{sys.executable}"',
        f'args = ["{hung_stub}", "{_HUNG_NAME}", "{stub_log}"]',
        "",
    ]
    (codex_home / "config.toml").write_text("\n".join(config_lines))

    port = _free_port()
    ws_url = f"ws://127.0.0.1:{port}"
    # The child dups the descriptor at spawn, so ours can close right away.
    with (tmp_path / "app_server.stderr").open("wb") as stderr_handle:
        proc = await asyncio.create_subprocess_exec(
            codex_path,
            "app-server",
            "--listen",
            ws_url,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_handle,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            cwd=str(tmp_path),
            # Matches CodexNativeAppServer.start: new session -> own process group.
            **_proc.spawn_kwargs(),
        )
    app_server_pgid = os.getpgid(proc.pid)
    healthy_wrappers: list[psutil.Process] = []
    try:
        client = await _wait_ready(proc, ws_url)
        try:
            # A real thread makes Codex launch the configured stdio MCP servers.
            await client.request("thread/start", {})
        finally:
            with contextlib.suppress(Exception):
                await client.close()

        spawn_deadline = time.monotonic() + _SPAWN_DEADLINE
        while time.monotonic() < spawn_deadline:
            healthy_wrappers = _wrapper_procs(healthy_stub)
            if len(healthy_wrappers) >= len(_HEALTHY_NAMES):
                break
            await asyncio.sleep(0.5)
        if len(healthy_wrappers) < len(_HEALTHY_NAMES):
            pytest.skip(
                "codex did not launch the stdio MCP wrappers in this environment "
                f"(saw {len(healthy_wrappers)}); cannot exercise detached-group shutdown"
            )

        # The reported precondition: each MCP wrapper is in its own POSIX group,
        # detached from the app server's group.
        wrapper_pgids = set()
        for wrapper in healthy_wrappers:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                wrapper_pgids.add(os.getpgid(wrapper.pid))
        assert wrapper_pgids and app_server_pgid not in wrapper_pgids, (
            "expected MCP wrappers in their own process groups distinct from the "
            f"app server group {app_server_pgid}; saw {wrapper_pgids}"
        )

        # Exactly CodexNativeAppServer.close: SIGTERM the tree, wait 5s, then
        # SIGKILL-escalate on timeout (the wedged MCP stalls graceful shutdown).
        escalated = False
        _proc.terminate_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_CLOSE_GRACE)
        except asyncio.TimeoutError:
            escalated = True
            _proc.kill_tree(proc)
            await proc.wait()

        reap_deadline = time.monotonic() + _REAP_DEADLINE
        survivors = healthy_wrappers
        while time.monotonic() < reap_deadline:
            survivors = [w for w in healthy_wrappers if _still_alive(w)]
            if not survivors:
                break
            await asyncio.sleep(0.5)

        survivor_pids = [w.pid for w in healthy_wrappers if _still_alive(w)]
        assert not survivor_pids, (
            "Codex app-server shutdown left detached MCP wrappers running: "
            f"pids {survivor_pids} survived close() (escalated_to_sigkill={escalated}; "
            f"reaped only the app-server group {app_server_pgid}, not the wrappers' "
            "own process groups)"
        )
    finally:
        for wrapper in _wrapper_procs(healthy_stub) + _wrapper_procs(hung_stub):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                wrapper.kill()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                _proc.kill_tree(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
