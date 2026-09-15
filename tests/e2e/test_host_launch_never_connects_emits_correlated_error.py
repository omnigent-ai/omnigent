"""End-to-end guard: a launch that never connects must emit a correlated ERROR.

The creation funnel logs a launch on the host (``_handle_launch``'s
``Launched runner runner_token_<hex> ...`` line) and a connect on the
runner (``_serve_tunnel_once``). When the launched runner never
connects its tunnel, no phase of the funnel emits an ERROR-level log
record correlated with the launch (the ``runner_token_<hex>`` runner id
or the session id) within the triage window (launch + 5 minutes):

- the host has no connect-deadline watchdog at all — ``_watch_runner``
  only fires when the runner process *exits* (and then logs WARNING,
  not ERROR; a clean exit is deliberately silent),
- the server's create-time launch does not wait for the connect, and
  its message-path connect wait logs INFO then raises a generic
  ``RUNNER_UNAVAILABLE`` without any ERROR-level record.

So the user's session hangs with a generic failure, and operators get
zero correlated ERROR telemetry — the never-connected launch signature
the creation-funnel triage aggregates.

This test drives the real stack (server subprocess, host daemon, real
runner subprocess) through the journey:

1. bring a host online against the server,
2. create a session on that host (the host spawns a runner and reports
   "launched"),
3. wedge the runner before it dials its tunnel (``SIGSTOP`` — the fault
   injection standing in for whatever starves/hangs runners in the
   field: resource exhaustion, a blocked egress, a wedged boot),
4. message the stuck session like a user would (fails generically),
5. assert that, within the funnel's launch+5m correlation window, at
   least one ERROR-level log record correlated with the launch (runner
   token or session id) appears in the host daemon log, the runner
   logs, or the server log.

Before a fix lands, step 5 finds nothing — the test FAILS, reproducing
the bug. A fix that adds bounded lifecycle diagnostics (or repairs
readiness attribution) makes step 5 find the diagnostic and the test
passes as soon as it is emitted.

Run it directly (mock mode, no credentials needed)::

    .venv/bin/python -m pytest \
        tests/e2e/test_host_launch_never_connects_emits_correlated_error.py -v
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

# The funnel's correlation window: an ERROR only counts as evidence for a
# never-connected launch attempt when it lands within five minutes of the
# launch. A bounded lifecycle diagnostic must therefore fire inside this
# window; poll a little past it before declaring the funnel silent.
_FUNNEL_ERROR_WINDOW_S = 300.0
_WINDOW_SLACK_S = 30.0

# How often the wedger thread scans the daemon log for freshly spawned
# runner pids. Must be far below the runner's boot time (a direct-Popen
# runner pays multi-second interpreter + import startup before it dials
# the tunnel) so the SIGSTOP always lands pre-connect.
_WEDGE_POLL_S = 0.02

# Process-log lines are "LEVELNAME <timestamp> <source> <func> | <msg>",
# uncolored when writing to a file — an ERROR-level record starts the
# line with "ERROR". (The debug-log sink ships these same records with
# ``level = record.levelname``, so this is the local mirror of the
# ``omnigent_debug_logs.level = 'ERROR'`` correlation the triage ran.)
_ERROR_LINE = re.compile(r"^ERROR\b")

_LAUNCH_LINE = re.compile(r"Launched runner (\S+) for workspace .*?\(pid=(\d+)\)")


def _launches(log_path: Path) -> list[tuple[str, int]]:
    """Parse every runner the host daemon spawned, in launch order.

    :param log_path: Path to the captured daemon stderr/process log.
    :returns: ``(runner_id, pid)`` pairs, oldest first.
    """
    if not log_path.exists():
        return []
    return [
        (rid, int(pid)) for rid, pid in _LAUNCH_LINE.findall(log_path.read_text(errors="replace"))
    ]


def _wait_for(predicate, *, timeout: float, what: str):  # type: ignore[no-untyped-def]
    """Poll ``predicate`` until it returns a truthy value.

    :param predicate: Zero-arg callable polled every :data:`POLL_INTERVAL_S`.
    :param timeout: Maximum seconds to wait.
    :param what: Description used in the failure message.
    :returns: The first truthy value the predicate returned.
    :raises AssertionError: If the predicate never went truthy.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _runner_online(client: httpx.Client, runner_id: str) -> bool:
    """Return whether the server holds an open tunnel for a runner.

    :param client: HTTP client pointed at the server.
    :param runner_id: Runner id to probe, e.g. ``"runner_token_0123..."``.
    :returns: The endpoint's ``online`` verdict (``False`` on any error).
    """
    try:
        resp = client.get(f"/v1/runners/{runner_id}/status")
    except httpx.HTTPError:
        return False
    return bool(resp.status_code == 200 and resp.json().get("online"))


