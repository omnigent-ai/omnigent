"""Check that a dispatched agy worker inherits the user's MCP servers.

Requires agy and a POSIX terminal; reads agy's startup log, no sign-in or model turn."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import time
from pathlib import Path

import pexpect
import pytest

from omnigent.harnesses.antigravity_native.bridge import (
    agy_gemini_dir,
    seed_isolated_agy_home,
    write_mcp_config,
)
from omnigent.harnesses.antigravity_native.launch import agy_binary_path

try:
    _AGY_BIN: str | None = agy_binary_path()
except RuntimeError:
    _AGY_BIN = None

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        _AGY_BIN is None,
        reason=(
            "antigravity-native isolated-mcp e2e needs the real `agy` CLI on PATH "
            "(or ~/.local/bin/agy); install it with "
            "`curl -fsSL https://antigravity.google/cli/install.sh | bash`"
        ),
    ),
]

# agy logs the servers that have not connected once its MCP scan has run a while;
# hanging stand-ins keep every declared server in that list so the log names them.
_HANGING_SERVER = {"command": "/bin/sh", "args": ["-c", "sleep 300"]}
_USER_MCP_SERVERS = {"graft": _HANGING_SERVER, "graphify": _HANGING_SERVER}

_CONNECTING_RE = re.compile(r"server\(s\) still connecting after \d+s:\s*(.+?)\s*$")
_AGY_MCP_SCAN_TIMEOUT = 75.0


def _agy_loaded_mcp_servers(gemini_dir: Path, *, cwd: Path) -> set[str]:
    """Launch agy against *gemini_dir*; return the servers its log reports still connecting."""
    assert _AGY_BIN is not None
    child = pexpect.spawn(
        _AGY_BIN,
        [f"--gemini_dir={gemini_dir}"],
        encoding="utf-8",
        codec_errors="replace",
        timeout=_AGY_MCP_SCAN_TIMEOUT,
        dimensions=(40, 200),
        cwd=str(cwd),
        env={**os.environ, "TERM": "xterm-256color"},
    )
    log_dir = gemini_dir / "antigravity-cli" / "log"
    servers: set[str] = set()
    try:
        deadline = time.monotonic() + _AGY_MCP_SCAN_TIMEOUT
        while time.monotonic() < deadline:
            try:
                child.read_nonblocking(4096, timeout=1)
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                break
            for log_path in sorted(log_dir.glob("*.log")) if log_dir.is_dir() else []:
                for line in log_path.read_text(errors="replace").splitlines():
                    match = _CONNECTING_RE.search(line)
                    if match:
                        servers |= {name.strip() for name in match.group(1).split(",")}
            if servers:
                break
    finally:
        # The hanging stand-ins share agy's process group; kill it so none outlive the test.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.close(force=True)
    return servers


def test_dispatched_agy_worker_inherits_user_mcp_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    real_gemini = fake_home / ".gemini"
    (real_gemini / "config").mkdir(mode=0o700, parents=True)
    (real_gemini / "config" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": _USER_MCP_SERVERS}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700)
    trusted_ws = tmp_path / "ws"
    trusted_ws.mkdir()
    write_mcp_config(bridge_dir)
    seed_isolated_agy_home(bridge_dir, trusted_workspace=trusted_ws)
    iso_gemini = agy_gemini_dir(bridge_dir)

    iso_config = iso_gemini / "config" / "mcp_config.json"
    assert iso_config.is_file(), "seeder wrote no isolated config/mcp_config.json"
    iso_servers = set(json.loads(iso_config.read_text())["mcpServers"])
    assert "omnigent" in iso_servers, "isolated config lost the Omnigent relay entry"

    interactive_ws = tmp_path / "interactive-ws"
    interactive_ws.mkdir()
    dispatched_ws = tmp_path / "dispatched-ws"
    dispatched_ws.mkdir()

    interactive = _agy_loaded_mcp_servers(real_gemini, cwd=interactive_ws)
    assert {"graft", "graphify"} <= interactive, (
        "interactive agy did not load the fixture MCP servers \u2014 fixture invalid "
        f"(loaded {sorted(interactive)})"
    )

    dispatched = _agy_loaded_mcp_servers(iso_gemini, cwd=dispatched_ws)
    assert {"graft", "graphify"} <= dispatched, (
        "dispatched agy worker lost the user's MCP servers: interactive agy loaded "
        f"{sorted(interactive)} but the isolated --gemini_dir worker loaded "
        f"{sorted(dispatched)} (isolated config has {sorted(iso_servers)})"
    )
