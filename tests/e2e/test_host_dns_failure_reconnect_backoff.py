"""End-to-end repro: host reconnect has no backoff on Windows DNS failure.

When the host daemon's tunnel connect fails with the Windows resolver
error ``[Errno 11001] getaddrinfo failed`` (WSAHOST_NOT_FOUND — e.g. the
VPN that resolves the server hostname is down), the reconnect loop
retries at the fixed 0.5s base interval indefinitely: every attempt is
announced as ``Reconnecting in 0.5s (recycle — prompt reconnect)`` and
the interval never grows. The same journey with a Linux resolver error
string backs off normally (0.5s → ~1.3s → 3.0s cap), so the recycle
fast path is misfiring on the Windows error specifically: the
classifier substring-matches close-code tokens against the exception
text, and ``"1001"`` (WebSocket "going away") is a substring of errno
``11001``.

This drives the real user journey: spawn the actual ``omnigent host
--server <url> --non-interactive`` process against a hostname whose DNS
resolution fails exactly like the report (a ``sitecustomize`` shim makes
``socket.getaddrinfo`` raise ``gaierror(11001, "getaddrinfo failed")``
for that one hostname — the local emulation of "VPN down on Windows"),
then reads the host's own log and asserts the announced reconnect delay
grows past the 0.5s base.

Run with::

    python -m pytest tests/e2e/test_host_dns_failure_reconnect_backoff.py -v --timeout=180
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The hostname the shim makes unresolvable; never touches real DNS.
_UNRESOLVABLE_HOST = "vpn-only-server.omni-dns.invalid"

# Mirrors _RECONNECT_BASE_S in omnigent/host/connect.py: the fixed
# interval the buggy path announces on every attempt. A working backoff
# must announce something larger within a few attempts.
_RECONNECT_BASE_S = 0.5

# How many "Reconnecting in Xs" announcements to collect before judging.
# With a working backoff (0.5 → ~1.3 → 3.0s cap) six attempts take ~14s;
# with the bug they take ~5s. Both fit the deadline comfortably.
_MIN_ANNOUNCEMENTS = 6
_COLLECT_DEADLINE_S = 90.0

_DELAY_RE = re.compile(r"Reconnecting in ([0-9.]+)s")

_SITECUSTOMIZE = f'''\
"""Emulate the Windows resolver for one hostname (test shim).

getaddrinfo fails with WSAHOST_NOT_FOUND (errno 11001, "getaddrinfo
failed") — exactly what a Windows laptop sees when the VPN that
resolves the server hostname is down.
"""
import socket

_real_getaddrinfo = socket.getaddrinfo


def _windows_dns_getaddrinfo(host, *args, **kwargs):
    if host == {_UNRESOLVABLE_HOST!r}:
        raise socket.gaierror(11001, "getaddrinfo failed")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _windows_dns_getaddrinfo
'''


def _announced_delays(log_path: Path) -> list[float]:
    """Parse every announced reconnect delay from the host log, in order.

    :param log_path: The host daemon's process log file.
    :returns: The delays, e.g. ``[0.5, 0.5, 0.5]`` on the buggy build.
    """
    if not log_path.exists():
        return []
    return [float(m) for m in _DELAY_RE.findall(log_path.read_text(errors="replace"))]


def test_host_dns_failure_reconnect_backs_off(tmp_path: Path) -> None:
    """A DNS-failing host tunnel must back off, not retry at 2 Hz forever."""
    from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

    inject_dir = tmp_path / "inject"
    inject_dir.mkdir()
    (inject_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE)

    host_log = tmp_path / "host-daemon.log"
    config_home = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_home.mkdir()
    data_dir.mkdir()

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(data_dir),
        PROCESS_LOG_FILE_ENV_VAR: str(host_log),
        # The sitecustomize shim must load in the child, and the child must
        # import this worktree's omnigent.
        "PYTHONPATH": os.pathsep.join(
            [str(inject_dir), str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    # A proxy would move DNS resolution to the proxy host, bypassing the
    # local resolver failure the report describes.
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(proxy_var, None)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "host",
            "--server",
            f"https://{_UNRESOLVABLE_HOST}",
            "--non-interactive",
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + _COLLECT_DEADLINE_S
        delays: list[float] = []
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            delays = _announced_delays(host_log)
            if len(delays) >= _MIN_ANNOUNCEMENTS:
                break
            time.sleep(0.5)

        delays = _announced_delays(host_log)
        # The daemon must still be retrying (a DNS outage is transient —
        # exiting would be a different bug), and it must have announced
        # enough reconnects to judge the cadence.
        assert proc.poll() is None, (
            f"host daemon exited (code {proc.returncode}) instead of retrying; "
            f"log: {host_log.read_text(errors='replace') if host_log.exists() else '<missing>'}"
        )
        assert len(delays) >= 3, (
            f"expected at least 3 reconnect announcements within "
            f"{_COLLECT_DEADLINE_S:.0f}s, got {len(delays)}; "
            f"log: {host_log.read_text(errors='replace') if host_log.exists() else '<missing>'}"
        )
        # The bug: every announced delay stays at the 0.5s base
        # ("recycle — prompt reconnect") — ~2 attempts/sec indefinitely.
        # A working backoff grows the announced delay past the base within
        # a few attempts (the handshake-timeout path already does).
        assert max(delays) > _RECONNECT_BASE_S, (
            f"reconnect interval never grew: {len(delays)} attempts all announced "
            f"{sorted(set(delays))}s — the DNS-failure path is retrying at the fixed "
            f"{_RECONNECT_BASE_S}s base with no backoff"
        )
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
