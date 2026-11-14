"""Mocked readiness checks for native host startup helpers."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from tests._helpers.server_runner import ServerRunner
from tests.e2e.resilience.lab.lab import Lab, LabConfig


@pytest.mark.parametrize("helper", ["lab", "server-runner"])
def test_host_startup_waits_for_rpc_when_online_row_precedes_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helper: str
) -> None:
    host_id = "example-host"
    rpc_attempted = False
    rpc_ready = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal rpc_attempted, rpc_ready
        assert request.method == "GET"
        if request.url.path == "/v1/hosts":
            # The database commit can be visible before the tunnel is registered.
            return httpx.Response(200, json={"hosts": [{"host_id": host_id, "status": "online"}]})
        assert request.url.path == f"/v1/hosts/{host_id}/filesystem"
        assert request.url.params["limit"] == "1"
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
            monkeypatch.setattr(stack, "_client", observer)
            monkeypatch.setattr(stack, "_spawn", lambda *_, **__: Mock(poll=lambda: None))
            stack.start_host()
        assert rpc_ready, "Host startup returned before the host could answer an RPC"
