"""The shared relay hook command answers from the relay, else replays stdin."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import _TOOL_RELAY_FILE, start_tool_relay
from omnigent.harnesses.pi_native import bridge as pi_native_bridge
from omnigent.native.relay_hook import (
    TOOL_RELAY_CURL_FILE,
    TOOL_RELAY_ENV_FILE,
    relay_hook_command,
)

_PAYLOAD = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash"})


def _run(command: str, payload: str = _PAYLOAD, **env: str) -> subprocess.CompletedProcess[str]:
    # As an argv, the way harnesses that do not use a shell would run it.
    return subprocess.run(
        shlex.split(command),
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **env},
    )


async def _unused_executor(name: str, arguments: dict[str, object]) -> object:
    raise AssertionError(f"no tool call expected, got {name}({arguments})")


def _curl_shim(directory: Path) -> tuple[Path, Path]:
    """Put a ``curl`` on PATH that records its argv before running the real one."""
    real_curl = shutil.which("curl")
    assert real_curl, "curl is required for the relay hooks"
    directory.mkdir(parents=True, exist_ok=True)
    argv_log = directory / "curl-argv"
    shim = directory / "curl"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> {shlex.quote(str(argv_log))}\n'
        f'exec {shlex.quote(real_curl)} "$@"\n'
    )
    shim.chmod(0o700)
    return shim.parent, argv_log


class _StrayServer(ThreadingHTTPServer):
    """A second listener an ambient ``.curlrc`` could point curl at."""

    def __init__(self) -> None:
        self.hits: list[tuple[str, str | None]] = []
        super().__init__(("127.0.0.1", 0), _StrayHandler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class _StrayHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.hits.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST

    def log_message(self, format: str, *args: object) -> None:
        del format, args


async def test_relay_hook_command_replays_stdin_unless_the_relay_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    bridge_dir = pi_native_bridge.prepare_bridge_dir("conv_relay_hook")
    command = relay_hook_command(
        bridge_dir, "/hook/observe-tool", "sed s/^/fallback:/", max_time_s=5
    )
    shim_dir, argv_log = _curl_shim(tmp_path / "shim")
    path = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    # An ambient curl config that adds a destination must never see the hook.
    stray = _StrayServer()
    curl_home = tmp_path / "curl-home"
    curl_home.mkdir()
    (curl_home / ".curlrc").write_text(f'url = "{stray.url}/stray"\n')
    env = {"PATH": path, "CURL_HOME": str(curl_home), "HOME": str(curl_home)}

    # Relay not started yet: no env file, so stdin goes to the fallback.
    absent = await asyncio.to_thread(_run, command, _PAYLOAD, **env)
    assert absent.returncode == 0, absent.stderr
    assert absent.stdout.strip() == f"fallback:{_PAYLOAD}"
    assert not argv_log.exists(), "curl must not run before the relay advertises itself"

    relay = start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[],
        tool_executor=_unused_executor,
        loop=asyncio.get_running_loop(),
    )
    try:
        token = json.loads((bridge_dir / _TOOL_RELAY_FILE).read_text())["token"]
        curlrc = bridge_dir / TOOL_RELAY_CURL_FILE
        assert stat.S_IMODE(curlrc.stat().st_mode) == 0o600
        assert curlrc.read_text() == f'header = "Authorization: Bearer {token}"\n'
        answered = await asyncio.to_thread(_run, command, _PAYLOAD, **env)
        assert answered.returncode == 0, answered.stderr
        assert answered.stdout == "", "the relay's (empty) answer is the hook output"
        # The bearer reaches curl only through its config file, never argv, and
        # the ambient .curlrc is ignored (-q leads the argv) so the stray
        # destination sees neither the payload nor the bearer.
        argv = argv_log.read_text().splitlines()
        assert argv[0] == "-q"
        assert argv[argv.index("-K") + 1] == str(curlrc)
        assert all(token not in arg for arg in argv)
        assert stray.hits == []

        # An older relay without this route answers 404: curl -f rejects it
        # and the fallback still runs with the same payload.
        unknown = relay_hook_command(
            bridge_dir, "/hook/not-served-here", "sed s/^/fallback:/", max_time_s=5
        )
        missing_route = await asyncio.to_thread(_run, unknown, _PAYLOAD, **env)
        assert missing_route.returncode == 0, missing_route.stderr
        assert missing_route.stdout.strip() == f"fallback:{_PAYLOAD}"

        # A relay from before the curl config existed: env file only, so the
        # command does not try curl and replays the same stdin.
        curlrc.rename(tmp_path / "curlrc.aside")
        argv_log.unlink()
        upgrading = await asyncio.to_thread(_run, command, _PAYLOAD, **env)
        assert upgrading.returncode == 0, upgrading.stderr
        assert upgrading.stdout.strip() == f"fallback:{_PAYLOAD}"
        assert not argv_log.exists()
        (tmp_path / "curlrc.aside").rename(curlrc)
    finally:
        relay.close()
        stray.close()

    # Runner gone, relay files left behind: curl fails and stdin is replayed.
    assert (bridge_dir / TOOL_RELAY_ENV_FILE).is_file()
    stale = await asyncio.to_thread(_run, command, _PAYLOAD, **env)
    assert stale.returncode == 0, stale.stderr
    assert stale.stdout.strip() == f"fallback:{_PAYLOAD}"


def test_relay_hook_command_bounds_its_stdin_when_asked(tmp_path: Path) -> None:
    """``max_bytes`` drops an oversized payload before the relay or the fallback see it."""
    command = relay_hook_command(
        tmp_path, "/hook/observe-tool", "wc -c", max_time_s=5, max_bytes=64
    )
    oversized = "x" * 1000
    dropped = _run(command, oversized)
    assert dropped.returncode == 0, dropped.stderr
    assert dropped.stdout == "", "an oversized payload must not reach the fallback"
    kept = _run(command, "y" * 64)
    assert kept.stdout.strip() == "64", "a payload at the bound still reaches the fallback whole"
    unbounded = _run(
        relay_hook_command(tmp_path, "/hook/observe-tool", "wc -c", max_time_s=5), oversized
    )
    assert unbounded.stdout.strip() == "1000"
