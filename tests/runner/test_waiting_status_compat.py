"""Exercise the version probe and terminal waiting-status publisher.

The E2E also checks explicit delegation refusal when an old server lacks the
requester protocol. These component cases retain the pre-0.3 downgrade proof
even when that refusal prevents creating child work through the public API.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.app import _version_supports_waiting_status
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests._helpers.polling import poll_until_async
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _spec_resolver_returning,
    _sse,
)


@pytest.mark.parametrize(
    ("server_version", "expected"),
    [
        ("0.2.0", False),  # the released version that 500s on "waiting"
        ("0.2.5", False),
        ("0.1.1", False),
        ("0.3.0", True),  # first release that models "waiting"
        ("0.3.0.dev0", True),  # main: dev of the supporting release still supports it
        ("0.3.1", True),
        ("0.4.0", True),  # later minor: still supports "waiting"
        ("1.0.0", True),
        ("source", False),  # source installs can report non-PEP-440 metadata
        ("not-a-version", False),  # malformed probes fail closed
    ],
)
def test_version_supports_waiting_status(server_version: str, expected: bool) -> None:
    assert _version_supports_waiting_status(server_version) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "probe_status", "expected"),
    [
        ("0.2.0", 200, "running"),
        ("0.3.0", 200, "waiting"),
        ("0.3.0.dev0", 200, "waiting"),
        ("source", 200, "running"),
        (None, 503, "running"),
    ],
)
async def test_completed_parent_publishes_version_gated_waiting_status(
    monkeypatch: pytest.MonkeyPatch, version: str | None, probe_status: int, expected: str
) -> None:
    monkeypatch.setattr(runner_app, "_server_version", None)
    probes = []

    def server_response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            probes.append(request)
            return httpx.Response(probe_status, json={"version": version})
        return httpx.Response(200, json={})

    finished = asyncio.Event()
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "response-waiting"}}),
            _sse({"type": "response.completed", "response": {"id": "response-waiting"}}),
        ],
        stream_finished=finished,
    )
    spec = AgentSpec(
        spec_version=1,
        name="waiting-parent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "test-waiting"}),
    )
    parent_id, child_id = uuid4().hex, uuid4().hex
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server_response), base_url="http://server"
    ) as server:
        app = create_runner_app(
            process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
            spec_resolver=await _spec_resolver_returning(spec),
            server_client=server,
        )
        subagent_work.register_subagent_work(
            parent_session_id=parent_id, child_session_id=child_id, agent="worker", title="probe"
        )
        try:
            async with _runner_client(app) as client:
                initialized = await client.post(
                    "/v1/sessions", json={"session_id": parent_id, "agent_id": "waiting-agent"}
                )
                assert initialized.status_code == 201, initialized.text
                response = await client.post(
                    f"/v1/sessions/{parent_id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "waiting-agent",
                        "content": [{"type": "input_text", "text": "Dispatch work"}],
                    },
                )
                assert response.status_code == 202, response.text
                await asyncio.wait_for(finished.wait(), timeout=5)
                await poll_until_async(lambda: parent_id not in app.state.active_turns)
                events = _drain_session_event_queue(
                    runner_app._session_event_queues_ref.get(parent_id)
                )
            assert len(probes) == 1
            completed = next(
                index
                for index, event in enumerate(events)
                if event["type"] == "response.completed"
            )
            assert [
                event["status"]
                for event in events[completed + 1 :]
                if event["type"] == "session.status"
            ] == [expected]
        finally:
            subagent_work.unregister_subagent_work(child_id)
