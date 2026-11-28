"""Host selection regressions for the daemon telemetry E2E journey."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.e2e.omnigent.test_host_daemon_telemetry_env_e2e import (
    _online_host_id,
    _telemetry_env,
)


def test_telemetry_env_isolates_inherited_host_identity(tmp_path: Path) -> None:
    inherited = {"OMNIGENT_HOST_ID": "a" * 32, "OMNIGENT_HOST_NAME": "shared-machine"}
    first = _telemetry_env(inherited, tmp_path / "first")
    second = _telemetry_env(inherited, tmp_path / "second")

    for key in ("OMNIGENT_HOST_ID", "OMNIGENT_HOST_NAME"):
        assert len({inherited[key], first[key], second[key]}) == 3


@pytest.mark.parametrize("initial_status", ["missing", "offline"])
def test_waits_for_own_host_while_another_host_is_online(initial_status: str) -> None:
    expected_host_id = "a" * 32
    unrelated = {"host_id": "b" * 32, "status": "online"}
    own_host_polls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal own_host_polls
        if request.url.path == "/v1/hosts":
            return httpx.Response(200, json={"hosts": [unrelated]})
        assert request.url.path == f"/v1/hosts/{expected_host_id}"
        own_host_polls += 1
        if own_host_polls == 1 and initial_status == "missing":
            return httpx.Response(404, json={"detail": "Host not found"})
        return httpx.Response(
            200,
            json={
                "host_id": expected_host_id,
                "status": "online" if own_host_polls > 1 else "offline",
            },
        )

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        selected = _online_host_id(client, host_id=expected_host_id, timeout=2)

    assert selected == expected_host_id
    assert own_host_polls == 2
