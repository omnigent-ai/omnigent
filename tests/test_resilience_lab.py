"""Mocked readiness checks for the resilience lab."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from tests.e2e.resilience.lab.lab import Lab, LabConfig


def test_host_startup_waits_for_rpc_when_online_row_precedes_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lab = Lab(LabConfig(root=tmp_path / "lab"))
    monkeypatch.setattr(lab, "_proxies", Mock(host=Mock(url="http://test")))
    monkeypatch.setattr(lab, "_host_side_env", dict)
    monkeypatch.setattr(lab, "_spawn", lambda *_: Mock(running=True))
    rpc_attempted = False
    rpc_ready = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal rpc_attempted, rpc_ready
        assert request.method == "GET"
        if request.url.path == "/v1/hosts":
            # The database commit can be visible before the tunnel is registered.
            return httpx.Response(
                200, json={"hosts": [{"host_id": lab.host_id, "status": "online"}]}
            )
        assert request.url.path == f"/v1/hosts/{lab.host_id}/filesystem"
        assert request.url.params["limit"] == "1"
        if not rpc_attempted:
            rpc_attempted = True
            return httpx.Response(
                409, json={"error": {"code": "conflict", "message": "host is offline"}}
            )
        rpc_ready = True
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    try:
        with httpx.Client(
            transport=httpx.MockTransport(respond), base_url="http://test"
        ) as observer:
            lab.observer = observer
            lab._start_host()
            assert rpc_ready, "Lab startup returned before the host could answer an RPC"
    finally:
        lab.stop()
