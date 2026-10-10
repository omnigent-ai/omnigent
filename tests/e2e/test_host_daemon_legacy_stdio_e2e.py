"""``omnigent start`` must register its host daemon under legacy-code-page stdio.

The background daemon's stdout/stderr are its host log, so on a Windows cp1252
machine Python opens them with ``errors="strict"`` and the daemon's
``✓ Connected as`` banner raises ``UnicodeEncodeError`` inside the tunnel loop;
the host reconnects forever and ``omnigent start`` fails with "did not register
within 30s". This drives the real journey on any OS by giving the daemon cp1252
stdio through a ``sitecustomize.py`` on its ``PYTHONPATH`` (forwarded by the
daemon env allowlist, unlike ``PYTHONIOENCODING``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CPython's defaults for stdio redirected to a file on a cp1252-locale Windows
# machine: strict stdout, backslashreplace stderr; a console (tty) stays UTF-8.
_WINDOWS_CP1252_SITECUSTOMIZE = """\
import sys

for _name, _errors in (("stdout", "strict"), ("stderr", "backslashreplace")):
    _stream = getattr(sys, _name, None)
    if _stream is not None and not _stream.isatty():
        _stream.reconfigure(encoding="cp1252", errors=_errors)
"""

_TUNNEL_HOST_ID = re.compile(r"/v1/hosts/([0-9a-f]{32})/tunnel")

# Startup deadline. With the 30s cleanup below it stays well under CI's 180s
# per-test timeout, leaving headroom for the assertions in between.
_START_DEADLINE_S = 90.0


def _legacy_stdio_env(base: Path) -> dict[str, str]:
    """Build an isolated CLI environment whose spawned daemon gets cp1252 stdio."""
    site_dir = base / "sitecustomize"
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(_WINDOWS_CP1252_SITECUSTOMIZE, encoding="utf-8")
    for name in ("home", "config", "data"):
        (base / name).mkdir()
    env = {
        **os.environ,
        "HOME": str(base / "home"),
        "OMNIGENT_CONFIG_HOME": str(base / "config"),
        "OMNIGENT_DATA_DIR": str(base / "data"),
        # The daemon is spawned with -P, so the worktree must come from PYTHONPATH.
        "PYTHONPATH": os.pathsep.join(
            [
                str(site_dir),
                str(_REPO_ROOT),
                str(_REPO_ROOT / "sdks" / "python-client"),
                str(_REPO_ROOT / "sdks" / "ui"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ).rstrip(os.pathsep),
    }
    # Either interpreter flag would mask the legacy encoding; a loopback server needs no proxy.
    for name in (
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
    ):
        env.pop(name, None)
    return env


def test_start_registers_host_daemon_with_cp1252_stdio(live_server: str, tmp_path: Path) -> None:
    """The host comes online although the daemon's stdio cannot encode ``✓``."""
    env = _legacy_stdio_env(tmp_path)
    cwd = str(tmp_path / "home")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnigent",
                "start",
                "--server",
                live_server,
                "--no-open",
                "--non-interactive",
            ],
            env=env,
            cwd=cwd,
            capture_output=True,
            timeout=_START_DEADLINE_S,
        )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        host_logs = sorted((tmp_path / "data" / "logs" / "host").glob("host-*.log"))
        host_log = "\n".join(p.read_bytes().decode("utf-8", errors="replace") for p in host_logs)

        assert result.returncode == 0, (
            f"`omnigent start` exited {result.returncode} with the daemon's stdio on cp1252:\n"
            f"{output}"
        )
        assert "Started the host daemon in the background" in output, output
        assert host_logs, f"the host daemon produced no host log; CLI output:\n{output}"
        assert "charmap" not in host_log and "UnicodeEncodeError" not in host_log, (
            f"the host daemon could not encode its status output:\n{host_log[-3000:]}"
        )
        tunnel = _TUNNEL_HOST_ID.search(host_log)
        assert tunnel is not None, f"no tunnel connection in the host log:\n{host_log[-3000:]}"
        hosts = httpx.get(f"{live_server}/v1/hosts", timeout=10).json()["hosts"]
        status = next((h["status"] for h in hosts if h["host_id"] == tunnel.group(1)), None)
        assert status == "online", (
            f"host {tunnel.group(1)} is {status!r} on the server; host log:\n{host_log[-3000:]}"
        )
    finally:
        subprocess.run(
            [sys.executable, "-m", "omnigent", "stop", "--force"],
            env=env,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
