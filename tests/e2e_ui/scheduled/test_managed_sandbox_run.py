"""A managed-sandbox automation saves its prompt before the sandbox exists.

Real product replicas sharing one database, the real host launcher and runner,
and a keyed ingress; only cloud allocation and model output are local. The
fire runs on replica A, which holds provisioning at the provider boundary, and
the host's tunnel lands on B, so only B's saved-input worker can deliver.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.start_session.test_managed_first_message_routing import (
    ManagedReplicas,
    _persisted_messages,
    _poll,
    managed_replicas,  # noqa: F401 — shared real-process fixture
)

_PROMPT = "Summarize the overnight build results for this automation."
_REPLY = "The automation's saved prompt reached the sandbox."
_CROSS_REPLICA = pytest.mark.parametrize("managed_replicas", [(True, True)], indirect=True)


async def _create_task(client: httpx.AsyncClient, rig: ManagedReplicas) -> str:
    agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
    agent_id = next(agent["id"] for agent in agents if agent["name"] == "routing_probe")
    response = await client.post(
        f"{rig.a}/v1/scheduled-tasks",
        json={
            "name": "managed sandbox automation",
            "prompt": _PROMPT,
            "rrule": "FREQ=DAILY",
            "agent_id": agent_id,
            "execution_target": "managed_sandbox",
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


async def _run_now(client: httpx.AsyncClient, rig: ManagedReplicas, task_id: str) -> str:
    """Fire the task on A and return the session its run created."""
    response = await client.post(f"{rig.a}/v1/scheduled-tasks/{task_id}/run")
    assert response.status_code == 202, response.text
    runs = await _poll(
        client,
        f"{rig.a}/v1/scheduled-tasks/{task_id}/runs",
        lambda body: bool(body["runs"] and body["runs"][0]["conversation_id"]),
    )
    return runs["runs"][0]["conversation_id"]


async def _run(client: httpx.AsyncClient, rig: ManagedReplicas, task_id: str) -> dict[str, Any]:
    body = (await client.get(f"{rig.a}/v1/scheduled-tasks/{task_id}/runs")).json()
    return body["runs"][0]


@pytest.mark.timeout(240)
@_CROSS_REPLICA
def test_managed_automation_prompt_is_saved_before_provisioning_and_delivered(
    managed_replicas: ManagedReplicas,  # noqa: F811 — pytest injects the imported fixture
    mock_llm_server_url: str,
) -> None:
    """The prompt is pending while the sandbox provisions, then runs on the host's replica."""
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY, "stream": True}],
        key="managed-automation-run",
        match=_PROMPT,
    )
    run_in_fresh_loop(_drive_delivery(managed_replicas))


async def _drive_delivery(rig: ManagedReplicas) -> None:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        task_id = await _create_task(client, rig)
        session_id = await _run_now(client, rig, task_id)
        path = f"/v1/sessions/{session_id}"

        await _poll(client, f"{rig.a}/__test/state", lambda body: body["provisioning"])
        snapshot = (await client.get(rig.a + path)).json()
        assert snapshot["host_id"] is None and snapshot["runner_id"] is None
        assert [_PROMPT in str(entry["content"]) for entry in snapshot["pending_inputs"]] == [
            True
        ], snapshot["pending_inputs"]
        assert (await _run(client, rig, task_id))["status"] == "running"

        (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()
        snapshot = await _poll(
            client,
            rig.b + path,
            lambda body: len(_persisted_messages(body, "assistant", _REPLY)) == 1,
            timeout=90,
        )
        state = (
            await client.get(f"{rig.b}/__test/state", params={"host_id": snapshot["host_id"]})
        ).json()
        assert state["host_local"] is True, "the host tunnel was expected on replica B"
        assert len(_persisted_messages(snapshot, "user", _PROMPT)) == 1
        assert snapshot["pending_inputs"] == []
        run = await _poll(
            client,
            f"{rig.a}/v1/scheduled-tasks/{task_id}/runs",
            lambda body: body["runs"][0]["status"] != "running",
        )
        assert run["runs"][0]["status"] == "succeeded", run


@pytest.mark.timeout(240)
@_CROSS_REPLICA
def test_failed_managed_automation_launch_fails_the_run_and_saved_prompt(
    managed_replicas: ManagedReplicas,  # noqa: F811 — pytest injects the imported fixture
) -> None:
    """A provider failure fails the run with launch_failed and reports the undelivered prompt."""
    run_in_fresh_loop(_drive_failed_launch(managed_replicas))


async def _drive_failed_launch(rig: ManagedReplicas) -> None:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        (await client.post(f"{rig.a}/__test/provision/fail")).raise_for_status()
        task_id = await _create_task(client, rig)
        session_id = await _run_now(client, rig, task_id)
        await _poll(client, f"{rig.a}/__test/state", lambda body: body["provisioning"])
        (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()

        runs = await _poll(
            client,
            f"{rig.a}/v1/scheduled-tasks/{task_id}/runs",
            lambda body: body["runs"][0]["status"] != "running",
            timeout=60,
        )
        assert runs["runs"][0]["status"] == "failed"
        assert runs["runs"][0]["error_code"] == "launch_failed"
        snapshot = await _poll(
            client,
            f"{rig.a}/v1/sessions/{session_id}",
            lambda body: any(
                item["type"] == "error" and item["data"]["code"] == "startup_input_failed"
                for item in body["items"]
            ),
        )
        assert snapshot["labels"]["omnigent.startup"] == "failed"
        assert snapshot["pending_inputs"] == []
