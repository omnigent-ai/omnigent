"""Recording driver for the cold-resume MCP-startup band.

Films the user-visible behavior on a cold-resumed native Codex session: the
web MCP-startup band must clear promptly instead of staying stuck on
"Starting MCP servers …". Unlike the deterministic regression guard
(``tests/e2e/test_codex_native_resume_mcp_startup_e2e.py``, which asserts on
the forwarder's posts through a mock client), this drives the **real**
``supervise_forwarder`` — through the production
``preload_codex_thread_for_resume`` — pointed at the **live e2e_ui server and
session**, so the band the browser renders is produced by the actual resume
code path, and captures the session page on video.

Recording-only: skipped unless ``OMNIGENT_E2E_RECORD_CODEX_RESUME_MCP=1``
(and ``codex`` is on PATH), so it never runs as a normal test. Run it with
the web recorder on the sync ``page`` fixture, e.g.::

    OMNIGENT_E2E_RECORD_CODEX_RESUME_MCP=1 \\
    pytest tests/e2e_ui/chat/test_codex_resume_mcp_startup_recording.py \\
        --ui-skip-build
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    preload_codex_thread_for_resume,
)
from omnigent.harnesses.codex_native.bridge import codex_home_for_bridge_dir
from omnigent.harnesses.codex_native.forwarder import supervise_forwarder

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_RECORD_CODEX_RESUME_MCP") != "1" or shutil.which("codex") is None,
    reason="recording-only driver; set OMNIGENT_E2E_RECORD_CODEX_RESUME_MCP=1 with codex on PATH",
)

_BAND = '[data-testid="mcp-startup-indicator"]'
_MCP_SLEEP_SECONDS = 600
_MCP_STARTUP_TIMEOUT_SECONDS = 300
_FILM_SECONDS = 8.0
_SETUP_TIMEOUT_SECONDS = 180.0

_SLOW_MCP_SCRIPT = f"""\
import json, sys, time
time.sleep({_MCP_SLEEP_SECONDS})
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    mid = msg.get("id")
    if msg.get("method") == "initialize":
        out = {{
            "jsonrpc": "2.0",
            "id": mid,
            "result": {{
                "protocolVersion": "2024-11-05",
                "capabilities": {{"tools": {{}}}},
                "serverInfo": {{"name": "slowmcp", "version": "1.0"}},
            }},
        }}
    elif mid is not None:
        out = {{"jsonrpc": "2.0", "id": mid, "result": {{"tools": []}}}}
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn_app_server(port: int, codex_home: Path, cwd: Path, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    return subprocess.Popen(
        ["codex", "app-server", "--listen", f"ws://127.0.0.1:{port}"],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log.open("w"),
    )


async def _wait_listener(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"codex app-server exited early (rc={proc.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            await asyncio.sleep(0.2)
    raise TimeoutError("codex app-server listener never came up")


def test_record_codex_resume_mcp_startup_band_clears(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Film the cold-resumed session rendering without a stuck MCP band."""
    base_url, session_id = seeded_session

    shared_home = tmp_path / "codex-home"
    shared_home.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    mcp_script = tmp_path / "slow_mcp.py"
    mcp_script.write_text(_SLOW_MCP_SCRIPT, encoding="utf-8")
    (shared_home / "config.toml").write_text(
        "[mcp_servers.slowmcp]\n"
        f'command = "{shutil.which("python3") or "python3"}"\n'
        f'args = ["{mcp_script}"]\n'
        f"startup_timeout_sec = {_MCP_STARTUP_TIMEOUT_SECONDS}\n",
        encoding="utf-8",
    )

    procs: list[subprocess.Popen] = []
    ready = threading.Event()
    stop = threading.Event()
    setup_error: dict[str, BaseException] = {}

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _make_thread() -> str:
            port = _free_port()
            proc = _spawn_app_server(port, shared_home, work, tmp_path / "setup.log")
            procs.append(proc)
            await _wait_listener(port, proc)
            creator = CodexAppServerClient(
                ws_url=f"ws://127.0.0.1:{port}", client_name="rec-setup"
            )
            await creator.connect()
            resp = await creator.request(
                "thread/start",
                {"approvalPolicy": "never", "cwd": str(work), "sandbox": "read-only"},
            )
            tid = resp["result"]["thread"]["id"]
            with contextlib.suppress(Exception):
                await creator.request(
                    "turn/start",
                    {"threadId": tid, "input": [{"type": "text", "text": "hello"}]},
                )
            await asyncio.sleep(3)
            await creator.close()
            proc.terminate()
            return tid

        async def _setup_and_run() -> None:
            thread_id = await _make_thread()

            # Fresh app-server for the resume drive; cold-resume preloads on a
            # temporary connection FIRST (consuming the initial idle edge).
            resume_port = _free_port()
            resume_proc = _spawn_app_server(
                resume_port, shared_home, work, tmp_path / "resume.log"
            )
            procs.append(resume_proc)
            ws_url = f"ws://127.0.0.1:{resume_port}"
            await _wait_listener(resume_port, resume_proc)
            await preload_codex_thread_for_resume(ws_url, thread_id)

            bridge_dir = tmp_path / "bridge_resume"
            bridge_dir.mkdir()
            home_link = codex_home_for_bridge_dir(bridge_dir)
            home_link.symlink_to(shared_home)

            # client=None → the forwarder connects AFTER the preload above (the
            # production cold-resume ordering). Its own thread/resume success
            # settles the synthesized 'starting' band on the LIVE session.
            task = asyncio.create_task(
                supervise_forwarder(
                    base_url=base_url,
                    headers={},
                    session_id=session_id,
                    bridge_dir=bridge_dir,
                    app_server_url=ws_url,
                    thread_id=thread_id,
                    client=None,
                    thread_preloaded_for_resume=True,
                )
            )
            # Let the forwarder connect and post its seed 'starting' round so the
            # server snapshot cache holds it before the page loads.
            await asyncio.sleep(4)
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        try:
            loop.run_until_complete(_setup_and_run())
        except BaseException as exc:
            setup_error["exc"] = exc
            ready.set()
        finally:
            loop.close()

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    try:
        if not ready.wait(timeout=_SETUP_TIMEOUT_SECONDS):
            raise TimeoutError("forwarder setup did not complete in time")
        if "exc" in setup_error:
            raise setup_error["exc"]

        band = page.locator(_BAND)
        page.goto(f"{base_url}/c/{session_id}")
        # Anchor on the rendered chat surface first so the band's absence is
        # proven on a loaded session page, not a blank one.
        expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=15_000)
        # The forwarder settles the synthesized round once its own
        # thread/resume succeeds, so the cold-resumed page renders with the
        # band already cleared — even though slowmcp is still handshaking.
        expect(band).to_have_count(0, timeout=10_000)
        # Hold the cleared state on camera, then confirm it STAYS cleared.
        page.wait_for_timeout(int(_FILM_SECONDS * 1000))
        expect(band).to_have_count(0)
    finally:
        stop.set()
        worker.join(timeout=20)
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
