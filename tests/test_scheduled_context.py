"""Automation metadata crosses requests without leaking into interactive traffic."""

import asyncio
import json

import httpx
import pytest

from omnigent.entities.scheduled_task import ScheduledRunLogContext
from omnigent.host.frames import HostLaunchRunnerFrame, decode_host_frame, encode_host_frame
from omnigent.scheduled_context import (
    SCHEDULED_RUN_HEADER,
    add_scheduled_run_header,
    current_scheduled_run,
    parse_scheduled_run,
    scheduled_run_scope,
)


@pytest.mark.parametrize("value", [None, "not json", "{}", {"run_id": "incomplete"}, 42])
def test_invalid_metadata_is_ignored(value):
    assert parse_scheduled_run(value) is None
    payload = json.loads(encode_host_frame(HostLaunchRunnerFrame("request", "token", "/repo")))
    payload["scheduled_run"] = value
    frame = decode_host_frame(json.dumps(payload))
    assert isinstance(frame, HostLaunchRunnerFrame)
    assert frame.scheduled_run is None


@pytest.mark.asyncio
async def test_shared_runner_client_isolates_concurrent_scheduled_and_interactive_requests():
    seen = {}

    async def receive(request):
        await asyncio.sleep(0)
        seen[request.url.path] = parse_scheduled_run(request.headers.get(SCHEDULED_RUN_HEADER))
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive),
        base_url="http://runner",
        event_hooks={"request": [add_scheduled_run_header]},
    ) as client:

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
