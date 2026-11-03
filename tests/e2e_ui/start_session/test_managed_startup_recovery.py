"""Managed startup recovers across replicas without any client request.

Real product replicas sharing one database, the real host launcher and runner,
and a keyed ingress; only cloud allocation is local. The create stays on
replica A while the host, keying by its own id, tunnels to replica B, so the
launch on A can't start the runner itself.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.start_session.test_managed_first_message_routing import (
    ManagedReplicas,
    _poll,
    managed_replicas,  # noqa: F401 — shared real-process fixture
)

_PROMPT = "Acknowledge this saved first message."
_CROSS_REPLICA = pytest.mark.parametrize("managed_replicas", [(True, True)], indirect=True)


async def _create(client: httpx.AsyncClient, rig: ManagedReplicas, **body: Any) -> str:
    agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
    agent_id = next(agent["id"] for agent in agents if agent["name"] == "routing_probe")
    response = await client.post(
        f"{rig.ingress}/v1/sessions", json={"agent_id": agent_id, "host_type": "managed", **body}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _events(rows: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    return [row for row in rows if row["path"] == f"/v1/sessions/{session_id}/events"]


@pytest.mark.timeout(240)
@_CROSS_REPLICA
def test_runner_starts_on_the_host_replica_without_a_first_message(
    managed_replicas: ManagedReplicas,  # noqa: F811 — pytest injects the imported fixture
) -> None:
    """A session created without a prompt has a working runner before anyone sends one."""
    run_in_fresh_loop(_drive_no_prompt(managed_replicas))


async def _drive_no_prompt(rig: ManagedReplicas) -> None:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        session_id = await _create(client, rig)
        path = f"/v1/sessions/{session_id}"
        await _poll(client, f"{rig.a}/__test/state", lambda body: body["provisioning"])
        (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()
        snapshot = await _poll(
            client,
            rig.b + path,
            lambda body: bool(body.get("runner_id") and body.get("runner_online")),
            timeout=90,
        )
        state = (
            await client.get(f"{rig.b}/__test/state", params={"host_id": snapshot["host_id"]})
        ).json()
        assert state["host_local"] is True, "the host tunnel was expected on replica B"
        # The workspace panels are served by the runner.
        resources = await client.get(f"{rig.b}{path}/resources")
        assert resources.status_code == 200, resources.text
        rows = (await client.get(f"{rig.ingress}/__test/network")).json()
        assert not _events(rows, session_id), "no message may be needed to start the runner"
        # Startup is over once the runner is bound, so later reads skip it.
        await _poll(
            client, rig.b + path, lambda body: "omnigent.startup" not in (body.get("labels") or {})
        )


@pytest.mark.timeout(240)
@_CROSS_REPLICA
def test_failed_launch_is_reported_on_the_replica_without_its_tracker(
    managed_replicas: ManagedReplicas,  # noqa: F811 — pytest injects the imported fixture
) -> None:
    """A failed create launch fails its saved first message everywhere, not just on A."""
    run_in_fresh_loop(_drive_failed_launch(managed_replicas))


async def _drive_failed_launch(rig: ManagedReplicas) -> None:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        (await client.post(f"{rig.a}/__test/provision/fail")).raise_for_status()
        session_id = await _create(
            client,
            rig,
            initial_input={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": _PROMPT}]},
            },
        )
        path = f"/v1/sessions/{session_id}"
        await _poll(client, f"{rig.a}/__test/state", lambda body: body["provisioning"])
        (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()
        # Replica B never ran this launch, so it has no tracker entry for it.
        snapshot = await _poll(
            client,
            rig.b + path,
            lambda body: (
                (body.get("labels") or {}).get("omnigent.startup") == "failed"
                and any(item["type"] == "error" for item in body["items"])
            ),
            timeout=60,
        )
        assert snapshot["pending_inputs"] == []
        errors = [item for item in snapshot["items"] if item["type"] == "error"]
        # The provider's own text stays server-side; the launch failure is named.
        assert any(
            item["data"]["code"] == "startup_input_failed"
            and "managed sandbox launch" in item["data"]["message"]
            for item in errors
        ), errors
        assert not any(
            "injected sandbox allocation failure" in item["data"]["message"] for item in errors
        )
        message = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "still there?"}]},
        }
        late = await client.post(f"{rig.b}{path}/events", json=message)
        # A failed launch doesn't hold new messages; they take the normal no-runner path.
        assert late.status_code == 503, late.text
        assert late.json()["error"]["code"] == "runner_unavailable", late.text
