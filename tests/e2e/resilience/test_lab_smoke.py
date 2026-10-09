"""The resilience lab boots a real claude-native topology and its controls work.

Each test drives a real server, host daemon (or bare runner), Claude Code TUI
and mock model, with every network link behind a FaultProxy. These check the
lab itself; scenario scripts assert the product's behavior under faults.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import psutil
import pytest

from tests.e2e.resilience.lab.lab import Lab, LabMode

_TURN_TIMEOUT_S = 120.0


def _round_trip(lab: Lab, session_id: str) -> str:
    marker = f"LAB_{uuid.uuid4().hex[:10].upper()}"
    lab.script_turn(marker, [{"text": f"reply {marker}"}])
    response = lab.send_message(session_id, f"Reply with {marker}")
    assert response.status_code < 400, response.text
    lab.wait_for_text(session_id, f"reply {marker}", timeout=_TURN_TIMEOUT_S)
    return marker


@pytest.mark.parametrize("mode", ["host", "runner"])
def test_claude_turn_crosses_every_proxied_link(
    lab_factory: Callable[..., Lab], mode: LabMode
) -> None:
    lab = lab_factory(mode)
    session_id = lab.create_claude_session()
    _round_trip(lab, session_id)

    host_tags = {conn.tag for conn in lab.proxies.host.connections()}
    expected = {"runner.tunnel", "host.tunnel"} if mode == "host" else {"runner.tunnel"}
    assert expected <= host_tags
    for proxy in ("client", "host", "model"):
        assert lab.events.events(source=f"proxy:{proxy}", kind="connect"), proxy
    assert lab.model is not None
    assert any(req.get("model") for req in lab.model.requests())


def test_runner_tunnel_reset_reconnects(lab_factory: Callable[..., Lab]) -> None:
    lab = lab_factory()
    session_id = lab.create_claude_session()
    _round_trip(lab, session_id)

    before = time.time()
    assert lab.proxies.host.reset({"runner.tunnel"}) == 1
    lab.proxies.host.wait_for_connection({"runner.tunnel"}, opened_after=before, timeout=60)
    _round_trip(lab, session_id)


@pytest.mark.parametrize("front", ["ingress", "direct"])
def test_server_restart_reconnects_host_and_runner(
    lab_factory: Callable[..., Lab], front: str
) -> None:
    lab = lab_factory(front=front)
    session_id = lab.create_claude_session()
    _round_trip(lab, session_id)

    before = time.time()
    lab.restart_server(downtime_s=3.0)
    for tag in ("host.tunnel", "runner.tunnel"):
        lab.proxies.host.wait_for_connection({tag}, opened_after=before, timeout=90)
    if front == "ingress":
        assert lab.events.events(source="proxy:host", kind="upstream_unreachable")
    _round_trip(lab, session_id)


def test_sleep_host_freezes_and_resumes_the_machine(lab_factory: Callable[..., Lab]) -> None:
    lab = lab_factory()
    session_id = lab.create_claude_session()
    _round_trip(lab, session_id)

    with lab.sleep_host():
        runners = lab.runner_processes()
        assert runners
        assert all(proc.status() == psutil.STATUS_STOPPED for proc in runners)
        time.sleep(3.0)
    assert all(proc.status() != psutil.STATUS_STOPPED for proc in lab.runner_processes())
    _round_trip(lab, session_id)


def test_kill_runner_ends_its_tunnel(lab_factory: Callable[..., Lab]) -> None:
    lab = lab_factory()
    session_id = lab.create_claude_session()
    _round_trip(lab, session_id)

    killed = lab.kill_runner()
    assert killed
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and (
        any(psutil.pid_exists(pid) for pid in killed)
        or lab.proxies.host.connections({"runner.tunnel"})
    ):
        time.sleep(0.25)
    assert not lab.proxies.host.connections({"runner.tunnel"})
