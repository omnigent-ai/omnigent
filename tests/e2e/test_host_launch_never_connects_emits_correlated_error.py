"""Check correlated launch diagnostics across a real server, host, and runner.

SIGSTOP freezes each launched runner before its tunnel connects. A correlated
ERROR must appear in the host, runner, or server logs within five minutes.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    upload_agent,
)
from tests.e2e.helpers import POLL_INTERVAL_S
from tests.e2e.test_host_e2e import (
    _pid_alive,
    _spawn_host_daemon,
    _wait_for_host_online,
    _write_smoke_agent_yaml,
)

# Correlate launch errors within five minutes, with slack for log polling.
_FUNNEL_ERROR_WINDOW_S = 300.0
_WINDOW_SLACK_S = 30.0

# Poll well below direct-spawn boot time so SIGSTOP lands pre-connect.
_WEDGE_POLL_S = 0.02

# Process-log files start ERROR records with the level name.
_ERROR_LINE = re.compile(r"^ERROR\b")

_LAUNCH_LINE = re.compile(r"Launched runner (\S+) for workspace .*?\(pid=(\d+)\)")


def _launches(log_path: Path) -> list[tuple[str, int]]:
    """Parse launched runner IDs and PIDs in order."""
    if not log_path.exists():
        return []
    return [
        (rid, int(pid)) for rid, pid in _LAUNCH_LINE.findall(log_path.read_text(errors="replace"))
    ]


def _wait_for(predicate, *, timeout: float, what: str):  # type: ignore[no-untyped-def]
    """Return the first truthy poll result or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _runner_online(client: httpx.Client, runner_id: str) -> bool:
    """Return the server's online verdict, or false on HTTP failure."""
    try:
        resp = client.get(f"/v1/runners/{runner_id}/status")
    except httpx.HTTPError:
        return False
    return bool(resp.status_code == 200 and resp.json().get("online"))


class _RunnerWedger:
    """SIGSTOP every launched runner, including message-path relaunches."""

    def __init__(self, daemon_log: Path) -> None:
        self._daemon_log = daemon_log
        self._stop = threading.Event()
        self.wedged: dict[str, int] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            for rid, pid in _launches(self._daemon_log):
                if rid in self.wedged:
                    continue
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGSTOP)
                self.wedged[rid] = pid
            time.sleep(_WEDGE_POLL_S)

    def close(self) -> None:
        """Stop the poller and SIGCONT everything it froze."""
        self._stop.set()
        self._thread.join(timeout=5.0)
        for pid in self.wedged.values():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGCONT)


def _correlated_error_lines(
    log_files: list[Path],
    correlation_keys: list[str],
) -> list[tuple[Path, str]]:
    """Find ERROR records naming a runner token or the session."""
    hits: list[tuple[Path, str]] = []
    for path in log_files:
        if not path.exists():
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if _ERROR_LINE.match(line) and any(key in line for key in correlation_keys):
                hits.append((path, line.strip()))
    return hits


def _funnel_log_files(daemon_log: Path, host_home: Path, basetemp: Path) -> list[Path]:
    """Collect host, runner, and server logs, host first."""
    files = [daemon_log]
    # Runner-log location depends on the host's runtime data dir.
    if host_home.exists():
        files.extend(
            sorted(p for p in host_home.rglob("*.log") if p.is_file() and p != daemon_log)
        )
    files.extend(sorted(basetemp.glob("e2e_logs*/server.log")))
    # De-dup while preserving host-first order.
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in files:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


