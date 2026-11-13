"""OMP's native form dialog parks on a safe, typed Omnigent web elicitation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runtime import session_stream
from omnigent.server.routes.sessions import routes_hooks
from tests.server.helpers import create_test_agent
from tests.test_pi_extension_ui import _omp_form_request

pytestmark = pytest.mark.asyncio


async def _session(client: httpx.AsyncClient, harness: str = "omp-native") -> str:
    agent = await create_test_agent(
        client,
        name="native-form-hook",
        executor={"type": "omnigent", "config": {"harness": harness}},
    )
    return agent["_session_id"]


async def _next_form(session_id: str) -> dict:
    async with asyncio.timeout(5):
        async for event in session_stream.subscribe(session_id):
            if event.get("type") == "response.elicitation_request":
                return event
    raise AssertionError("native form was not published")


@pytest.mark.parametrize("action", ["accept", "cancel", "decline"])
async def test_native_form_roundtrip_preserves_schema_and_typed_answers(
    client: httpx.AsyncClient, action: str
) -> None:
    session_id = await _session(client)
    request = _omp_form_request()
    elicitation_id = "elicit_omp_" + "a" * 32
    drain = asyncio.create_task(_next_form(session_id))
    await asyncio.sleep(0)
    parked = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/pi-extension-ui",
            json={"elicitation_id": elicitation_id, "request": request},
        )
    )
    try:
        event = await drain
        assert event["elicitation_id"] == elicitation_id
        assert event["params"]["requestedSchema"] == request["requestedSchema"]
        assert event["params"]["phase"] == "omp_extension_ui"
        assert "ask_user_question" not in event["params"]
        assert not parked.done()
        content = {"f0": "keep\nnewlines\n", "f1": 3, "f2_0": False, "f2_1": True, "f3": ""}
        resolved = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": action, "content": content if action == "accept" else None},
        )
        assert resolved.status_code == 202, resolved.text
        response = await asyncio.wait_for(parked, 5)
        assert response.status_code == 200, response.text
        if action == "accept":
            assert response.json()["value"] == content
        else:
            assert response.json()["cancelled"] is True
            assert response.json()["timedOut"] is False
    finally:
        for task in (drain, parked):
            if not task.done():
                task.cancel()
        await asyncio.gather(drain, parked, return_exceptions=True)


async def test_form_deadline_is_distinct_from_explicit_cancellation(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _session(client)
    clock = iter([100.0, 100.01])
    monkeypatch.setattr(routes_hooks, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def expired(*args, **kwargs):
        return None

    monkeypatch.setattr(routes_hooks, "_publish_and_wait_for_harness_elicitation", expired)
    response = await client.post(
        f"/v1/sessions/{session_id}/hooks/pi-extension-ui",
        json={
            "elicitation_id": "elicit_omp_" + "b" * 32,
            "request": {**_omp_form_request(), "timeout": 5},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["cancelled"] is True
    assert response.json()["timedOut"] is True


async def test_pi_session_cannot_request_omp_native_form_method(client: httpx.AsyncClient) -> None:
    session_id = await _session(client, "pi-native")
    response = await client.post(
        f"/v1/sessions/{session_id}/hooks/pi-extension-ui",
        json={"elicitation_id": "elicit_omp_" + "c" * 32, "request": _omp_form_request()},
    )
    assert response.status_code == 400, response.text
