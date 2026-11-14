"""Mocked readiness checks for native host startup helpers."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from tests._helpers.server_runner import ServerRunner
from tests.e2e.resilience.lab.lab import Lab, LabConfig


@pytest.mark.parametrize(
    "helper,scenario",
    [
        pytest.param("lab", "rpc-delay", id="lab"),
        pytest.param("server-runner", "rpc-delay", id="server-runner"),
        pytest.param("server-runner", "unrelated-host", id="server-runner-unrelated-host"),
        pytest.param("server-runner", "deadline", id="server-runner-deadline"),
        pytest.param("lab", "remaining-budget", id="lab-rpc-remaining-budget"),
        pytest.param("lab", "late-success", id="lab-rpc-late-success"),
        pytest.param("server-runner", "remaining-budget", id="server-runner-rpc-remaining-budget"),
        pytest.param("server-runner", "late-success", id="server-runner-rpc-late-success"),
    ],
)
def test_host_startup_waits_for_rpc_when_online_row_precedes_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helper: str, scenario: str
) -> None:
    host_id = "a" * 32
    rpc_attempted = False
    rpc_ready = False
    clock = SimpleNamespace(now=0.0)
    spawned_env = {}
    request_timeouts = []
    rpc_timeouts = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal rpc_attempted, rpc_ready
        assert request.method == "GET"
        request_timeouts.append(request.extensions["timeout"])
        if request.url.path == "/v1/hosts":
            if scenario == "deadline":
                clock.now = 1.0
            # The database commit can be visible before the tunnel is registered.
            rows = [{"host_id": host_id, "status": "online"}]
            if scenario == "unrelated-host":
                rows.insert(0, {"host_id": "unrelated-host", "status": "online"})
            return httpx.Response(200, json={"hosts": rows})
        if request.url.path == "/v1/hosts/unrelated-host/filesystem":
            return httpx.Response(200, json={"data": []})
        assert request.url.path == f"/v1/hosts/{host_id}/filesystem"
        assert request.url.params["limit"] == "1"
        rpc_timeouts.append(request.extensions["timeout"])
        if scenario == "deadline":
            rpc_ready = True
            return httpx.Response(200, json={"data": []})
        if not rpc_attempted:
            rpc_attempted = True
            if scenario in {"remaining-budget", "late-success"}:
                clock.now = 0.75
            return httpx.Response(
                409, json={"error": {"code": "conflict", "message": "host is offline"}}
            )
        rpc_ready = True
        if scenario == "late-success":
            clock.now = 1.1
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    with ExitStack() as resources:
        observer = resources.enter_context(
            httpx.Client(transport=httpx.MockTransport(respond), base_url="http://test")
        )
        if helper == "lab":
            lab = Lab(LabConfig(root=tmp_path / "lab"))
            resources.callback(lab.stop)
            host_id = lab.host_id
            monkeypatch.setattr(lab, "_proxies", Mock(host=Mock(url="http://test")))
            monkeypatch.setattr(lab, "_host_side_env", dict)
            monkeypatch.setattr(lab, "_spawn", lambda *_: Mock(running=True))
            lab.observer = observer
            if scenario in {"remaining-budget", "late-success"}:
                monkeypatch.setattr("tests.e2e.resilience.lab.lab._STARTUP_TIMEOUT_S", 1.0)
                monkeypatch.setattr(
                    "tests.e2e.resilience.lab.lab.time",
                    SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _: None),
                )
            start_host = lab._start_host
        else:
            stack = ServerRunner(
                tmp_path / "stack",
                resources,
                server_bootstrap=None,
                server_env={},
                base_env={},
                server_cwd=None,
                workspace=None,
                database_uri=None,
                artifact_location=None,
                binding_token="example-token",
                health_timeout=1,
                poll_interval=0,
            )
            stack.host_id = host_id

            def spawn(*args, **kwargs):
                spawned_env.update(args[3])
                return Mock(poll=lambda: None)

            monkeypatch.setattr(stack, "_client", observer)
            monkeypatch.setattr(stack, "_spawn", spawn)
            monkeypatch.setattr(
                "tests._helpers.server_runner.time",
                SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _: None),
            )
            start_host = stack.start_host
        if scenario in {"deadline", "late-success"}:
            error = TimeoutError if helper == "lab" else AssertionError
            with pytest.raises(error, match=r"host daemon|never became ready"):
                start_host()
            if scenario == "deadline":
                assert not rpc_ready, "No host RPC may start after the readiness deadline"
                assert request_timeouts[0]["read"] <= 1.0
            else:
                assert rpc_ready, "The deadline must reject the late successful RPC"
        else:
            start_host()
            assert rpc_ready, "Host startup returned before the host could answer an RPC"
        if scenario in {"remaining-budget", "late-success"}:
            assert len(rpc_timeouts) == 2
            assert 0 < rpc_timeouts[0]["read"] <= 1.0
            assert 0 < rpc_timeouts[1]["read"] <= 0.25
        if helper == "server-runner":
            assert spawned_env["OMNIGENT_HOST_ID"] == host_id
            assert spawned_env["OMNIGENT_HOST_NAME"]
