from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.quota_wait import quota_wait_path, watch_quota_waits


@pytest.mark.asyncio
async def test_watcher_posts_wait_telemetry_then_clears(tmp_path: Path) -> None:
    posts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(204)

    waits = tmp_path / "quota-waits"
    waits.mkdir(mode=0o700)
    path = quota_wait_path(waits, "conv_123")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runner_id": "runner-a",
                "session_id": "conv_123",
                "state": "waiting",
                "updated_at": time.time(),
                "admission_delay_seconds": 12.5,
                "current_rate_ppm_per_second": 2.0,
                "burst_multiplier": 3.0,
                "linear_schedule_delta_ppm": -400,
            }
        )
    )
    path.chmod(0o600)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")
    await watch_quota_waits(
        client,
        waits,
        runner_id="runner-a",
        poll_seconds=0,
        iterations=1,
    )
    path.unlink()
    await watch_quota_waits(
        client,
        waits,
        runner_id="runner-a",
        poll_seconds=0,
        iterations=1,
        previously_waiting={"conv_123"},
    )
    await client.aclose()

    first = posts[0]["data"]
    assert isinstance(first, dict)
    assert first["status"] == "running"
    assert first["blocked_on"] == "quota pacing"
    assert first["quota_wait"] == {
        "admission_delay_seconds": 12.5,
        "current_rate_ppm_per_second": 2.0,
        "burst_multiplier": 3.0,
        "linear_schedule_delta_ppm": -400,
    }
    assert posts[1] == {"type": "external_session_status", "data": {"status": "running"}}


@pytest.mark.asyncio
async def test_watcher_ignores_foreign_runner_and_unsafe_file(tmp_path: Path) -> None:
    posts: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(204)

    waits = tmp_path / "quota-waits"
    waits.mkdir(mode=0o700)
    foreign = quota_wait_path(waits, "conv_foreign")
    foreign.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runner_id": "runner-b",
                "session_id": "conv_foreign",
                "state": "waiting",
                "updated_at": time.time(),
                "admission_delay_seconds": 1,
            }
        )
    )
    foreign.chmod(0o600)
    unsafe = quota_wait_path(waits, "conv_unsafe")
    unsafe.write_text(foreign.read_text())
    unsafe.chmod(0o644)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")
    await watch_quota_waits(
        client,
        waits,
        runner_id="runner-a",
        poll_seconds=0,
        iterations=1,
    )
    await client.aclose()
    assert posts == []


@pytest.mark.asyncio
async def test_watcher_reasserts_wait_after_terminal_status_churn(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        event = json.loads(request.content)
        events.append(event)
        if len(events) == 1:
            events.append({"type": "session.status", "data": {"status": "idle"}})
        return httpx.Response(204)

    waits = tmp_path / "quota-waits"
    waits.mkdir(mode=0o700)
    path = quota_wait_path(waits, "conv_churn")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runner_id": "runner-a",
                "session_id": "conv_churn",
                "state": "waiting",
                "updated_at": time.time(),
                "admission_delay_seconds": 10,
            }
        )
    )
    path.chmod(0o600)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        await watch_quota_waits(
            client,
            waits,
            runner_id="runner-a",
            poll_seconds=0,
            iterations=2,
        )

    assert [event["data"]["status"] for event in events] == ["running", "idle", "running"]
    assert events[-1]["data"]["quota_wait"] == {"admission_delay_seconds": 10.0}
