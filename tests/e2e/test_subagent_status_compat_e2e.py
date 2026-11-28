"""The current transcript forwarder must keep working with released servers.

The compat_smoke marker includes this in Backwards-Compat's PR gate, which
pins the real server subprocess to the latest stable release (including 0.14.x).
HTTP response hooks observe traffic; no server responses are mocked.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from packaging.version import Version

from omnigent.harnesses.claude_native import forwarder
from tests.e2e.conftest import create_runner_bound_session, register_inline_agent


@pytest.mark.parametrize("character,encoded_bytes", [("x", 1), ("\x00", 6), ("€", 3)])
def test_large_tool_mirror_crosses_real_server_body_limit(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    character: str,
    encoded_bytes: int,
) -> None:
    """A separate forwarder process uploads once, then resumes its durable cursor."""
    from omnigent.session_event_batch import MAX_SESSION_EVENT_REQUEST_BYTES

    agent_name = register_inline_agent(
        http_client,
        name=f"large-output-{uuid.uuid4().hex[:8]}",
        harness="openai-agents",
        model="mock-model",
        profile="",
        prompt="Acknowledge.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    http_client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {"omnigent.wrapper": "claude-code-native-ui"}},
    ).raise_for_status()
    output = character * (MAX_SESSION_EVENT_REQUEST_BYTES // encoded_bytes + 1)
    records = [
        {
            "type": "user",
            "uuid": "tool-result",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "large-call", "content": output}
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "after-tool",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "AFTER_LARGE_TOOL"}],
            },
        },
    ]
    transcript = tmp_path / "session.jsonl"
    original = "".join(
        json.dumps(record, ensure_ascii=False) + "\n" for record in records
    ).encode()
    transcript.write_bytes(original)
    rejected = http_client.post(
        f"/v1/sessions/{session_id}/events",
        content=b" " * (MAX_SESSION_EVENT_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert rejected.status_code == 400, rejected.text
    assert "session event request exceeds the 10 MiB limit" in rejected.text
    config = tmp_path / "forwarder-input.json"
    config.write_text(
        json.dumps(
            {
                "server": str(http_client.base_url),
                "headers": dict(http_client.headers),
                "session_id": session_id,
                "transcript": str(transcript),
                "bridge": str(tmp_path / "bridge"),
                "receipt": str(tmp_path / "forwarder-receipt.json"),
            }
        )
    )
    script = """
import asyncio, json, sys
from pathlib import Path
import httpx
from omnigent.harnesses.claude_native import forwarder
config = json.loads(Path(sys.argv[1]).read_text())
async def run():
    bridge = Path(config["bridge"])
    state = forwarder._read_forward_state(bridge) or forwarder.TranscriptForwardState(
        transcript_path=Path(config["transcript"]), line_cursor=0, byte_offset=0)
    delivered = []
    async def capture(response):
        delivered.append({"bytes": len(response.request.content), "status": response.status_code})
    async with httpx.AsyncClient(base_url=config["server"], headers=config["headers"],
        trust_env=False, timeout=30, event_hooks={"response": [capture]}) as client:
        state = await forwarder._forward_available_items(
            client=client, session_id=config["session_id"],
            bridge_dir=bridge, agent_name="claude-native-ui", state=state,
            retry_tracker=forwarder._PostRetryTracker(), dedupe=forwarder._ForwardDedupeState())
    Path(config["receipt"]).write_text(json.dumps(
        {"byte_offset": state.byte_offset, "requests": delivered}))
asyncio.run(run())
"""
    for cycle in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script, str(config)],
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        (tmp_path / f"forwarder-{cycle}.log").write_text(result.stdout)
        assert result.returncode == 0, result.stdout
        receipt = json.loads((tmp_path / "forwarder-receipt.json").read_text())
        (tmp_path / f"forwarder-{cycle}-receipt.json").write_text(json.dumps(receipt, indent=2))
        assert receipt["byte_offset"] == len(original), receipt
        if cycle == 0:
            assert len(receipt["requests"]) == 2, receipt
            assert all(
                request["status"] == 202 and request["bytes"] <= MAX_SESSION_EVENT_REQUEST_BYTES
                for request in receipt["requests"]
            ), receipt
        else:
            assert receipt["requests"] == [], "a restarted forwarder must not duplicate delivery"
        items = http_client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
        items.raise_for_status()
        stored = items.json()["data"]
        (tmp_path / f"items-{cycle}.json").write_text(items.text)
        tool_items = [item for item in stored if item.get("type") == "function_call_output"]
        replies = [item for item in stored if item.get("role") == "assistant"]
        assert len(tool_items) == len(replies) == 1, stored
        assert "output truncated by omnigent" in tool_items[0]["output"]
        assert "AFTER_LARGE_TOOL" in json.dumps(replies[0]["content"])
        assert transcript.read_bytes() == original
        assert not (tmp_path / "bridge" / "dead_letter.jsonl").exists()


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
