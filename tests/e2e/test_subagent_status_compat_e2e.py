"""The current transcript forwarder must keep working with released servers.

The compat_smoke marker includes this in Backwards-Compat's PR gate, which
pins the real server subprocess to the latest stable release (including 0.14.x).
HTTP response hooks observe traffic; no server responses are mocked.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from packaging.version import Version

from omnigent.harnesses.claude_native import forwarder
from tests.e2e.conftest import create_runner_bound_session, register_inline_agent


@pytest.mark.compat_smoke
@pytest.mark.min_server_version("0.14.0")
async def test_subagent_idle_reporting_with_old_server(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    server_version: str,
    tmp_path: Path,
) -> None:
    """An unsupported idle event is tried once; later items/statuses still arrive."""
    agent_name = register_inline_agent(
        http_client,
        name=f"subagent-status-compat-{uuid.uuid4().hex[:8]}",
        harness="openai-agents",
        model="mock-model",
        profile="",
        prompt="Acknowledge the message.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    parent_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response = http_client.patch(
        f"/v1/sessions/{parent_id}",
        json={"labels": {"omnigent.wrapper": "claude-code-native-ui"}},
    )
    response.raise_for_status()

    transcript_path = tmp_path / "parent.jsonl"
    transcript_path.touch()
    subagents_dir = tmp_path / "parent" / "subagents"
    subagents_dir.mkdir(parents=True)
    bridge_dir = tmp_path / "bridge"
    state = forwarder.SubagentForwardState(subagents={})
    capability = forwarder._SubagentStatusCapability()
    batch_capability = forwarder._SessionEventBatchCapability()
    start_retries = forwarder._PostRetryTracker()
    item_retries = forwarder._PostRetryTracker()
    status_retries = forwarder._PostRetryTracker()
    status_events: list[tuple[dict[str, Any], httpx.Response]] = []

    async def capture_status(response: httpx.Response) -> None:
        if response.request.method != "POST":
            return
        body = json.loads(response.request.content)
        if isinstance(body, dict) and body.get("type") in {
            "subagent.status",
            "external_session_status",
        }:
            await response.aread()
            status_events.append((body, response))

    async with httpx.AsyncClient(
        base_url=str(http_client.base_url),
        headers=dict(http_client.headers),
        timeout=30.0,
        event_hooks={"response": [capture_status]},
    ) as client:

        async def tick() -> None:
            nonlocal state
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id=parent_id,
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native",
                start_retry_tracker=start_retries,
                item_retry_tracker=item_retries,
                status_retry_tracker=status_retries,
                batch_capability=batch_capability,
                status_capability=capability,
            )

        # Resume the first child, then discover a second child after the first
        # rejection. Both must share the forwarder's unsupported-event cache.
        for cycle, child in enumerate(("first", "first", "second")):
            if child not in state.subagents:
                tool_id = f"toolu_{child}"
                with transcript_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "type": "assistant",
                                "uuid": f"spawn-{child}",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": tool_id,
                                            "name": "Agent",
                                            "input": {"description": child},
                                        }
                                    ],
                                },
                            }
                        )
                        + "\n"
                    )
                (subagents_dir / f"agent-{child}.meta.json").write_text(
                    json.dumps(
                        {
                            "agentType": "general-purpose",
                            "description": child,
                            "toolUseId": tool_id,
                        }
                    ),
                    encoding="utf-8",
                )
            text = f"Still working: {child}, cycle {cycle}"
            with (subagents_dir / f"agent-{child}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "isSidechain": True,
                            "type": "assistant",
                            "uuid": f"message-{cycle}",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": text}],
                            },
                        }
                    )
                    + "\n"
                )
            await tick()
            entry = state.subagents[child]
            child_id = entry.child_conversation_id
            assert child_id and entry.last_status == "running"
            snapshot = await client.get(f"/v1/sessions/{child_id}")
            snapshot.raise_for_status()
            assert snapshot.json()["status"] == "running"
            assert text in json.dumps(snapshot.json()["items"])

            await asyncio.sleep(forwarder._SUBAGENT_IDLE_THRESHOLD_S + 0.1)
            await tick()
            await tick()
            assert state.subagents[child].last_status == "idle"
            assert not status_retries.has_retry_state(f"subagent_status:{child_id}")

            idle_responses = [r for body, r in status_events if body["type"] == "subagent.status"]
            assert idle_responses, "the real inactivity path must attempt the new event"
            first = idle_responses[0]
            if Version(server_version).release < (0, 15):
                assert first.status_code == 400, first.text
            if first.status_code == 400:
                assert first.json()["error"]["code"] == "invalid_input"
                assert first.json()["error"]["message"].startswith(
                    "Unknown event type: 'subagent.status'."
                )
                assert len(idle_responses) == 1
                assert not capability.supported
                expected_status = "running"
            else:
                assert all(r.status_code == 202 for r in idle_responses)
                assert len(idle_responses) == cycle + 1
                expected_status = "idle"
            snapshot = await client.get(f"/v1/sessions/{child_id}")
            snapshot.raise_for_status()
            assert snapshot.json()["status"] == expected_status

        # No fallback terminal status may be emitted by the inactivity path.
        ordinary_statuses = [
            body for body, _ in status_events if body["type"] == "external_session_status"
        ]
        assert (
            ordinary_statuses
            == [{"type": "external_session_status", "data": {"status": "running"}}] * 3
        )

        # Authoritative completion and failure still work after the no-op.
        for child, status in (("first", "idle"), ("second", "failed")):
            child_id = state.subagents[child].child_conversation_id
            await forwarder.post_external_session_status(
                client, session_id=child_id, status=status
            )
            snapshot = await client.get(f"/v1/sessions/{child_id}")
            snapshot.raise_for_status()
            assert snapshot.json()["status"] == status
