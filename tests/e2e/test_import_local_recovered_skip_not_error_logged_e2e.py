"""E2E: an unimportable local Claude transcript must not ERROR-log during import.

Journey: a machine holds local Claude Code transcripts, one of
which is corrupt in a way the loader does not expect (pathologically nested
JSON makes ``json.loads`` raise ``RecursionError``, which escapes the
``(SessionImportNotFoundError, OSError, ValueError, TypeError)`` net in the
host's ``_load``). The user brings the machine online as a host and imports
recent Claude sessions. The import itself behaves as designed — the readable
session lands, the corrupt one is skipped and counted on the done frame — but
the host records the expected, recovered per-session skip at ERROR level with
a full traceback (``import_local: skipping session source='claude' id=...``).

That ERROR record is what a user sees erupt in their ``omnigent host``
terminal, and on dev builds it ships to the debug-log telemetry where the
session-reliability KPI counts every in-scope post-connect ERROR as a
mid-session error attempt. An expected skip that the batch fully recovers
from must surface as a lower-severity diagnostic, not an ERROR.

Runs against the mock LLM server — no real credentials needed::

    .venv/bin/python -m pytest \
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
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S

# Deep enough that the C JSON scanner raises RecursionError (observed from
# ~10k on CPython 3.12) with 2x margin, small enough to stay a tiny file.
_NESTING_DEPTH = 20_000

_GOOD_SESSION_ID = "6f0d9f4e-2f0f-4bde-9b6e-2f6a0f6c1a01"
_CORRUPT_SESSION_ID = "bad0bad0-c0de-4bad-9dad-badbadbadbad"


def _seed_claude_home(claude_home: Path, workspace: Path) -> None:
    """Write one readable and one corrupt Claude transcript under *claude_home*.

    :param claude_home: Directory used as ``CLAUDE_CONFIG_DIR`` for the host.
    :param workspace: Existing directory recorded as the transcripts' cwd.
    """
    project_dir = claude_home / "projects" / "-repo"
    project_dir.mkdir(parents=True)

    good_record = {
        "type": "user",
        "uuid": "good-user-1",
        "timestamp": "2026-09-09T20:00:00.000Z",
        "cwd": str(workspace),
        "message": {"role": "user", "content": "hello from a healthy transcript"},
    }
    (project_dir / f"{_GOOD_SESSION_ID}.jsonl").write_text(
        json.dumps(good_record) + "\n", encoding="utf-8"
    )

    # Real on-disk corruption: a record whose content is nested past the JSON
    # scanner's recursion limit. json.loads raises RecursionError — not a
    # JSONDecodeError — so the failure escapes the loader's expected-error
    # handling and exercises the host's unexpected-exception skip path.
    corrupt_record = (
        '{"type": "user", "uuid": "corrupt-user-1", "cwd": "'
        + str(workspace)
        + '", "message": {"role": "user", "content": '
        + "[" * _NESTING_DEPTH
        + '"x"'
        + "]" * _NESTING_DEPTH
        + "}}"
    )
    corrupt_path = project_dir / f"{_CORRUPT_SESSION_ID}.jsonl"
    corrupt_path.write_text(
        json.dumps(good_record) + "\n" + corrupt_record + "\n", encoding="utf-8"
    )


def _spawn_import_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
    claude_home: Path,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn an isolated host daemon whose Claude home is the seeded fixture.

    Mirrors the host e2e daemon spawn: a unique ``(host_id, name)`` pre-seeded
    into ``config.yaml`` (the host store enforces a unique owner/name row on
    the shared session-scoped server), stderr and process logs captured to one
    file the test can assert on.

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param live_server: Server URL the daemon registers with.
    :param claude_home: Directory exported as ``CLAUDE_CONFIG_DIR``.
    :returns: ``(process, host_id, daemon_log_path)``.
    """
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-import-host-{uuid.uuid4().hex[:12]}"
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
        "CLAUDE_CONFIG_DIR": str(claude_home),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                live_server,
            ],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float = 30.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* shows online.

    :param client: HTTP client pointed at the server.
    :param host_id: Host ID to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the host never appears online.
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
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


def _wait_for_log_text(log_path: Path, needle: str, timeout: float = 15.0) -> str:
    """Poll *log_path* until *needle* appears, returning the full log text.

    :param log_path: The captured daemon log file.
    :param needle: Substring that marks the awaited record.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the needle never appears.
    """
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        if needle in text:
            return text
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Daemon log never contained {needle!r} within {timeout}s.\n--- log ---\n{text}"
    )


def test_import_local_corrupt_claude_skip_is_not_error_logged(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A recovered per-session import skip must not produce an ERROR-level record.

    Drives the real user journey: bring a host online whose Claude home holds
    one readable and one corrupt transcript, import recent Claude sessions via
    ``POST /v1/imports/local/stream`` (what Settings › Import sessions calls),
    and verify the batch recovers (readable imported, corrupt counted as
    failed). The regression assertion: the host's log — mirrored to the
    ``omnigent host`` terminal and shipped to debug-log telemetry, where the
    session-reliability KPI counts post-connect ERROR records — carries no
    ERROR-level record for the expected skip.
    """
    claude_home = tmp_path / "claude-home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _seed_claude_home(claude_home, workspace)

    proc, host_id, daemon_log = _spawn_import_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        claude_home=claude_home,
    )
    try:
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        resp = http_client.post(
            "/v1/imports/local/stream",
            json={"host_id": host_id, "source": "claude", "limit": 25},
            timeout=120.0,
        )
        assert resp.status_code == 200, resp.text
        events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]

        # The batch recovers: the readable session lands, the corrupt one is
        # skipped and counted. This part is correct behavior and must hold
        # before and after any logging fix.
        done_events = [e for e in events if e.get("event") == "done"]
        assert len(done_events) == 1, events
        assert done_events[0]["imported"] == 1, events
        assert done_events[0]["failed"] == 1, events
        session_events = [e for e in events if e.get("event") == "session"]
        assert len(session_events) == 1, events

        # The host diagnosed the skip (the unexpected-exception path ran).
        log_text = _wait_for_log_text(daemon_log, "import_local: skipping session")
        assert _CORRUPT_SESSION_ID in log_text

        # Regression guard: the expected, recovered skip must not be recorded
        # at ERROR level. An ERROR here erupts as a traceback in the user's
        # `omnigent host` terminal and is counted by the session-reliability
        # KPI as a mid-session error attempt, misattributing a benign local
        # transcript problem to Omnigent session reliability.
        error_skip_lines = [
            line
            for line in log_text.splitlines()
            if re.match(r"^ERROR\b", line)
            and ("import_local" in line or _CORRUPT_SESSION_ID in line)
        ]
        assert not error_skip_lines, (
            "Expected the recovered import skip to log below ERROR level, but the "
            "host recorded it as ERROR (counted against session reliability):\n"
            + "\n".join(error_skip_lines)
        )
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
