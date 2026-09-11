"""Cold-resumed native Codex sessions keep showing the synthesized MCP startup
band (``"starting"``) after ``Working...`` appears, while *fresh* sessions
clear it promptly.

Mechanism (confirmed live against the real codex app-server)
-----------------------------------------------------------
The codex-native forwarder synthesizes an MCP startup round at launch
(``_seed_mcp_startup_round`` posts ``external_mcp_startup`` with every
config-declared server marked ``starting``) and clears it on the first
*settle signal*. With a **slow** MCP handshake, neither an ``mcpServer``
``ready`` edge nor model output arrives promptly, so the only prompt clearer
is the initial idle ``thread/status/changed`` edge, which codex delivers
**only to the connection that first resumes the thread** (it is not replayed
to a later connection).

* **Fresh path** — the forwarder's own event client is the first to
  ``thread/resume`` the thread, so it receives that initial idle edge and
  ``_settle_mcp_startup`` clears the band.
* **Cold-resume path** — ``preload_codex_thread_for_resume`` resumes the
  thread on a *temporary* connection first (to make it injectable), consuming
  the initial idle edge; the forwarder connects *afterward*, never sees that
  idle edge, and the synthesized band stays stuck on ``starting``.

So this test drives the **real** ``supervise_forwarder`` against a **real**
codex app-server (plus the production ``preload_codex_thread_for_resume``),
captures the ``external_mcp_startup`` posts through a mock Omnigent client,
and asserts that the band clears **without** any MCP-ready event or model
output. Parameterized over ``fresh`` / ``resume``:

* before the fix, ``fresh`` clears (passes) and ``resume`` stays stuck (fails);
* after the fix, both clear.

Why a forwarder-level e2e (not a web-UI test)
---------------------------------------------
The user *sees* this as the web MCP-startup band lingering next to
``Working...``, but the failing state is produced entirely by the forwarder's
connect-ordering relative to preload. The web-UI band tests
(``tests/e2e_ui/chat/test_mcp_startup_indicator.py``) drive the band via direct
``external_mcp_startup`` posts and deliberately avoid the live codex TUI "whose
MCP round timing would make the assertions flaky", so they cannot exercise this
defect. Driving the real forwarder is the deterministic reproduction.

Environment
-----------
Needs only the ``codex`` binary on ``PATH`` (no Codex login and no real LLM:
the thread reaches ``idle`` and boots MCP without authentication; the deferred
turn never needs to complete). Skips when ``codex`` is absent. Lives under
``tests/e2e`` (excluded from the default pytest run); invoke explicitly::

    .venv/bin/python -m pytest \\
        tests/e2e/test_codex_native_resume_mcp_startup_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    preload_codex_thread_for_resume,
)
from omnigent.harnesses.codex_native.bridge import codex_home_for_bridge_dir
from omnigent.harnesses.codex_native.forwarder import supervise_forwarder

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex-native MCP-startup resume e2e needs the `codex` binary on PATH",
)

# The MCP handshake must not finish (no ``ready`` edge) and codex must not
# time it out (no ``failed``/``cancelled`` edge) within the observation
# window, so the *only* prompt clearer is the initial idle edge — exactly the
# report's "slow MCP" precondition. ``startup_timeout_sec`` (300) >> the
# observation window keeps codex from cancelling the server; the forwarder's
# own settle timer (min(300 + 15, 240) = 240s) is likewise far outside it.
_MCP_SLEEP_SECONDS = 600
_MCP_STARTUP_TIMEOUT_SECONDS = 300
# Fresh clears the band via the idle edge in well under a second; a generous
# window keeps the "should clear" assertion non-flaky while staying far below
# the ready / timeout / settle-timer horizons above.
_OBSERVE_WINDOW_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.25

_SLOW_MCP_SCRIPT = '''\
"""Minimal MCP stdio server with a handshake too slow to finish in-window."""
import json
import sys
import time

time.sleep({sleep})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        out = {{
            "jsonrpc": "2.0",
            "id": mid,
            "result": {{
                "protocolVersion": (msg.get("params") or {{}}).get(
                    "protocolVersion", "2024-11-05"
                ),
                "capabilities": {{"tools": {{}}}},
                "serverInfo": {{"name": "slowmcp", "version": "1.0"}},
            }},
        }}
    elif mid is not None and method == "tools/list":
        out = {{"jsonrpc": "2.0", "id": mid, "result": {{"tools": []}}}}
    elif mid is not None:
        out = {{"jsonrpc": "2.0", "id": mid, "result": {{}}}}
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn_app_server(port: int, codex_home: Path, cwd: Path, log: Path) -> subprocess.Popen:
    import os

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


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _band_has_starting(servers: dict) -> bool:
    return any((entry or {}).get("status") == "starting" for entry in servers.values())


def _capture_transport(posts: list[dict]) -> httpx.MockTransport:
    """Record every ``external_mcp_startup`` map the forwarder posts."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = {}
            if body.get("type") == "external_mcp_startup":
                posts.append(dict(body["data"]["servers"]))
        # Any other AP call (session snapshot GET, replayed items, dead-letter
        # drain) gets a harmless 200 — none affects the band assertion.
        return httpx.Response(200, json={"id": "conv_probe", "labels": {}})

    return httpx.MockTransport(handler)


