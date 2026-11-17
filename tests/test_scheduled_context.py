"""Automation metadata crosses requests without leaking into interactive traffic."""

import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from omnigent.entities.scheduled_task import ScheduledRunLogContext
from omnigent.host.frames import HostLaunchRunnerFrame, decode_host_frame, encode_host_frame
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.tcp import create_tcp_client
from omnigent.runner.transports.uds import create_uds_client
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.scheduled_context import (
    SCHEDULED_RUN_HEADER,
    current_scheduled_run,
    parse_scheduled_run,
    scheduled_run_scope,
)
from omnigent.server._runner_transport import build_uds_runner


@pytest.mark.parametrize("value", [None, "not json", "{}", {"run_id": "incomplete"}, 42])
def test_invalid_metadata_is_ignored(value):
    assert parse_scheduled_run(value) is None
    payload = json.loads(encode_host_frame(HostLaunchRunnerFrame("request", "token", "/repo")))
    payload["scheduled_run"] = value
    frame = decode_host_frame(json.dumps(payload))
    assert isinstance(frame, HostLaunchRunnerFrame)
    assert frame.scheduled_run is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_kind", ["tunnel", "tcp", "uds", "server_uds"])
async def test_runner_client_factories_isolate_scheduled_and_interactive_requests(
    transport_kind, monkeypatch, tmp_path
):
    seen = {}

    async def receive(request):
        await asyncio.sleep(0)
        seen[request.url.path] = parse_scheduled_run(request.headers.get(SCHEDULED_RUN_HEADER))
        return httpx.Response(200)

    socket_path = str(tmp_path / "runner.sock")
    if transport_kind == "tunnel":
        router = RunnerRouter(registry=TunnelRegistry(), conversation_store=Mock())
        client = router._client_for_runner("runner")
        assert router._client_for_runner("runner") is client
    elif transport_kind == "tcp":
        client = create_tcp_client("http://runner")
    elif transport_kind == "uds":
        client = create_uds_client(socket_path)
    else:
        client, _ = build_uds_runner(socket_path)

    async with httpx.MockTransport(receive) as transport, client:
        # Keep the factory's hooks; replace only network I/O, including proxy routes.
        monkeypatch.setattr(client, "_transport_for_url", lambda _: transport)

        async def send(run_id):
            run = ScheduledRunLogContext(
                task_id="task",
                run_id=run_id,
                task_workspace_id=0,
                scheduled_at=1000,
                target="connected_host",
                session_mode="continue",
            )
            with scheduled_run_scope(run):
                await client.post(f"/{run_id}")
                with scheduled_run_scope(None):
                    await client.post(f"/{run_id}/interactive")
                assert current_scheduled_run() == run
            assert current_scheduled_run() is None

        await asyncio.gather(send("first"), send("second"))
        await client.post("/interactive", headers={SCHEDULED_RUN_HEADER: "stale"})
    assert seen["/first"].run_id == "first"
    assert seen["/second"].run_id == "second"
    assert all(seen[path] is None for path in seen if path.endswith("interactive"))
