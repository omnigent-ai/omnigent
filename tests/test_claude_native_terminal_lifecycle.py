"""Lifecycle regressions across transcript order, retries and Host upgrades."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import forwarder as f

pytestmark = pytest.mark.asyncio
T1, T2, T3 = ("2026-09-10T13:22:05.710Z", "2026-09-10T13:23:13.274Z", "2026-09-10T13:24:00Z")


def _append(path: Path, *rows: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)


def _terminal(*, task="worker", tool="tool_spawn", timestamp=T1, output="first done") -> dict:
    tool_tag = f"<tool-use-id>{tool}</tool-use-id>" if tool else ""
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": (
                f"<task-notification><task-id>{task}</task-id>{tool_tag}"
                f"<status>completed</status><result>{output}</result></task-notification>"
            ),
        },
    }


class _Scene:
    def __init__(self, path: Path):
        self.parent = path / "parent.jsonl"
        self.child = path / "parent/subagents/agent-worker.jsonl"
        self.child.parent.mkdir(parents=True)
        self.bridge = path / "bridge"
        self.events: list[dict] = []
        self.state = f.SubagentForwardState(subagents={})
        _append(
            self.parent,
            {
                "type": "assistant",
                "uuid": "spawn",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_spawn",
                            "name": "Agent",
                            "input": {"description": "Lifecycle test"},
                        }
                    ],
                },
            },
        )

    def register(self):
        self.child.touch(exist_ok=True)
        self.child.with_suffix(".meta.json").write_text(
            json.dumps(
                {
                    "agentType": "Explore",
                    "description": "Lifecycle test",
                    "toolUseId": "tool_spawn",
                }
            )
        )

    def reload(self, *, legacy=False):
        if legacy:
            state_file = self.bridge / "subagent_forwarder.json"
            data = json.loads(state_file.read_text())
            data.pop("terminal_evidence_version", None)
            data.pop("pending_terminal_tool_use_ids", None)
            data.pop("pending_terminal_parent_offsets", None)
            for row in data["subagents"].values():
                row.pop("resume_observed_at", None)
                row.pop("status_reconcile_pending", None)
                row.pop("terminal_evidence_pending", None)
                row.pop("tool_use_id_pending", None)
            state_file.write_text(json.dumps(data))
        self.state = f._read_subagent_forward_state(self.bridge)

    async def tick(self, *rows, fail_status=False, fail_items=False, replayed_items=False):
        _append(self.parent, *rows)

        def handle(request):
            body = json.loads(request.content)
            if isinstance(body, list):
                if fail_items:
                    return httpx.Response(500)
                return httpx.Response(
                    202,
                    json=[
                        {"item_id": f"item-{i}", "replayed": replayed_items}
                        for i in range(len(body))
                    ],
                )
            if body["type"] == "external_subagent_start":
                return httpx.Response(
                    202, json={"child_session_id": "conv_worker", "existing": False}
                )
            if body["type"] == "external_session_status":
                if fail_status:
                    return httpx.Response(500)
                self.events.append(body["data"])
            return httpx.Response(202, json={})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://test"
        ) as client:
            self.state = await f._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=self.bridge,
                transcript_path=self.parent,
                state=self.state,
                agent_name="claude-native-ui",
                start_retry_tracker=f._PostRetryTracker(base_delay_s=0),
                item_retry_tracker=f._PostRetryTracker(base_delay_s=0),
                status_retry_tracker=f._PostRetryTracker(base_delay_s=0),
            )

    def assert_completed(self, output):
        assert self.events, "The child completion must be published to the Server"
        assert self.events[-1]["status"] in {"completed", "idle"}
        assert self.events[-1]["output"] == output
        assert self.state.subagents["worker"].terminal_status == "completed"


@pytest.mark.parametrize("legacy", [False])
async def test_parked_terminal_retains_fallback_across_registration_and_restart(tmp_path, legacy):
    scene = _Scene(tmp_path)
    await scene.tick(_terminal(task="external-task"))
    scene.reload(legacy=legacy)
    scene.register()
    await scene.tick()
    scene.assert_completed("first done")
    assert not scene.state.pending_terminal_notifications


@pytest.mark.parametrize("parked", [False, True])
async def test_late_old_completion_cannot_replace_newer_result(tmp_path, parked):
    scene = _Scene(tmp_path)
    if not parked:
        scene.register()
    await scene.tick(_terminal(timestamp=T3, output="second done"), _terminal())
    if parked:
        scene.reload()
        scene.register()
        await scene.tick()
    scene.assert_completed("second done")
