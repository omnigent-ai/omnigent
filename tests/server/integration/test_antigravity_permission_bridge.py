"""Real hook/resolve transport with the production agy reader and interaction bridge."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.antigravity_native.interactions import bridge_interaction
from omnigent.harnesses.antigravity_native.reader import _request_agy_elicitation
from omnigent.harnesses.antigravity_native.steps import pending_interaction
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult
from tests.server.helpers import create_test_session, start_session_stream_collector


@pytest.mark.parametrize("persist, digit", [("session", "2"), ("always", "3")])
@pytest.mark.parametrize("changed_command", [False, True])
async def test_permission_consent_through_reader_and_server(
    client: httpx.AsyncClient, persist: str, digit: str, changed_command: bool
) -> None:
    """Only an unchanged retry may use the answer returned over the hook transport."""
    session = await create_test_session(client, "agy-consent-bridge")
    session_id = session["id"]
    step = json.loads(
        (
            Path(__file__).parents[2] / "fixtures/antigravity/steps/run_command_waiting.json"
        ).read_text()
    )
    pending = pending_interaction(step)
    assert pending is not None
    retry = copy.deepcopy(step)
    retry["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"] += 1
    if changed_command:
        retry["requestedInteraction"]["permission"]["resource"]["target"] = "pwd -P"
    delivered: list[dict[str, object]] = []
    typed: list[list[str]] = []

    async def get_steps() -> list[dict[str, object]]:
        return [retry]

    async def request(eid: str, params: ElicitationRequestParams) -> ElicitationResult | None:
        return await _request_agy_elicitation(
            client, session_id, elicitation_id=eid, params=params
        )

    async def deliver(
        port: int,
        cascade_id: str,
        *,
        trajectory_id: str,
        step_index: int,
        payload: dict[str, object],
    ) -> None:
        delivered.append(payload)

    async def inject_tui(keys: list[str]) -> None:
        if not changed_command:
            assert delivered == [], "RPC must not close the menu before persistence"
        typed.append(keys)

    collector = await start_session_stream_collector(session_id)

    async def next_prompt() -> dict:
        async with asyncio.timeout(5):
            while True:
                event = await collector.next_event()
                if event["type"] == "response.elicitation_request":
                    return event

    task = asyncio.create_task(
        bridge_interaction(
            "test-cascade",
            pending,
            port=1,
            get_steps=get_steps,
            request_elicitation=request,
            deliver=deliver,
            inject_tui=inject_tui,
        )
    )
    try:
        first = await next_prompt()
        assert first["params"]["command"] == "pwd"
        assert first["params"]["always_allow_pattern"] == "pwd"
        response = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{first['elicitation_id']}/resolve",
            json={"action": "accept", "_meta": {"persist": persist}},
        )
        assert response.status_code == 202, response.text
        if changed_command:
            second = await next_prompt()
            assert second["elicitation_id"] != first["elicitation_id"]
            assert second["params"]["command"] == "pwd -P"
            assert delivered == []
            assert typed == []
            response = await client.post(
                f"/v1/sessions/{session_id}/elicitations/{second['elicitation_id']}/resolve",
                json={"action": "decline"},
            )
            assert response.status_code == 202, response.text
        await asyncio.wait_for(task, timeout=5)
        assert delivered == ([{"permission": {"allow": False}}] if changed_command else [])
        assert typed == [["4" if changed_command else digit, "Enter"]]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await collector.stop()
