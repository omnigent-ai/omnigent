"""E2E regression: server-to-runner requests must honor their httpx timeouts.

Every request the server sends to a runner travels over the runner's
WebSocket tunnel. A runner that is online but not answering (process
wedged, host stalled) must not hold such a request past the timeout its
call site configured — the caller passed ``timeout=5.0`` precisely to
bound this wait. This test drives the real user journey end-to-end
against actual server + runner subprocesses:

1. Bring a runner online, create a session bound to it, and start a turn
   the mock LLM holds open — the "agent looks stuck" moment a user acts on.
2. Wedge the runner with ``SIGSTOP``: its tunnel stays registered (the
   server keeps reporting it online) but it answers nothing, exactly like a
   hung runner process in production.
3. Facet 1 — the user clicks Stop. ``POST /v1/sessions/{id}/events``
   (``interrupt``) forwards to the runner with ``timeout=5.0``, so the
   user's request must come back on that order of magnitude instead of
   hanging until the tunnel's ping liveness (90s+) finally drops it.
4. Facet 2 — the runner-stream relay reads with ``read=45.0`` to detect a
   dead stream after three missed 15s heartbeats. The relay must notice
   the dead stream within that window (its "runner transport lost" retry
   handling), not only when the tunnel drops.

Runs against the mock LLM server — no real credentials needed::

    .venv/bin/python -m pytest tests/e2e/test_ws_tunnel_wedged_runner_timeouts.py -v

The assertions encode the CORRECT post-fix behavior: on the buggy build
both facets stall until the tunnel drops (~90-120s), so the test fails;
it passes once the tunnel transport honors httpx read timeouts.
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import token_bound_runner_id
from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    lookup_agent_id,
    send_user_message_to_session,
    set_fallback_mock_llm,
    upload_agent,
)
from tests.e2e.helpers import POLL_INTERVAL_S
from tests.e2e.test_host_e2e import _write_smoke_agent_yaml
from tests.e2e.test_orphaned_running_session_stop import (
    _runner_online,
    _spawn_runner,
    _spawn_server,
    _terminate,
)

# The interrupt forward passes timeout=5.0; the relay detects a dead stream
# at read=45.0. Bounds sit far above those targets and far below the ~90s+
# tunnel ping death that the buggy build waits for instead.
_INTERRUPT_RETURN_BOUND_S = 30.0
_RELAY_DETECT_BOUND_S = 70.0
# How long to keep measuring before giving up (past the ping-death window
# so the buggy build's actual durations land in the failure message).
_MEASURE_CEILING_S = 200.0

_RELAY_LOST_MARKER = "Relay: runner transport lost"


def _count_marker(log_path: Path, marker: str) -> int:
    """Count occurrences of *marker* in *log_path* (0 when unreadable).

    :param log_path: Log file of the server subprocess.
    :param marker: Substring to count, e.g. ``"Relay: runner transport lost"``.
    :returns: Occurrence count.
    """
    try:
        return log_path.read_text(errors="replace").count(marker)
    except OSError:
        return 0


@pytest.mark.timeout(360)
def test_wedged_runner_requests_honor_timeouts(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    """A wedged-but-online runner must not hold timed requests until tunnel death.

    Facet 1: the user's Stop (``interrupt`` event) forwards to the runner
    with ``timeout=5.0`` and must return within :data:`_INTERRUPT_RETURN_BOUND_S`.

    Facet 2: the runner-stream relay's ``read=45.0`` must surface the dead
    stream (its transport-lost retry handling) within
    :data:`_RELAY_DETECT_BOUND_S` of the runner wedging.
    """
    mock_llm_server_url = isolated_mock_llm_server_url
    tunnel_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(tunnel_token)
    db_path = tmp_path / "e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    server_log = logs / "server.log"

    server: subprocess.Popen[bytes] | None = None
    runner: subprocess.Popen[bytes] | None = None
    client: httpx.Client | None = None
    runner_stopped = False

    try:
        # ── 1. Boot server + bound runner on the mock LLM. ─────────────────
        server, base_url = _spawn_server(
            port=find_free_port(),
            db_path=db_path,
            artifact_dir=artifact_dir,
            log_path=server_log,
            config_home=config_home,
            mock_llm_server_url=mock_llm_server_url,
            tunnel_token=tunnel_token,
        )
        client = httpx.Client(base_url=base_url, timeout=30.0, trust_env=False)
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )

        runner = _spawn_runner(
            base_url=base_url,
            runner_id=runner_id,
            tunnel_token=tunnel_token,
            log_path=logs / "runner.log",
            config_home=config_home,
            mock_llm_server_url=mock_llm_server_url,
        )
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            if _runner_online(client, runner_id):
                break
            time.sleep(POLL_INTERVAL_S)
        assert _runner_online(client, runner_id), f"Runner {runner_id} never came online"

        # ── 2. Create a bound session and hold a turn in flight. ───────────
        agent_name = upload_agent(client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(client, agent_name)
        create = client.post("/v1/sessions", json={"agent_id": agent_id})
        create.raise_for_status()
        session_id = create.json()["id"]
        client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id}).raise_for_status()

        configure_mock_llm(mock_llm_server_url, [{"text": "TUNNEL_TIMEOUT_HOLD", "block": True}])
        send_user_message_to_session(
            client,
            session_id=session_id,
            content="Reply with the literal string TUNNEL_TIMEOUT_HOLD and nothing else.",
        )

        # The turn is genuinely in flight once the runner's LLM call is
        # parked on the mock gate — the moment a user sees a stuck agent.
        gate_deadline = time.monotonic() + 30.0
        gate_pending = False
        while time.monotonic() < gate_deadline:
            g = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0, trust_env=False)
            if g.status_code == 200 and g.json().get("pending") is True:
                gate_pending = True
                break
            time.sleep(POLL_INTERVAL_S)
        assert gate_pending, "The blocked turn never reached the mock LLM (no pending gate)"

        # The relay must be attached before the wedge, or facet 2 would
        # measure relay startup rather than dead-stream detection.
        relay_connect_deadline = time.monotonic() + 30.0
        while time.monotonic() < relay_connect_deadline:
            if _count_marker(server_log, "Relay: connected to runner GET /stream") > 0:
                break
            time.sleep(POLL_INTERVAL_S)
        assert _count_marker(server_log, "Relay: connected to runner GET /stream") > 0, (
            "Runner stream relay never connected; cannot measure dead-stream detection"
        )
        relay_lost_baseline = _count_marker(server_log, _RELAY_LOST_MARKER)

        # ── 3. Wedge the runner: online tunnel, no answers. ────────────────
        os.kill(runner.pid, signal.SIGSTOP)
        runner_stopped = True
        wedge_t0 = time.monotonic()

        # Facet 1 driver: the user's Stop click, exactly as the web UI sends
        # it. Measured from a thread so relay detection is observed in
        # parallel on the same wedge window.
        interrupt_elapsed: list[float | None] = [None]
        interrupt_response: list[object] = [None]

        def _post_interrupt() -> None:
            started = time.monotonic()
            try:
                with httpx.Client(base_url=base_url, timeout=240.0, trust_env=False) as ic:
                    resp = ic.post(
                        f"/v1/sessions/{session_id}/events",
                        json={"type": "interrupt", "data": {}},
                    )
                    interrupt_response[0] = (resp.status_code, resp.text)
            except httpx.HTTPError as exc:
                interrupt_response[0] = repr(exc)
            interrupt_elapsed[0] = time.monotonic() - started

        interrupt_thread = threading.Thread(target=_post_interrupt, daemon=True)
        interrupt_thread.start()

        # Facet 2 driver: watch the server log for the relay's transport-lost
        # handling. Wall time is measured by this poll loop, not by parsing
        # log timestamps.
        relay_lost_elapsed: float | None = None
        online_at_10s: bool | None = None
        while time.monotonic() - wedge_t0 < _MEASURE_CEILING_S:
            if (
                relay_lost_elapsed is None
                and _count_marker(server_log, _RELAY_LOST_MARKER) > relay_lost_baseline
            ):
                relay_lost_elapsed = time.monotonic() - wedge_t0
            if online_at_10s is None and time.monotonic() - wedge_t0 >= 10.0:
                online_at_10s = _runner_online(client, runner_id)
            if relay_lost_elapsed is not None and interrupt_elapsed[0] is not None:
                break
            time.sleep(POLL_INTERVAL_S)
        interrupt_thread.join(timeout=10.0)

        # Precondition, not a facet: the wedged runner's tunnel was still
        # registered while the request hung — "online but not answering".
        assert online_at_10s is True, (
            "Precondition failed: the wedged runner did not stay online, so the "
            "hang cannot be attributed to an online-but-unresponsive tunnel"
        )

        failures: list[str] = []
        if interrupt_elapsed[0] is None or interrupt_elapsed[0] >= _INTERRUPT_RETURN_BOUND_S:
            took = (
                f"{interrupt_elapsed[0]:.1f}s"
                if interrupt_elapsed[0] is not None
                else f"still pending after {_MEASURE_CEILING_S:.0f}s"
            )
            failures.append(
                "Facet 1 (interrupt forward, timeout=5.0): the user's Stop request "
                f"took {took} (response={interrupt_response[0]!r}); "
                f"expected < {_INTERRUPT_RETURN_BOUND_S:.0f}s"
            )
        if relay_lost_elapsed is None or relay_lost_elapsed >= _RELAY_DETECT_BOUND_S:
            took = (
                f"{relay_lost_elapsed:.1f}s"
                if relay_lost_elapsed is not None
                else f"not detected within {_MEASURE_CEILING_S:.0f}s"
            )
            failures.append(
                "Facet 2 (relay dead-stream detection, read=45.0): transport lost "
                f"surfaced after {took}; expected < {_RELAY_DETECT_BOUND_S:.0f}s"
            )
        assert not failures, (
            "WS tunnel transport ignored httpx timeouts on a wedged-but-online "
            "runner:\n  " + "\n  ".join(failures)
        )
    finally:
        if runner is not None and runner_stopped and runner.poll() is None:
            os.kill(runner.pid, signal.SIGCONT)
        try:
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0, trust_env=False)
        except httpx.HTTPError:
            pass
        if client is not None:
            client.close()
        _terminate(runner, timeout=10.0)
        _terminate(server, timeout=10.0)
