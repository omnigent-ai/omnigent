"""The host daemon must register when its redirected stdio is legacy cp1252.

``omnigent start`` redirects the daemon's stdout/stderr to its log file; on a
default-locale Windows machine those streams are cp1252/strict and cannot encode
the ``✓`` in the connect loop's banner. A ``sitecustomize`` on the daemon's
PYTHONPATH reproduces that stream shape on any OS.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Emulates Windows redirected cp1252 stdio at interpreter startup; see the
# shim's module docstring.
_CP1252_SITECUSTOMIZE_DIR = Path(__file__).parent / "_windows_cp1252_stdio"


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:]


def _banner_encode_crash(log_text: str) -> bool:
    """Report the ✓-banner cp1252 encode failure in the daemon log.

    ``str(UnicodeEncodeError)`` renders the glyph ascii-escaped, so the log
    carries the literal ``'charmap' codec can't encode character '\\u2713'``.
    """
    return "charmap" in log_text and "\\u2713" in log_text


@pytest.mark.timeout(180)
def test_host_daemon_registers_under_cp1252_stdio(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A daemon spawned with cp1252 redirected stdio registers and stays online.

    Spawns the real ``_daemon_entry`` the way ``omnigent start`` does — argv
    ``-P -m omnigent.host._daemon_entry --server <url>`` with stdout/stderr to
    a log file — but with the cp1252 ``sitecustomize`` on ``PYTHONPATH``.
    """
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-cp1252-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )

    daemon_log = tmp_path / "host-daemon.log"
    # ``-P`` keeps the cwd off sys.path, so the worktree under test must come
    # from PYTHONPATH (after the sitecustomize dir) or the daemon would import
    # whichever omnigent the venv has installed.
    pythonpath = os.pathsep.join(
        p
        for p in (
            str(_CP1252_SITECUSTOMIZE_DIR),
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        )
        if p
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OMNIGENT_CONFIG_HOME": str(omni_dir),
        "OMNIGENT_DATA_DIR": str(tmp_path / "omnigent-data"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
        "PYTHONPATH": pythonpath,
    }
    # The documented workaround (PYTHONUTF8=1 / PYTHONIOENCODING) would mask the
    # bug — strip any ambient value so the daemon sees raw cp1252 streams.
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)

    def _host_online() -> bool:
        try:
            resp = http_client.get("/v1/hosts")
        except httpx.HTTPError:
            return False
        if resp.status_code != 200:
            return False
        return any(
            host.get("host_id") == host_id and host.get("status") == "online"
            for host in resp.json().get("hosts", [])
        )

    with open(daemon_log, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
    try:
        online = False
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            log_text = daemon_log.read_text(encoding="latin-1")
            if _banner_encode_crash(log_text):
                break
            if _host_online():
                online = True
            if online and " Connected as " in log_text:
                break
            time.sleep(0.25)

        log_text = daemon_log.read_text(encoding="latin-1")
        banner_logged = " Connected as " in log_text
        assert proc.poll() is None, (
            "the host daemon process exited in Windows degraded mode:\n" + _tail(log_text)
        )
        assert not _banner_encode_crash(log_text), (
            "the host daemon crashed encoding its '✓ Connected' banner on a "
            "cp1252 (legacy Windows codepage) stdout, so the tunnel "
            "reconnect-loops on the crash:\n" + _tail(log_text)
        )
        assert online and _host_online(), (
            "the host daemon never registered / did not stay online in "
            "Windows degraded mode:\n" + _tail(log_text)
        )
        assert banner_logged, (
            "the host registered but never logged its ' Connected as ' banner, "
            "so the cp1252 write that used to crash was not exercised:\n" + _tail(log_text)
        )
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
