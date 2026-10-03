"""E2E: a corrupt local Claude transcript skipped during import must not log at ERROR.

``omnigent host --server <url>`` imports recent Claude sessions (the request
Settings > Import sessions sends) from a ``~/.claude`` holding one readable
transcript and one nested past the JSON scanner's recursion limit. The corrupt
one is skipped and counted; that recovered skip must log below ERROR, since an
ERROR erupts as a traceback in the host terminal and counts against the
session-reliability KPI.

Runs against the mock LLM server::

    .venv/bin/python -m pytest \\
        tests/e2e/test_import_local_recovered_skip_not_error_logged_e2e.py -v
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S

_GOOD_SESSION_ID = "6f0d9f4e-2f0f-4bde-9b6e-2f6a0f6c1a01"
_CORRUPT_SESSION_ID = "bad0bad0-c0de-4bad-9dad-badbadbadbad"
# Past CPython's JSON scanner recursion limit (RecursionError from ~10k), with margin.
_NESTING_DEPTH = 20_000
_SKIP_MESSAGE = "import_local: skipping session"


def _seed_claude_transcripts(home: Path) -> None:
    """Write one readable and one corrupt Claude transcript under ``home/.claude``."""
    workspace = home / "repo"
    workspace.mkdir()
    project_dir = home / ".claude" / "projects" / "-repo"
    project_dir.mkdir(parents=True)
    user_record = {
        "type": "user",
        "uuid": "user-1",
        "timestamp": "2026-09-09T20:00:00.000Z",
        "cwd": str(workspace),
        "message": {"role": "user", "content": "hello from a healthy transcript"},
    }
    good = project_dir / f"{_GOOD_SESSION_ID}.jsonl"
    good.write_text(json.dumps(user_record) + "\n", encoding="utf-8")
    nested = "[" * _NESTING_DEPTH + '"x"' + "]" * _NESTING_DEPTH
    corrupt_record = (
        '{"type": "user", "uuid": "user-2", "cwd": '
        + json.dumps(str(workspace))
        + ', "message": {"role": "user", "content": '
        + nested
        + "}}"
    )
    corrupt = project_dir / f"{_CORRUPT_SESSION_ID}.jsonl"
    corrupt.write_text(json.dumps(user_record) + "\n" + corrupt_record + "\n", encoding="utf-8")
    now = time.time()
    os.utime(good, (now - 60, now - 60))
    os.utime(corrupt, (now, now))


def _start_host(*, home: Path, live_server: str) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Run ``omnigent host --server`` with *home* as its HOME.

    :returns: ``(process, host_id, host_log_path)``.
    """
    host_id = uuid.uuid4().hex
    host_log = home / "host.log"
    env = apply_runner_env(
        {
            **os.environ,
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
            "OMNIGENT_DATA_DIR": str(home / "omnigent-data"),
            "OMNIGENT_HOST_ID": host_id,
            "OMNIGENT_HOST_NAME": f"e2e-import-host-{host_id[:12]}",
            PROCESS_LOG_FILE_ENV_VAR: str(host_log),
        }
    )
    with open(home / "host-stderr.log", "w") as stderr_fh:
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent",
                "host",
                "--server",
                live_server,
                "--no-open",
                "--non-interactive",
            ],
            env=env,
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=stderr_fh,
        )
    return proc, host_id, host_log


def _wait_for_host_online(client: httpx.Client, host_id: str, home: Path, timeout: float) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* shows online.

    :raises AssertionError: If the host never appears online within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    stderr_text = (home / "host-stderr.log").read_text(encoding="utf-8", errors="replace")
    raise AssertionError(
        f"Host {host_id!r} did not appear online within {timeout}s.\n--- stderr ---\n{stderr_text}"
    )


def _read_host_log(log_path: Path, timeout: float = 10.0) -> str:
    """Return the host log once it mentions the corrupt session, else after *timeout*."""
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        if _CORRUPT_SESSION_ID in text:
            break
        time.sleep(POLL_INTERVAL_S)
    return text


def test_import_local_corrupt_claude_skip_is_not_error_logged(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A recovered per-session import skip must not produce an ERROR-level record.

    The batch must still recover (readable imported, corrupt counted with a
    reason) while the host log names the skip and its cause below ERROR.
    """
    home = tmp_path / "home"
    home.mkdir()
    _seed_claude_transcripts(home)

    proc, host_id, host_log = _start_host(home=home, live_server=live_server)
    try:
        _wait_for_host_online(http_client, host_id, home, timeout=60.0)

        resp = http_client.post(
            "/v1/imports/local/stream",
            json={"host_id": host_id, "source": "claude", "limit": 25},
            timeout=120.0,
        )
        assert resp.status_code == 200, resp.text
        events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]

        done_events = [e for e in events if e.get("event") == "done"]
        assert len(done_events) == 1, events
        done = done_events[0]
        assert (done["imported"], done["already_imported"], done["failed"]) == (1, 0, 1), events
        assert [f["external_session_id"] for f in done["failures"]] == [_CORRUPT_SESSION_ID], (
            events
        )
        assert len([e for e in events if e.get("event") == "session"]) == 1, events

        log_text = _read_host_log(host_log)
        skip_lines = [
            line
            for line in log_text.splitlines()
            if _SKIP_MESSAGE in line or _CORRUPT_SESSION_ID in line
        ]
        error_skip_lines = [line for line in skip_lines if re.match(r"^ERROR\b", line)]
        assert not error_skip_lines, (
            "The recovered import skip was recorded at ERROR level (erupts as a "
            "traceback in the `omnigent host` terminal and counts against session "
            "reliability):\n" + "\n".join(error_skip_lines)
        )
        # The skip is still diagnosed, and via the unexpected-exception path this
        # corruption is meant to exercise (RecursionError, not the loader's
        # ValueError net).
        assert any(
            _SKIP_MESSAGE in line and _CORRUPT_SESSION_ID in line and "RecursionError" in line
            for line in skip_lines
        ), log_text
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