class _RunnerWedger:
    """SIGSTOPs every runner the host daemon spawns, before it can connect.

    A daemon thread polls the daemon log for ``Launched runner ...
    (pid=NNNN)`` lines and stops each new pid the moment it appears —
    the persistent host-side condition the funnel aggregates (runners
    that consistently fail to come up), applied to every generation so
    a message-path relaunch cannot quietly heal the session.
    """

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
    """Find ERROR-level log lines correlated with the launch.

    Mirrors the operators' triage query: an ERROR record counts as
    evidence for a never-connected launch attempt when it names the
    launch's ``runner_token_<hex>`` runner id (the query's only
    correlation key for never-connected attempts) — accepting the
    session id too, so a fix that attributes its diagnostic by session
    instead of token also satisfies the guard.

    :param log_files: Log files to scan (host daemon, runners, server).
    :param correlation_keys: Runner token ids and the session id.
    :returns: ``(file, line)`` pairs for every correlated ERROR line.
    """
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
    """Collect every funnel phase's log file.

    :param daemon_log: The host daemon's captured stderr/process log.
    :param host_home: The daemon's ``HOME`` — runner process logs land
        under ``<home>/.omnigent/**``.
    :param basetemp: The pytest session basetemp — the shared
        ``live_server`` fixture writes ``e2e_logs*/server.log`` there.
    :returns: Existing log files, host first.
    """
    files = [daemon_log]
    # Runner process logs land under ``<home>/.omnigent/logs/runner/`` (the
    # host opens them via open_process_log_file → logs_root() → HOME); glob
    # the whole home so they are picked up wherever the runtime data dir
    # resolves them.
    if host_home.exists():
        files.extend(
            sorted(p for p in host_home.rglob("*.log") if p.is_file() and p != daemon_log)
        )
    files.extend(sorted(basetemp.glob("e2e_logs*/server.log")))
    # De-dup while preserving order (host first).
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
    """A launch whose runner never connects must emit a correlated ERROR.

    Journey: host online → create a session on the host (runner spawns,
    host reports "launched") → the runner wedges before dialing its
    tunnel → the user messages the stuck session and gets only a
    generic failure → within launch+5m, the funnel must contain at
    least one ERROR-level record correlated with the launch (runner
    token or session id). Before the fix there was none — the funnel was
    mute about never-connected launches — so this assertion failed.
    """
    configure_mock_llm(mock_llm_server_url, [{"text": "NEVER_REACHED"}])

    # Direct-Popen spawn (no zygote): the runner pays the full
    # interpreter + import boot before its tunnel dial, giving the
    # wedger a multi-second window; a zygote-forked runner is
    # pre-imported and could connect between wedger polls.
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE", "0")

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    wedger: _RunnerWedger | None = None
    try:
        _wait_for_host_online(http_client, daemon.host_id, timeout=30.0)

        # Arm the wedge before anything can launch a runner.
        wedger = _RunnerWedger(daemon.daemon_log)

        agent_name = upload_agent(http_client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(http_client, agent_name)
        workspace = tmp_path / "project"
        workspace.mkdir()

        # 1-2. The user creates a session on the host. The host spawns
        # the runner and answers "launched"; the create succeeds.
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

        # 3. Injection validity: the wedged runner must never connect.
        # (If it connected, the SIGSTOP lost the race and this run
        # proves nothing about the never-connect funnel.)
        time.sleep(3.0)
        if _runner_online(http_client, gen1_id):
            pytest.fail(
                f"fault injection lost the race: runner {gen1_id} connected "
                "its tunnel before the SIGSTOP landed"
            )

        # 4. The user acts on the stuck session. The message takes the
        # relaunch branch (which spawns another runner — the wedger
        # freezes that generation too, like a persistently failing
        # host), waits out the connect grace, and must NOT be accepted
        # as a normal live turn. Today it surfaces only a generic
        # RUNNER_UNAVAILABLE-style failure.
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

        # 5. The regression guard: within the funnel's launch+5m
        # correlation window, some phase (host daemon, runner, server)
        # must emit an ERROR-level record correlated with the launch.
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
        # A SIGSTOPped runner can't act on the daemon's shutdown
        # cascade; make sure nothing frozen outlives the test.
        if wedger is not None:
            for pid in wedger.wedged.values():
                if _pid_alive(pid):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGKILL)