@pytest.fixture(scope="module")
def persisted_codex_thread(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """Create + materialize one codex thread both scenarios can resume.

    :returns: ``(shared_codex_home, thread_id)``.
    """
    root = tmp_path_factory.mktemp("codex_mcp_startup")
    shared_home = root / "codex-home"
    shared_home.mkdir(parents=True)
    work = root / "work"
    work.mkdir()

    mcp_script = root / "slow_mcp.py"
    mcp_script.write_text(_SLOW_MCP_SCRIPT.format(sleep=_MCP_SLEEP_SECONDS), encoding="utf-8")
    (shared_home / "config.toml").write_text(
        "[mcp_servers.slowmcp]\n"
        f'command = "{shutil.which("python3") or "python3"}"\n'
        f'args = ["{mcp_script}"]\n'
        f"startup_timeout_sec = {_MCP_STARTUP_TIMEOUT_SECONDS}\n",
        encoding="utf-8",
    )

    async def _make() -> str:
        port = _free_port()
        proc = _spawn_app_server(port, shared_home, work, root / "setup-app-server.log")
        try:
            await _wait_listener(port, proc)
            creator = CodexAppServerClient(
                ws_url=f"ws://127.0.0.1:{port}", client_name="resume-mcp-setup"
            )
            await creator.connect()
            resp = await creator.request(
                "thread/start",
                {"approvalPolicy": "never", "cwd": str(work), "sandbox": "read-only"},
            )
            thread_id = resp.get("result", {}).get("thread", {}).get("id")
            assert isinstance(thread_id, str) and thread_id, "codex did not return a thread id"
            # Accepting a turn materializes the rollout on disk so a later
            # app-server can thread/resume it; the turn itself never needs to
            # complete (it stays deferred behind the slow MCP).
            with contextlib.suppress(Exception):
                await creator.request(
                    "turn/start",
                    {"threadId": thread_id, "input": [{"type": "text", "text": "hello"}]},
                )
            await asyncio.sleep(3)
            await creator.close()
            return thread_id
        finally:
            _terminate(proc)

    thread_id = asyncio.run(_make())
    return shared_home, thread_id


def _bridge_dir(shared_home: Path, root: Path, scenario: str) -> Path:
    """Per-scenario bridge dir whose private codex-home is the shared thread store."""
    bridge_dir = root / f"bridge_{scenario}"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    home_link = codex_home_for_bridge_dir(bridge_dir)
    if home_link.is_symlink() or home_link.exists():
        if home_link.is_symlink():
            home_link.unlink()
    if not home_link.exists():
        home_link.symlink_to(shared_home)
    # A clean synthesized round each launch (mirrors clear_bridge_state).
    mcp_state = bridge_dir / "mcp_startup.json"
    if mcp_state.exists():
        mcp_state.unlink()
    return bridge_dir


async def _run_scenario(
    scenario: str, shared_home: Path, thread_id: str, work: Path, log_dir: Path
) -> list[dict]:
    """Drive the real forwarder for one scenario, return posted startup maps."""
    bridge_dir = _bridge_dir(shared_home, log_dir, scenario)
    port = _free_port()
    proc = _spawn_app_server(port, shared_home, work, log_dir / f"{scenario}-app-server.log")
    posts: list[dict] = []
    fwd_client: CodexAppServerClient | None = None
    ws_url = f"ws://127.0.0.1:{port}"
    try:
        await _wait_listener(port, proc)
        if scenario == "resume":
            # Cold-resume: preload on a temporary connection FIRST (consumes
            # the initial idle edge), then the forwarder connects afterward.
            await preload_codex_thread_for_resume(ws_url, thread_id)
        else:
            # Fresh: the forwarder's own client connects BEFORE any resume, so
            # it is the first to thread/resume and catches the initial idle.
            fwd_client = CodexAppServerClient(ws_url=ws_url, client_name="resume-mcp-fresh")
            await fwd_client.connect()

        task = asyncio.create_task(
            supervise_forwarder(
                base_url="http://127.0.0.1:0",
                headers={},
                session_id="conv_probe",
                bridge_dir=bridge_dir,
                app_server_url=ws_url,
                thread_id=thread_id,
                client=fwd_client,
                ap_transport=_capture_transport(posts),
            )
        )
        try:
            deadline = time.monotonic() + _OBSERVE_WINDOW_SECONDS
            while time.monotonic() < deadline:
                # Cleared == the band was seeded 'starting' and then a later
                # post dropped it (no server left 'starting'), with neither a
                # ready edge nor model output.
                if any(_band_has_starting(p) for p in posts) and not _band_has_starting(posts[-1]):
                    break
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        _terminate(proc)
    return posts


@pytest.mark.parametrize("scenario", ["fresh", "resume"])
def test_codex_native_mcp_startup_band_clears(
    scenario: str,
    persisted_codex_thread: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """The synthesized MCP startup band clears without ready/model output.

    ``fresh`` passes on the buggy build; ``resume`` fails on the buggy build
    (the band stays stuck on ``starting`` for the whole window) and passes
    once the forwarder connects before the resume preload.
    """
    shared_home, thread_id = persisted_codex_thread
    work = shared_home.parent / "work"

    posts = asyncio.run(_run_scenario(scenario, shared_home, thread_id, work, tmp_path))

    assert posts, "forwarder never posted the synthesized MCP startup band"
    assert any(_band_has_starting(p) for p in posts), (
        f"expected a seeded 'starting' band; got {posts!r}"
    )

    final = posts[-1]
    assert not _band_has_starting(final), (
        f"[{scenario}] MCP startup band never cleared within "
        f"{_OBSERVE_WINDOW_SECONDS}s without an MCP-ready event or model "
        f"output — it stayed stuck on 'starting'. Posts: {posts!r}"
    )
