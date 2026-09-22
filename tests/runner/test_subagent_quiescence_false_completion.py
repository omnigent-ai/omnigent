"""Transcript inactivity must stay separate from authoritative completion.

A lull emits subagent.status with idle=true; genuine external_session_status
idle still delivers the child's result to the parent inbox. Server route tests
cover publishing the observation without forwarding it to the runner.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native.forwarder import (
    SubagentEntry,
    SubagentForwardState,
    _forward_available_subagents,
    _PostRetryTracker,
)
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_quiesce"
CHILD_SESSION_ID = "conv_child_quiesce"
SUBAGENT_ID = "afalsecompletelull"


@pytest.fixture
def _clean_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide sub-agent / inbox registries."""
    saved = (
        dict(runner_app._subagent_work_by_child),
        {k: set(v) for k, v in runner_app._subagent_work_by_parent.items()},
        dict(runner_app._session_inboxes_ref),
        set(runner_app._drained_delivered_subagent_children),
    )
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    runner_app._drained_delivered_subagent_children.clear()
    try:
        yield
    finally:
        runner_app._subagent_work_by_child.clear()
        runner_app._subagent_work_by_child.update(saved[0])
        runner_app._subagent_work_by_parent.clear()
        runner_app._subagent_work_by_parent.update(saved[1])
        runner_app._session_inboxes_ref.clear()
        runner_app._session_inboxes_ref.update(saved[2])
        runner_app._drained_delivered_subagent_children.clear()
        runner_app._drained_delivered_subagent_children.update(saved[3])


class _CapturingForwarderClient:
    """Fake Omnigent HTTP client recording the forwarder's status posts.

    Stands in for the AP-server client the forwarder posts to; every
    status payload is captured so the test can distinguish idle observations
    from authoritative completion events.
    """

    def __init__(self) -> None:
        self.status_posts: list[dict[str, Any]] = []

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {}

        def raise_for_status(self) -> None:
            return None

    async def post(self, url: str, **kwargs: Any) -> Any:
        body = kwargs.get("json") or {}
        if body.get("type") in {"external_session_status", "subagent.status"}:
            self.status_posts.append(body)
        return self._Resp()


class _SnapshotServerClient(NullServerClient):
    """Server client returning a claude-native child snapshot for GET …/{child}."""

    class _ChildResp:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith(CHILD_SESSION_ID):
            return self._ChildResp(
                {
                    "id": CHILD_SESSION_ID,
                    "agent_id": "ag_child_quiesce",
                    "agent_name": "claude-native",
                    "sub_agent_name": "general-purpose",
                    "parent_session_id": PARENT_SESSION_ID,
                    "created_at": 0,
                    "workspace": None,
                }
            )
        if url.rstrip("/").endswith("/items"):
            return self._ChildResp({"data": [], "has_more": False})
        return self._Response()


async def _run_forwarder_quiescence_tick(tmp_path: Path) -> list[dict[str, Any]]:
    """Run one real forwarder tick over a still-running sub-agent's lull.

    Lays out the on-disk shape Claude Code produces for a Task-tool sub-agent
    (``<session>/subagents/agent-<id>.meta.json`` + ``agent-<id>.jsonl``) with
    a transcript that has been quiet for 60 s but carries no done record, then
    runs :func:`_forward_available_subagents` for real.

    :param tmp_path: Pytest tmp dir for the bridge dir and transcript tree.
    :returns: The status event payloads posted during the tick.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "project" / "parent-session.jsonl"
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / f"agent-{SUBAGENT_ID}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "long-running background research task",
                "toolUseId": "toolu_longtask",
            }
        ),
        encoding="utf-8",
    )
    # The sub-agent's transcript exists but has produced nothing new this
    # tick — the mid-task lull (e.g. a long tool call still executing).
    (subagents_dir / f"agent-{SUBAGENT_ID}.jsonl").write_text("", encoding="utf-8")

    state = SubagentForwardState(
        subagents={
            SUBAGENT_ID: SubagentEntry(
                subagent_id=SUBAGENT_ID,
                child_conversation_id=CHILD_SESSION_ID,
                byte_offset=0,
                seen_source_ids=(),
                # Last item flowed 60 s ago — beyond the 5 s quiescence
                # window — while the sub-agent is still running.
                last_activity_ts=time.time() - 60.0,
                last_status="running",
            )
        }
    )
    client = _CapturingForwarderClient()
    await _forward_available_subagents(
        client=client,  # type: ignore[arg-type]
        parent_session_id=PARENT_SESSION_ID,
        bridge_dir=bridge_dir,
        transcript_path=transcript_path,
        state=state,
        agent_name="claude-native",
        start_retry_tracker=_PostRetryTracker(),
        item_retry_tracker=_PostRetryTracker(),
        status_retry_tracker=_PostRetryTracker(),
    )
    return client.status_posts


async def _post_status_to_runner(
    status: str,
    *,
    output: str | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """POST an ``external_session_status`` edge for the child to the real runner app.

    Mirrors what the AP server's relay does with a forwarder status post.
    The child is registered as in-flight sub-agent work for the parent, and
    the parent's inbox queue is seeded, exactly as after a live Task spawn.

    :param status: Status value the forwarder emitted, e.g. ``"idle"``.
    :param output: Optional forwarded output text.
    :returns: ``(http_status, drained_parent_inbox_items)``.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="general-purpose",
        title="long-running background research task",
    )

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="general-purpose",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
    )

    data: dict[str, Any] = {"status": status}
    if output is not None:
        data["output"] = output
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={"type": "external_session_status", "data": data},
        )

    inbox = runner_app._session_inboxes_ref.get(PARENT_SESSION_ID)
    items: list[dict[str, Any]] = []
    if inbox is not None:
        while not inbox.empty():
            items.append(inbox.get_nowait())
    return resp.status_code, items


@pytest.mark.asyncio
async def test_quiescence_lull_does_not_deliver_false_completion(
    tmp_path: Path,
) -> None:
    """A mid-task lull must emit an observation without a terminal status."""
    status_posts = await _run_forwarder_quiescence_tick(tmp_path)
    assert status_posts == [{"type": "subagent.status", "data": {"idle": True}}]


@pytest.mark.asyncio
async def test_explicit_idle_edge_still_delivers_completion(
    _clean_registry: None,
) -> None:
    """A genuine terminal ``idle`` edge must still wake the parent inbox.

    Guards the legitimate completion contract so the quiescence fix cannot
    simply suppress every ``idle`` edge: an authoritative terminal ``idle``
    (with the child's final output attached) must keep delivering a
    ``completed`` item to the parent's inbox.
    """
    http, items = await _post_status_to_runner(
        "idle",
        output="task complete: all findings summarized",
    )

    assert http == 204, f"genuine idle edge returned unexpected HTTP {http}"
    assert items, (
        "A genuine terminal idle edge delivered nothing to the parent inbox — "
        "the quiescence fix must not suppress real completions."
    )
    assert items[0]["status"] == "completed"
    assert items[0]["conversation_id"] == CHILD_SESSION_ID
    assert "task complete" in items[0].get("output", "")
