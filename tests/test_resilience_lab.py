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
        if scenario == "deadline":
            rpc_ready = True
            return httpx.Response(200, json={"data": []})
        if not rpc_attempted:
            rpc_attempted = True
            return httpx.Response(
                409, json={"error": {"code": "conflict", "message": "host is offline"}}
            )
        rpc_ready = True
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
            lab._start_host()
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
            if scenario == "deadline":
                with pytest.raises(AssertionError, match="never became ready"):
                    stack.start_host()
                assert not rpc_ready, "No host RPC may start after the readiness deadline"
                assert request_timeouts[0]["read"] <= 1.0
                return
            stack.start_host()
        assert rpc_ready, "Host startup returned before the host could answer an RPC"
        if helper == "server-runner":
            assert spawned_env["OMNIGENT_HOST_ID"] == host_id
            assert spawned_env["OMNIGENT_HOST_NAME"]
