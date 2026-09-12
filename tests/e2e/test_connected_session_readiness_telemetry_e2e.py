"""Regression e2e: connected sessions must emit readiness or a correlated ERROR.

The launch-to-ready KPI funnel (see the ticket's Reproduce SQL) declares a
host-launched session "ready" only when one of three functions logs a
non-ERROR line, keyed to the session id, within five minutes of the runner
connect (``_serve_tunnel_once``):

    _auto_create_claude_terminal   # claude-native only, at create
    _ensure_native_terminal        # native-terminal only
    post_session_events            # fires only when a turn is posted

There is **no connect/bind-time readiness lifecycle emitter**. All three
proxies are either native-terminal-only or downstream of the first user turn.
So a session that connects on a non-native (SDK) agent and is *functionally
ready* (server status ``idle``) but has not yet received a turn emits a connect
record and nothing else: no readiness marker, no correlated ERROR. The KPI then
counts it as a failed launch ("connected, never ready, no error") even though
the launch succeeded.

This test drives the real funnel live -- a real host daemon, real server, real
runner subprocess -- to the "runner online" state for an ``openai-agents`` (SDK)
session, leaves it idle (no turn), and asserts the funnel *does* attribute a
readiness signal to the connected session. Today it does not, so the test fails
with the exact failure signature (connect recorded, zero readiness markers,
zero correlated ERRORs). A fix that emits a bounded readiness lifecycle
diagnostic at bind/serve time -- or otherwise repairs readiness attribution so
a connected, idle-ready session is observable as ready -- flips it green.

Runs against the mock LLM server; no real credentials needed::

    .venv/bin/python -m pytest \
        tests/e2e/test_connected_session_readiness_telemetry_e2e.py -v
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

import httpx

from tests.e2e.conftest import POLL_INTERVAL_S, lookup_agent_id, upload_agent
from tests.e2e.test_host_e2e import (
    _spawn_host_daemon,
    _wait_for_host_online,
    _write_smoke_agent_yaml,
)

# The KPI's readiness proxy: a non-ERROR line from one of these funcs within
# five minutes of connect marks the session "ready".
_KPI_READINESS_FUNCS = (
    "_auto_create_claude_terminal",
    "_ensure_native_terminal",
    "post_session_events",
)

# A fix may instead add a dedicated connect/bind-time readiness emitter. Accept
# an explicit readiness lifecycle marker too, so the guard is robust to the fix
# shape rather than pinned to the three legacy proxy funcs.
_READINESS_LIFECYCLE_HINT = re.compile(
    r"\b(session[_ -]?ready|readiness|ready to serve|serving session)\b",
    re.IGNORECASE,
)

# Locally a readiness marker (when one exists) lands within a second or two of
# connect -- in the happy path post_session_events fires ~0.4s after connect.
# A short idle window is ample and keeps the KPI's five-minute rule honest.
_READINESS_WINDOW_S = 12.0


def _readiness_signals_for_session(runner_log_text: str, session_id: str) -> list[str]:
    """Return runner funnel lines that would attribute readiness to *session_id*.

    Mirrors the KPI's readiness definition (a non-ERROR line from one of the
    three proxy funcs) and additionally accepts any explicit readiness
    lifecycle marker a fix might add. The runner log is the per-session process
    log the debug-log sink uploads to ``omnigent_debug_logs``; its ``func_name``
    column is exactly what the KPI keys on.

    :param runner_log_text: Concatenated runner log text for the session.
    :param session_id: The connected session's conversation id.
    :returns: Matching readiness lines (empty when the funnel emits none).
    """
    signals: list[str] = []
    for line in runner_log_text.splitlines():
        if "ERROR" in line:
            continue
        proxy_hit = any(func in line for func in _KPI_READINESS_FUNCS)
        lifecycle_hit = bool(_READINESS_LIFECYCLE_HINT.search(line))
        if not (proxy_hit or lifecycle_hit):
            continue
        # A lifecycle-hint line must name the session to count as attributed
        # readiness; the proxy funcs run in the session's own process log so
        # they are session-scoped by construction.
        if lifecycle_hit and not proxy_hit and session_id not in line:
            continue
        signals.append(line)
    return signals


def test_connected_session_emits_readiness_without_a_turn(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A host-launched session that connects and is idle-ready must be
    observable as ready in the launch-to-ready funnel, without a user turn.

    Reproduces the defect: the connect phase is recorded but no readiness marker
    (and no correlated ERROR) is attributed to the connected session, so a
    successful launch is invisible to the readiness KPI.
    """
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    host_proc = daemon.proc
    host_id = daemon.host_id

    try:
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        agent_id = lookup_agent_id(
            http_client,
            upload_agent(http_client, _write_smoke_agent_yaml(tmp_path)),
        )
        resp = http_client.post("/v1/sessions", json={"agent_id": agent_id})
        resp.raise_for_status()
        session_id = resp.json()["id"]

        launch_resp = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(tmp_path)},
            timeout=60.0,
        )
        assert launch_resp.status_code == 200, (
            f"Launch failed: {launch_resp.status_code} {launch_resp.text}"
        )
        runner_id = launch_resp.json()["runner_id"]

        # Wait for the runner to connect and come online (the connect phase).
        deadline = time.monotonic() + 30.0
        runner_online = False
        while time.monotonic() < deadline:
            status_resp = http_client.get(f"/v1/runners/{runner_id}/status")
            if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                runner_online = True
                break
            time.sleep(POLL_INTERVAL_S)
        assert runner_online, f"Runner {runner_id} never came online after launch"

        http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        ).raise_for_status()

        # The session is functionally READY: bound, runner online, no turn yet.
        # Crucially we do NOT post a message -- readiness must not depend on it.
        info = http_client.get(f"/v1/sessions/{session_id}").json()
        assert info.get("status") == "idle", (
            f"expected a connected, ready-idle session, got status={info.get('status')!r} "
            f"(error={info.get('error')!r})"
        )

        # Give any readiness signal ample time to land, then read the runner's
        # own funnel telemetry (the per-session process log the debug-log sink
        # uploads to omnigent_debug_logs).
        time.sleep(_READINESS_WINDOW_S)

        runner_log_dir = Path(os.environ["OMNIGENT_DATA_DIR"]) / "logs" / "runner"
        session_logs = sorted(runner_log_dir.glob(f"runner-{session_id}*.log"))
        assert session_logs, (
            f"no runner log for session {session_id} under {runner_log_dir} "
            f"(existing: {[p.name for p in runner_log_dir.glob('runner-*.log')]})"
        )
        log_text = "\n".join(p.read_text() for p in session_logs)

        connect_lines = [ln for ln in log_text.splitlines() if "_serve_tunnel_once" in ln]
        error_lines = [ln for ln in log_text.splitlines() if "ERROR" in ln]
        readiness_signals = _readiness_signals_for_session(log_text, session_id)

        # Precondition: we really reached the connect phase (the funnel's
        # denominator), so a missing readiness marker is a gap and not an
        # un-launched runner.
        assert connect_lines, (
            "expected a _serve_tunnel_once connect record for the session; "
            f"runner log:\n{log_text}"
        )

        # The launch succeeded silently: no correlated ERROR. This is what makes
        # the KPI signature 'no_ready_no_error' rather than a real crash.
        assert not error_lines, (
            "expected no correlated ERROR for a cleanly connected session, but found:\n"
            + "\n".join(error_lines)
        )

        # The defect: a connected, idle-ready session emits NO
        # readiness marker attributed to it (the readiness proxies are
        # turn-driven or native-terminal-only). This assertion fails today with
        # the exact ticket signature and passes once a fix makes a connected
        # session's readiness observable without a user turn.
        assert readiness_signals, (
            "session connected and is ready (status=idle) but the "
            "launch-to-ready funnel emitted no readiness marker for it and no "
            "correlated ERROR -- the connect phase is recorded via "
            "_serve_tunnel_once, yet none of "
            f"{_KPI_READINESS_FUNCS} (nor any readiness lifecycle marker) fired "
            f"within {_READINESS_WINDOW_S:.0f}s of connect. A successful launch "
            "is invisible to the readiness KPI.\n"
            f"connect_lines={len(connect_lines)} error_lines={len(error_lines)} "
            f"readiness_signals={len(readiness_signals)}\n"
            f"runner log:\n{log_text}"
        )

    finally:
        host_proc.send_signal(signal.SIGTERM)
        try:
            host_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host_proc.kill()
            host_proc.wait()
