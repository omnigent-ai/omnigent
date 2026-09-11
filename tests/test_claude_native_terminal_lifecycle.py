"""Lifecycle regressions across transcript order, retries and Host upgrades."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

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


def _resume(timestamp=T2) -> dict:
    return {
        "type": "user",
        "uuid": "resume",
        "timestamp": timestamp,
        "isSidechain": True,
        "isMeta": True,
        "origin": {"kind": "coordinator"},
        "message": {"role": "user", "content": "Continue the next part."},
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


async def test_resume_running_retries_after_host_restart_and_long_pause(tmp_path):
    scene = _Scene(tmp_path)
    scene.register()
    await scene.tick(_terminal())
    _append(scene.child, _resume())
    await scene.tick(fail_status=True)
    scene.reload()
    with patch.object(
        f.time, "time", return_value=scene.state.subagents["worker"].last_activity_ts + 60
    ):
        await scene.tick()
    assert scene.events[-1]["status"] == "running"


@pytest.mark.parametrize("completion_already_seen", [True, False])
async def test_older_completion_cannot_close_an_accepted_resume(tmp_path, completion_already_seen):
    scene = _Scene(tmp_path)
    scene.register()
    await scene.tick(*([_terminal()] if completion_already_seen else []))
    _append(scene.child, _resume())
    await scene.tick()
    scene.reload()
    await scene.tick(_terminal())
    assert scene.events[-1]["status"] == "running"
    assert scene.state.subagents["worker"].terminal_status is None


@pytest.mark.parametrize("tool", ["tool_spawn", None])
async def test_two_completed_runs_survive_one_parent_read(tmp_path, tool):
    scene = _Scene(tmp_path)
    scene.register()
    _append(scene.child, _resume())
    await scene.tick(
        _terminal(tool=tool), _terminal(tool=tool, timestamp=T3, output="second done")
    )
    scene.assert_completed("second done")


@pytest.mark.parametrize("legacy", [False, True])
async def test_parked_terminal_retains_fallback_across_registration_and_restart(tmp_path, legacy):
    scene = _Scene(tmp_path)
    await scene.tick(_terminal(task="external-task"))
    scene.reload(legacy=legacy)
    scene.register()
    await scene.tick()
    scene.assert_completed("first done")
    assert not scene.state.pending_terminal_notifications


@pytest.mark.parametrize("later_completion", [False, True])
async def test_host_upgrade_heals_consumed_legacy_evidence_without_new_records(
    tmp_path, later_completion
):
    scene = _Scene(tmp_path)
    scene.register()
    await scene.tick(_terminal())
    _append(scene.child, _resume())
    if later_completion:
        _append(scene.parent, _terminal(timestamp=T3, output="second done"))
    entry = scene.state.subagents["worker"]
    # State written by the previous Host after it consumed the resume and,
    # optionally, lost the later completion through its old dedupe rule.
    entry = replace(
        entry,
        byte_offset=scene.child.stat().st_size,
        terminal_status=None if later_completion else entry.terminal_status,
        terminal_observed_at=None if later_completion else entry.terminal_observed_at,
        last_status="running" if later_completion else entry.last_status,
    )
    scene.state = replace(
        scene.state, subagents={"worker": entry}, parent_byte_offset=scene.parent.stat().st_size
    )
    f._write_subagent_forward_state(scene.bridge, scene.state)
    scene.reload(legacy=True)
    scene.events.clear()
    await scene.tick()
    if later_completion:
        scene.assert_completed("second done")
    else:
        # A consumed cursor alone cannot distinguish a new prompt from an
        # acknowledged historical replay. Preserve the settled state.
        assert scene.events == []
        assert scene.state.subagents["worker"].terminal_status == "completed"


@pytest.mark.parametrize("legacy", [False, True])
async def test_foreground_resumed_agent_completion_uses_stable_task_id(tmp_path, legacy):
    scene = _Scene(tmp_path)
    scene.register()
    await scene.tick(_terminal())
    _append(scene.child, _resume())
    await scene.tick()
    _append(
        scene.parent,
        {
            "type": "user",
            "uuid": "foreground-result",
            "timestamp": T3,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool_resume", "content": "second done"}
                ],
            },
            "toolUseResult": {"status": "completed", "agentId": "worker"},
        },
    )
    if legacy:
        scene.state = replace(scene.state, parent_byte_offset=scene.parent.stat().st_size)
        f._write_subagent_forward_state(scene.bridge, scene.state)
        scene.reload(legacy=True)
    scene.events.clear()
    await scene.tick()
    scene.assert_completed("second done")
    if legacy and scene.events[-1]["status"] == "completed":
        assert scene.events[-1]["replayed"] is True


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


@pytest.mark.parametrize("dropped", [False, True])
async def test_upgrade_never_treats_an_unaccepted_prompt_as_a_resume(tmp_path, dropped):
    scene = _Scene(tmp_path)
    scene.register()
    await scene.tick(_terminal())
    _append(scene.child, _resume())
    if dropped:
        entry = scene.state.subagents["worker"]
        scene.state = replace(
            scene.state,
            subagents={
                "worker": replace(
                    entry,
                    byte_offset=scene.child.stat().st_size,
                    delivery_error=f._SUBAGENT_DROPPED_ITEM_REASON,
                )
            },
        )
    f._write_subagent_forward_state(scene.bridge, scene.state)
    scene.reload(legacy=True)
    scene.events.clear()
    await scene.tick(fail_items=True)
    assert scene.state.subagents["worker"].terminal_status == "completed"
    assert scene.state.subagents["worker"].resume_observed_at is None
    assert all(event["status"] != "running" for event in scene.events)


async def test_missing_old_peer_does_not_block_completion_recovery(tmp_path):
    scene = _Scene(tmp_path)
    scene.register()
    _append(scene.child, _resume("2026-09-10T13:20:00Z"))
    await scene.tick()
    _append(scene.parent, _terminal(), _terminal(task="gone", tool="gone", output="gone done"))
    gone = f.SubagentEntry(
        subagent_id="gone", child_conversation_id="conv_gone", byte_offset=1, last_status="running"
    )
    scene.state = replace(
        scene.state,
        subagents={**scene.state.subagents, "gone": gone},
        parent_byte_offset=scene.parent.stat().st_size,
    )
    f._write_subagent_forward_state(scene.bridge, scene.state)
    scene.reload(legacy=True)
    scene.events.clear()
    await scene.tick()
    scene.assert_completed("first done")
    assert scene.state.subagents["gone"].terminal_evidence_pending
    scene.reload()
    (scene.child.parent / "agent-gone.jsonl").write_text("{}\n")
    await scene.tick()
    assert scene.state.subagents["gone"].terminal_status == "completed"
    assert not scene.state.subagents["gone"].terminal_evidence_pending


async def test_upgrade_retries_late_metadata_and_preserves_historical_delivery(tmp_path):
    scene = _Scene(tmp_path)
    scene.register()
    _append(scene.child, _resume("2026-09-10T13:20:00Z"))
    await scene.tick()
    scene.child.with_suffix(".meta.json").unlink()
    _append(scene.parent, _terminal(task="external-task"))
    scene.state = replace(
        scene.state,
        subagents={"worker": replace(scene.state.subagents["worker"], tool_use_id=None)},
        parent_byte_offset=scene.parent.stat().st_size,
    )
    f._write_subagent_forward_state(scene.bridge, scene.state)
    scene.reload(legacy=True)
    scene.events.clear()
    await scene.tick()
    scene.reload()
    scene.register()
    await scene.tick()
    scene.assert_completed("first done")
    if scene.events[-1]["status"] == "completed":
        assert scene.events[-1]["replayed"] is True


async def test_old_delivery_error_cannot_hide_a_later_accepted_resume(tmp_path):
    scene = _Scene(tmp_path)
    scene.register()
    await scene.tick(_terminal())
    scene.state = replace(
        scene.state,
        subagents={
            "worker": replace(
                scene.state.subagents["worker"], delivery_error=f._SUBAGENT_DROPPED_ITEM_REASON
            )
        },
    )
    _append(scene.child, _resume())
    await scene.tick()
    assert scene.events[-1]["status"] == "running"
    scene.reload(legacy=True)
    scene.events.clear()
    await scene.tick()
    assert scene.state.subagents["worker"].terminal_status is None
    assert scene.state.subagents["worker"].last_status == "running"
    assert scene.events == []
