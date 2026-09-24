"""E2E: the background host daemon must survive legacy (cp1252) stdio.

On Windows the auto-launched host daemon (``python -m
omnigent.host._daemon_entry``, spawned by ``_ensure_host_daemon`` with
stdout/stderr redirected to its log file) gets stdio encoded with the ANSI
code page (cp1252). Its registration banner (``✓ Connected as …`` in
``omnigent/host/connect.py``) then raises ``UnicodeEncodeError: 'charmap'
codec can't encode character '\\u2713'``; the reconnect handler's ``⚠``
warning raises the same way, the daemon dies, and ``omnigent run`` /
``omnigent start`` report "The host daemon started but did not register with
the server within 30s".

The daemon is spawned here exactly as the product spawns it (module entry,
stdio redirected to a log file) with ``PYTHONIOENCODING=cp1252`` standing in
for the Windows charmap default. It must register, stay online, and never
crash on its own status glyphs.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_host_daemon_legacy_stdio.py -v
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

import omnigent
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

# The product's registration wait (_BACKGROUND_HOST_REGISTRATION_GRACE_S in
# omnigent/cli.py): the daemon must be online before the CLI would give up.
_REGISTRATION_WINDOW_S = 30.0
# The crash fires right at the moment registration succeeds, so a single
# online observation isn't enough — the daemon must also survive past it.
_STABILITY_WINDOW_S = 5.0


def _spawn_daemon_with_cp1252_stdio(
    *, tmp_path: Path, server_url: str, mock_llm_server_url: str
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn an isolated host daemon whose redirected stdio encodes as cp1252.

    Mirrors ``_spawn_host_daemon_process`` in ``omnigent/cli.py``: the module
    entry with stdin closed, stdout+stderr redirected to the daemon log file,
    and the process log routed to the same file. ``PYTHONIOENCODING=cp1252``
    reproduces what Windows does by default to a redirected stream (charmap
    encoding, strict errors).

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param server_url: Live e2e server URL the daemon registers with.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: ``(proc, host_id, daemon_log)``.
    """
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-charmap-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "PYTHONIOENCODING": "cp1252",
        # ``-P`` keeps the cwd off sys.path, so pin the checkout the test
        # process imports; otherwise a co-installed omnigent would resolve.
        "PYTHONPATH": str(Path(omnigent.__file__).resolve().parents[1]),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    with open(daemon_log, "wb") as log_fh:
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-P",
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                server_url,
            ],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _host_online(client: httpx.Client, host_id: str) -> bool:
    resp = client.get("/v1/hosts")
    if resp.status_code != 200:
        return False
    return any(
        h["host_id"] == host_id and h["status"] == "online" for h in resp.json().get("hosts", [])
    )


def _assert_daemon_healthy(proc: subprocess.Popen[bytes], daemon_log: Path) -> None:
    """Fail with the log tail when the daemon crashed or hit a charmap error."""
    log_text = daemon_log.read_text(encoding="utf-8", errors="replace")
    assert "UnicodeEncodeError" not in log_text, (
        "Host daemon raised UnicodeEncodeError on legacy (cp1252) stdio — its "
        "status glyphs must degrade instead of crashing the daemon. Log tail:\n" + log_text[-2000:]
    )
    rc = proc.poll()
    assert rc is None, f"Host daemon exited with code {rc}. Log tail:\n{log_text[-2000:]}"


@pytest.mark.timeout(180)
def test_host_daemon_survives_cp1252_stdio(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A daemon with charmap stdio must register and stay online.

    Journey (from the bug report): on Windows, ``omnigent run`` spawns the
    background host daemon with its stdio redirected to the host log, which
    Python encodes as cp1252. Expected: the daemon registers and keeps
    hosting (its decorative glyphs degrade at worst). Actual (bug): the
    ``✓ Connected`` banner crashes the tunnel, the ``⚠`` reconnect warning
    crashes the daemon, and registration never completes.
    """
    proc, host_id, daemon_log = _spawn_daemon_with_cp1252_stdio(
        tmp_path=tmp_path,
        server_url=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    try:
        deadline = time.monotonic() + _REGISTRATION_WINDOW_S
        while True:
            _assert_daemon_healthy(proc, daemon_log)
            if _host_online(http_client, host_id):
                break
            assert time.monotonic() < deadline, (
                "Host daemon did not register within "
                f"{_REGISTRATION_WINDOW_S:.0f}s. Log tail:\n"
                + daemon_log.read_text(encoding="utf-8", errors="replace")[-2000:]
            )
            time.sleep(POLL_INTERVAL_S)

        # Registration succeeded — now it must survive its own banner.
        stable_until = time.monotonic() + _STABILITY_WINDOW_S
        while time.monotonic() < stable_until:
            _assert_daemon_healthy(proc, daemon_log)
            time.sleep(POLL_INTERVAL_S)
        _assert_daemon_healthy(proc, daemon_log)
        assert _host_online(http_client, host_id), (
            "Host went offline right after registering. Log tail:\n"
            + daemon_log.read_text(encoding="utf-8", errors="replace")[-2000:]
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
