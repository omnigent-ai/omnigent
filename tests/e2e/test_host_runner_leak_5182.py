"""End-to-end repro for #5182: host runners leak on session relaunch.

Every relaunch rotates the session's runner binding
(``replace_runner_id``) and asks the host for a fresh runner, but
nothing ever stops the runner it just displaced. The superseded
process idles on the host forever (tunnel still authenticating,
transcript forwarder still tailing the session, native pane resident),
so a relaunch storm accumulates one live runner per attempt.

This drives the real stack (server subprocess, host daemon, real
``omnigent.runner._entry`` subprocesses) through one relaunch and
counts the runner processes the host is left holding::

    .venv/bin/python -m pytest tests/e2e/test_host_runner_leak_5182.py -v

Relaunch is triggered the way #5182's own repro sketch does: ``SIGSTOP``
the bound runner so its process stays alive while its tunnel goes
silent. The server declares the tunnel dead on the ping-miss threshold,
and the next message takes the relaunch branch, with the session still
bound to the stopped runner, exactly as in the incident.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    POLL_INTERVAL_S,
    configure_mock_llm,
    lookup_agent_id,
    upload_agent,
)
from tests.e2e.test_host_e2e import (
    _pid_alive,
    _spawn_host_daemon,
    _wait_for_host_online,
    _write_smoke_agent_yaml,
)

# The server declares a runner tunnel dead after PING_MISS_THRESHOLD (3)
# missed PING_INTERVAL_S (30s) ticks, so a stopped runner needs ~90s to
# fall out of the registry. Budget past that plus scheduling slack.
_TUNNEL_DEATH_BUDGET_S = 150.0


def _launches(log_path: Path) -> list[tuple[str, int]]:
    """Parse every runner the host daemon spawned, in launch order.

    The daemon logs ``Launched runner <id> for workspace <ws> (pid=NNNN)``
    once per spawn, so the line count is the generation count.

    :param log_path: Path to the captured daemon stderr log.
    :returns: ``(runner_id, pid)`` pairs, oldest first.
    """
    if not log_path.exists():
        return []
    return [
        (rid, int(pid))
        for rid, pid in re.findall(
            r"Launched runner (\S+) for workspace .*?\(pid=(\d+)\)",
            log_path.read_text(),
        )
    ]


def _reap_log_lines(log_path: Path, runner_id: str) -> list[str]:
    """Return the host daemon's stop lines naming a runner.

    Distinguishes "the fix terminated it" from "it happened to exit":
    ``_handle_stop`` logs ``Stopped runner <id>`` (the server's explicit
    ``host.stop_runner``) and the host's own supersession logs
    ``Stopped superseded runner <id> for session <s>``.

    :param log_path: Path to the captured daemon stderr log.
    :param runner_id: The runner id expected in the stop line.
    :returns: Matching log lines, in order.
    """
    if not log_path.exists():
        return []
    return [
        line.strip()
        for line in log_path.read_text().splitlines()
        if "Stopped" in line and runner_id in line
    ]


def _wait_for(predicate, *, timeout: float, what: str):  # type: ignore[no-untyped-def]
    """Poll ``predicate`` until it returns a truthy value.

    :param predicate: Zero-arg callable polled every
        :data:`POLL_INTERVAL_S`.
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
    :param runner_id: Runner id to probe.
    :returns: The endpoint's ``online`` verdict (``False`` on any error).
    """
    try:
        resp = client.get(f"/v1/runners/{runner_id}/status")
    except httpx.HTTPError:
        return False
    return bool(resp.status_code == 200 and resp.json().get("online"))


@pytest.mark.timeout(600)
def test_relaunch_reaps_the_superseded_runner(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A session relaunch must not leave its previous runner running.

    Journey: host-bound session → runner gen1 spawns and connects →
    ``SIGSTOP`` gen1 so its tunnel dies while its process lives → the
    server declares the runner offline → a message relaunches → gen2
    spawns.

    The assertion is on the host's process table, not on frames: after
    the relaunch the session must be down to exactly one live runner.
    Before the fix gen1 is still alive (the #5182 leak: one leaked
    generation per relaunch); after it, gen1 has been terminated.
    """
    configure_mock_llm(mock_llm_server_url, [{"text": "RELAUNCH_OK"}])

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    stopped_pid: int | None = None
    try:
        _wait_for_host_online(http_client, daemon.host_id, timeout=30.0)

        agent_name = upload_agent(http_client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(http_client, agent_name)
        workspace = tmp_path / "project"
        workspace.mkdir()

        # Host-bound create launches gen1 inline and binds it as the
        # session's runner_id, the state a relaunch supersedes.
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
            what="the host daemon to log gen1's launch",
        )
        _wait_for(
            lambda: _runner_online(http_client, gen1_id),
            timeout=90.0,
            what=f"gen1 runner {gen1_id} to connect its tunnel",
        )
        assert _pid_alive(gen1_pid), f"gen1 (pid={gen1_pid}) died before it was superseded"

        # Silence gen1's tunnel without killing it: the process stays
        # alive (leakable) while the server's ping loop stops seeing
        # frames and eventually closes the tunnel.
        os.kill(gen1_pid, signal.SIGSTOP)
        stopped_pid = gen1_pid
        _wait_for(
            lambda: not _runner_online(http_client, gen1_id),
            timeout=_TUNNEL_DEATH_BUDGET_S,
            what=f"the server to declare gen1 {gen1_id} offline",
        )

        # Runner offline + host online + session still bound to gen1 →
        # this message takes the relaunch branch.
        http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "relaunch me"}],
                },
            },
            timeout=120.0,
        )

        gen2_id, gen2_pid = _wait_for(
            lambda: next(
                ((r, p) for r, p in _launches(daemon.daemon_log) if p != gen1_pid),
                None,
            ),
            timeout=120.0,
            what="the relaunch to spawn gen2 on the host",
        )
        assert _pid_alive(gen2_pid), f"gen2 (pid={gen2_pid}) died right after launch"

        # The fix stops the superseded runner in the background, so wait for
        # both halves of the evidence: the process going down, and the host
        # logging the stop that took it down. They are not simultaneous:
        # _stop_runner_proc gives SIGTERM a 5s grace before SIGKILL, and for
        # a zygote-forked runner the exit is only observed through a control
        # round-trip (serialized, 30s timeout) that runs after the kill.
        reaped = False
        reap_lines: list[str] = []
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            reaped = reaped or not _pid_alive(gen1_pid)
            reap_lines = _reap_log_lines(daemon.daemon_log, gen1_id)
            if reaped and reap_lines:
                break
            time.sleep(POLL_INTERVAL_S)

        live = [(r, p) for r, p in _launches(daemon.daemon_log) if _pid_alive(p)]
        assert reaped, (
            f"#5182: the relaunch superseded gen1 ({gen1_id}, pid={gen1_pid}) but left its "
            f"process running. {len(live)} live runners for one session: {live}"
        )
        assert live == [(gen2_id, gen2_pid)], (
            f"expected only gen2 alive after the relaunch, got {live}"
        )
        # gen1 must be gone *because* it was reaped, not by coincidence.
        assert reap_lines, (
            f"gen1 ({gen1_id}) exited but the host logged no stop for it: "
            "the process died on its own, so this run proves nothing about reaping"
        )
    finally:
        # A still-stopped runner can't act on SIGTERM; wake it so the
        # host's shutdown cascade (or the assertion above) isn't racing
        # a process frozen in the middle of teardown.
        if stopped_pid is not None and _pid_alive(stopped_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(stopped_pid, signal.SIGCONT)
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait()