@pytest.mark.timeout(900)
def test_never_connected_launch_emits_correlated_error(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A never-connected launch emits a correlated ERROR within five minutes."""
    configure_mock_llm(mock_llm_server_url, [{"text": "NEVER_REACHED"}])

    # Direct spawn gives SIGSTOP time to land before the tunnel dial.
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE", "0")

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    wedger: _RunnerWedger | None = None
    try:
        _wait_for_host_online(http_client, daemon.host_id, timeout=30.0)

        wedger = _RunnerWedger(daemon.daemon_log)

        agent_name = upload_agent(http_client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(http_client, agent_name)
        workspace = tmp_path / "project"
        workspace.mkdir()

        create = http_client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": daemon.host_id,
                "workspace": str(workspace),
            },
            timeout=90.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        gen1_id, gen1_pid = _wait_for(
            lambda: (_launches(daemon.daemon_log) or [None])[0],
            timeout=60.0,
            what="the host daemon to log the launch",
        )
        t_launch = time.monotonic()
        assert gen1_id.startswith("runner_token_"), (
            f"launch line carries an unexpected runner id shape: {gen1_id!r}"
        )
        _wait_for(
            lambda: gen1_id in (wedger.wedged if wedger else {}),
            timeout=30.0,
            what="the wedger to freeze the launched runner",
        )

        # Discard runs where SIGSTOP lost the pre-connect race.
        time.sleep(3.0)
        if _runner_online(http_client, gen1_id):
            pytest.fail(
                f"fault injection lost the race: runner {gen1_id} connected "
                "its tunnel before the SIGSTOP landed"
            )

        # The send relaunches; the wedger freezes that runner too.
        message = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello?"}],
                },
            },
            timeout=180.0,
        )
        message_queued = False
        if message.status_code < 400:
            payload = message.json() if message.content else {}
            message_queued = bool(isinstance(payload, dict) and payload.get("queued"))
        assert message.status_code >= 400 or message_queued, (
            "a message to a session whose runner never connected should not "
            f"be accepted as a live turn, got HTTP {message.status_code}: "
            f"{message.text[:500]}"
        )

        # Check every phase for a launch-correlated ERROR.
        deadline = t_launch + _FUNNEL_ERROR_WINDOW_S + _WINDOW_SLACK_S
        basetemp = tmp_path.parent
        hits: list[tuple[Path, str]] = []
        while time.monotonic() < deadline:
            correlation_keys = [session_id, *wedger.wedged.keys()]
            log_files = _funnel_log_files(daemon.daemon_log, tmp_path, basetemp)
            hits = _correlated_error_lines(log_files, correlation_keys)
            if hits:
                break
            if _runner_online(http_client, gen1_id):
                pytest.fail(
                    f"fault injection lost the race: runner {gen1_id} came online mid-window"
                )
            time.sleep(2.0)

        assert not _runner_online(http_client, gen1_id), (
            "precondition broken: the wedged runner connected its tunnel"
        )

        if not hits:
            log_files = _funnel_log_files(daemon.daemon_log, tmp_path, basetemp)
            all_error_lines = [
                (path, line.strip())
                for path in log_files
                if path.exists()
                for line in path.read_text(errors="replace").splitlines()
                if _ERROR_LINE.match(line)
            ]
            scanned = "\n".join(f"  {p}" for p in log_files)
            uncorrelated = "\n".join(f"  {p.name}: {line}" for p, line in all_error_lines) or (
                "  (none at all)"
            )
            pytest.fail(
                "the runner for session "
                f"{session_id} (launch {gen1_id}, pid={gen1_pid}) never "
                f"connected its tunnel, yet {_FUNNEL_ERROR_WINDOW_S:.0f}s "
                "after the launch NO ERROR-level log record correlated with "
                "the launch (runner token or session id) exists in any "
                "funnel phase. The session hung for the user with only a "
                f"generic failure (message POST → HTTP {message.status_code}) "
                "and operators have no correlated ERROR to find.\n"
                f"Scanned:\n{scanned}\n"
                f"ERROR-level lines present (none correlated):\n{uncorrelated}"
            )
    finally:
        if wedger is not None:
            wedger.close()
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait()
        # SIGSTOPped runners cannot respond to daemon shutdown.
        if wedger is not None:
            for pid in wedger.wedged.values():
                if _pid_alive(pid):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGKILL)
