"""Subagent discovery tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _seed_subagent_on_disk,
    _start_recording_server_with_responses,
    _wait_for_json_state,
)


@pytest.fixture(autouse=True)
def _fresh_spawn_id_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with an empty spawn-id memo so entries never leak between tests."""
    monkeypatch.setattr(forwarder, "_SPAWN_TOOL_USE_ID_CACHE", {})


def _count_transcript_scans(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every transcript the forwarder parses for spawn ids from now on."""
    scans: list[Path] = []
    real_scan = forwarder._tool_use_ids_in_transcript

    def counting_scan(path: Path, *, include_sidechains: bool) -> set[str] | None:
        scans.append(path)
        return real_scan(path, include_sidechains=include_sidechains)

    monkeypatch.setattr(forwarder, "_tool_use_ids_in_transcript", counting_scan)
    return scans


async def test_subagent_watcher_registers_a_task_named_spawn(
    tmp_path: Path,
) -> None:
    """A spawn recorded under the legacy ``Task`` name still registers.

    ``Task`` was renamed to ``Agent`` in CLI 2.1.63 but remains a supported
    alias, so a transcript may carry either name. Correlation gates all
    registration, so missing the alias would strand every such sub-agent.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-worker",
        agent_type="Explore",
        description="spawned via the Task alias",
        tool_use_id="toolu_task",
        spawn_tool_name="Task",
    )

    start_paths: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") != "external_subagent_start":
            return httpx.Response(202, json={})
        subagent_id = body["data"]["subagent_id"]
        start_paths[subagent_id] = request.url.path
        return httpx.Response(
            202,
            json={"queued": False, "child_session_id": f"conv_{subagent_id}"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert start_paths == {"a-worker": "/v1/sessions/conv_root/events"}
    assert state.subagents["a-worker"].child_conversation_id == "conv_a-worker"
    assert state.subagents["a-worker"].parent_subagent_id is None


@pytest.mark.parametrize(
    "name,description,label",
    [
        ("differ", "Version-diff pass on findings", "differ: Version-diff pass on findings"),
        ("", "Version-diff pass on findings", "Version-diff pass on findings"),
        ("differ", "", "differ"),
    ],
)
async def test_subagent_watcher_registers_an_in_process_teammate_under_the_lead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    description: str,
    label: str,
) -> None:
    """An agent-team teammate has no spawn tool call, so it attaches to the lead.

    Its meta carries ``taskKind: "in_process_teammate"`` and no ``toolUseId``.
    Registration must not wait on spawn correlation, the label leads with the
    teammate's name when both parts exist, and a meta missing ``toolUseId``
    for any other reason stays unregistered.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True)
    metas = {
        "teammate": {
            "agentType": "differ",
            "description": description,
            "name": name,
            "taskKind": "in_process_teammate",
            "teamName": "session-c13bb742",
        },
        "malformed": {"agentType": "Explore", "description": "no spawn id"},
    }
    for subagent_id, meta in metas.items():
        (subagents_dir / f"agent-{subagent_id}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        (subagents_dir / f"agent-{subagent_id}.jsonl").write_text("", encoding="utf-8")

    def fail_correlation(*_args: Any) -> dict[str, str | None]:
        raise AssertionError("a teammate alone must not trigger a transcript scan")

    monkeypatch.setattr(forwarder, "_subagent_parents_by_tool_use", fail_correlation)
    starts: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") != "external_subagent_start":
            return httpx.Response(202, json={})
        starts.append((request.url.path, body["data"]))
        return httpx.Response(
            202,
            json={"queued": False, "child_session_id": f"conv_{body['data']['subagent_id']}"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert starts == [
        (
            "/v1/sessions/conv_root/events",
            {
                "subagent_id": "teammate",
                "agent_type": "differ",
                "description": label,
                "tool_use_id": "teammate:teammate",
            },
        )
    ]
    assert state.subagents["teammate"].child_conversation_id == "conv_teammate"
    assert state.subagents["teammate"].parent_subagent_id is None
    assert "malformed" not in state.subagents


async def test_subagent_watcher_registers_a_teammate_spawned_subagent_under_the_teammate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Task sub-agent a teammate spawns registers under the teammate's child session.

    The teammate's meta has no spawn id, so its transcript contributes every
    spawn it owns to correlation, which runs once for the Task meta; the
    teammate registers first so its child can be parented to it.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "agent-t-mate.meta.json").write_text(
        json.dumps(
            {
                "agentType": "differ",
                "description": "Version-diff pass on findings",
                "name": "differ",
                "taskKind": "in_process_teammate",
            }
        ),
        encoding="utf-8",
    )
    teammate_transcript = subagents_dir / "agent-t-mate.jsonl"
    teammate_transcript.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-helper",
        agent_type="Explore",
        description="helper spawned by the teammate",
        tool_use_id="toolu_helper",
        spawn_transcript_path=teammate_transcript,
        spawn_tool_name="Task",
    )
    correlations: list[Path] = []
    real_correlation = forwarder._subagent_parents_by_tool_use

    def counting_correlation(*args: Any) -> dict[str, str | None]:
        correlations.append(args[0])
        return real_correlation(*args)

    monkeypatch.setattr(forwarder, "_subagent_parents_by_tool_use", counting_correlation)
    start_paths: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") != "external_subagent_start":
            return httpx.Response(202, json={})
        subagent_id = body["data"]["subagent_id"]
        start_paths[subagent_id] = request.url.path
        return httpx.Response(
            202,
            json={"queued": False, "child_session_id": f"conv_{subagent_id}"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert start_paths == {
        "t-mate": "/v1/sessions/conv_root/events",
        "a-helper": "/v1/sessions/conv_t-mate/events",
    }
    assert state.subagents["t-mate"].parent_subagent_id is None
    assert state.subagents["a-helper"].parent_subagent_id == "t-mate"
    assert correlations == [transcript_path]


_TEAMMATE_META = {
    "agentType": "differ",
    "description": "Version-diff pass on findings",
    "name": "differ",
    "taskKind": "in_process_teammate",
    "teamName": "session-c13bb742",
}


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            _TEAMMATE_META,
            {
                "agentType": "differ",
                "description": "Version-diff pass on findings",
                "toolUseId": "",
                "name": "differ",
            },
        ),
        (
            {**_TEAMMATE_META, "name": "  "},
            {
                "agentType": "differ",
                "description": "Version-diff pass on findings",
                "toolUseId": "",
                "name": "",
            },
        ),
        (
            {**_TEAMMATE_META, "description": ""},
            {"agentType": "differ", "description": "", "toolUseId": "", "name": "differ"},
        ),
        # A teammate that also carries a spawn id correlates like a Task sub-agent.
        (
            {**_TEAMMATE_META, "toolUseId": "toolu_spawn"},
            {
                "agentType": "differ",
                "description": "Version-diff pass on findings",
                "toolUseId": "toolu_spawn",
                "name": "differ",
            },
        ),
        (
            {"agentType": "Explore", "description": "task", "toolUseId": "toolu_task"},
            {"agentType": "Explore", "description": "task", "toolUseId": "toolu_task", "name": ""},
        ),
        ({"agentType": "Explore", "description": "no spawn id"}, None),
        ({**_TEAMMATE_META, "taskKind": "background"}, None),
    ],
)
def test_read_subagent_meta_returns_fields_as_written(
    tmp_path: Path, raw: dict[str, Any], expected: dict[str, str] | None
) -> None:
    meta_path = tmp_path / "agent-x.meta.json"
    meta_path.write_text(json.dumps(raw), encoding="utf-8")
    assert forwarder._read_subagent_meta(meta_path) == expected


async def test_subagent_watcher_posts_external_subagent_start_for_new_meta(
    tmp_path: Path,
) -> None:
    """
    When a new ``agent-<id>.meta.json`` appears under the parent's
    ``subagents/`` dir, the forwarder POSTs ``external_subagent_start``
    with the meta fields and persists the returned ``child_session_id``
    in its durable cursor.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a5c7eff",
        agent_type="Explore",
        description="Trace the auth flow",
        tool_use_id="toolu_xyz",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    def response_for(body: dict[str, Any]) -> dict[str, Any]:
        """Return a minted child id for the subagent_start event.

        :param body: Decoded request body.
        :returns: Response payload.
        """
        if body.get("type") == "external_subagent_start":
            return {"queued": False, "child_session_id": "conv_child_alpha"}
        return {}

    server, _thread, base_url = _start_recording_server_with_responses(response_for)
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # Skip the transcript-status / mirror PATCHes that may land
        # before our event, and stop at the first
        # ``external_subagent_start`` we see.
        start_req: dict[str, Any] | None = None
        for _ in range(20):
            req = await _get_recorded_request(server)
            if req["body"].get("type") == "external_subagent_start":
                start_req = req
                break
        assert start_req is not None, "forwarder did not POST external_subagent_start"
        assert start_req["path"] == "/v1/sessions/conv_parent/events"
        assert start_req["body"]["data"] == {
            "subagent_id": "a5c7eff",
            "agent_type": "Explore",
            "description": "Trace the auth flow",
            "tool_use_id": "toolu_xyz",
        }
        # The cursor persists the returned child id so a forwarder
        # restart won't re-mint a duplicate row. Wait on it BEFORE
        # cancelling so the writer's ``asyncio.to_thread`` has time
        # to flush — cancellation can interrupt the inflight write.
        cursor = await _wait_for_json_state(
            bridge_dir / "subagent_forwarder.json",
            lambda payload: "a5c7eff" in payload.get("subagents", {}),
        )
        assert cursor["subagents"]["a5c7eff"]["child_conversation_id"] == "conv_child_alpha"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()


async def test_subagent_watcher_preserves_nested_parent_graph_across_restart(
    tmp_path: Path,
) -> None:
    """Nested Claude agents register under their immediate Omnigent parent."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    parent_transcript = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="z-parent",
        agent_type="general-purpose",
        description="parent worker",
        tool_use_id="toolu_parent",
    )
    child_transcript = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-child",
        agent_type="general-purpose",
        description="nested child",
        tool_use_id="toolu_child",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "nested-child-output",
                "message": {"role": "assistant", "content": "working"},
            }
        ],
        spawn_transcript_path=parent_transcript,
    )
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "isSidechain": True,
                    "type": "assistant",
                    "uuid": "mirrored-nested-spawn",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_child",
                                "name": "Agent",
                                "input": {"description": "nested child"},
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    start_paths: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") != "external_subagent_start":
            return httpx.Response(202, json={})
        subagent_id = body["data"]["subagent_id"]
        start_paths[subagent_id] = request.url.path
        return httpx.Response(
            202,
            json={"queued": False, "child_session_id": f"conv_{subagent_id}"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

        assert start_paths == {
            "z-parent": "/v1/sessions/conv_root/events",
            "a-child": "/v1/sessions/conv_z-parent/events",
        }
        assert state.subagents["z-parent"].parent_subagent_id is None
        assert state.subagents["a-child"].parent_subagent_id == "z-parent"

        reconstructed = forwarder._read_subagent_forward_state(bridge_dir)
        assert reconstructed == state

        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id="b-grandchild",
            agent_type="Explore",
            description="second nested level",
            tool_use_id="toolu_grandchild",
            spawn_transcript_path=child_transcript,
        )
        restarted = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=reconstructed,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert start_paths["b-grandchild"] == "/v1/sessions/conv_a-child/events"
    assert restarted.subagents["b-grandchild"].parent_subagent_id == "a-child"


async def test_subagent_watcher_parks_child_of_a_parked_parent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A child whose parent was parked is parked too, not retried forever.

    When a parent's registration exhausts its retries it is parked with an empty
    ``child_conversation_id`` — its Omnigent conversation will never exist. A
    child that resolves to that parent can therefore never attach; it must be
    parked (and logged) rather than silently re-resolved on every poll.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    parent_transcript = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="z-parent",
        agent_type="general-purpose",
        description="parent worker",
        tool_use_id="toolu_parent",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-child",
        agent_type="general-purpose",
        description="nested child",
        tool_use_id="toolu_child",
        spawn_transcript_path=parent_transcript,
    )
    # The parent is already parked on disk (empty child id): its registration
    # exhausted retries on an earlier tick.
    parked = forwarder.SubagentForwardState(
        subagents={
            "z-parent": forwarder.SubagentEntry(
                subagent_id="z-parent",
                child_conversation_id="",
                parent_subagent_id=None,
            )
        }
    )

    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if json.loads(request.content).get("type") == "external_subagent_start":
            starts += 1
        return httpx.Response(202, json={})

    caplog.set_level(logging.WARNING, logger="omnigent.harnesses.claude_native.forwarder")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=parked,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    # The child was parked, not registered: no start POST, empty child id,
    # and the parked entry survives a state round-trip.
    assert starts == 0
    assert state.subagents["a-child"].child_conversation_id == ""
    assert state.subagents["a-child"].parent_subagent_id == "z-parent"
    assert forwarder._read_subagent_forward_state(bridge_dir) == state
    assert "whose parent was dropped" in caplog.text

    # No dead letter: a replay would re-post the child under the root session and
    # flatten the hierarchy, so the child is parked (WARNING only), not recorded
    # for replay.
    assert not (bridge_dir / "dead_letter.jsonl").exists()


async def test_subagent_watcher_defers_a_spawn_owned_by_two_transcripts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spawn id claimed by two agent transcripts is dropped as ambiguous.

    Attribution is trustworthy only when a spawn `tool_use` id has a single
    owner. If the same id appears in two `agent-*.jsonl` transcripts, the owner
    can't be resolved, so it must be dropped (not guessed) and the agent
    deferred rather than mis-attributed.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    # Seed a normal agent (spawn lands in the root transcript, owner=None); then
    # write the SAME spawn tool-use id into an agent transcript too, so the id
    # resolves to two conflicting owners (root and that agent) and is dropped.
    jsonl_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-worker",
        agent_type="Explore",
        description="ambiguous spawn",
        tool_use_id="toolu_dup",
    )
    other_owner = jsonl_path.parent / "agent-owner-two.jsonl"
    other_owner.write_text(
        json.dumps(
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "dup-spawn",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_dup", "name": "Agent"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if json.loads(request.content).get("type") == "external_subagent_start":
            starts += 1
        return httpx.Response(202, json={})

    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert starts == 0
    assert "a-worker" not in state.subagents
    assert "no resolved parent" in caplog.text


async def test_subagent_watcher_defers_and_logs_when_no_transcript_owns_the_spawn(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A meta whose spawn record no transcript owns is deferred, not registered.

    Claude can flush ``agent-<id>.meta.json`` before the spawning ``tool_use``
    record lands in a transcript. The watcher must skip such an agent (retry next
    tick) and log the miss so a spawn record that never arrives is diagnosable.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    (subagents_dir / "agent-orphan.meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": "spawn record not flushed yet",
                "toolUseId": "toolu_missing",
            }
        ),
        encoding="utf-8",
    )
    (subagents_dir / "agent-orphan.jsonl").write_text("", encoding="utf-8")

    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if json.loads(request.content).get("type") == "external_subagent_start":
            starts += 1
        return httpx.Response(202, json={})

    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert starts == 0
    assert "orphan" not in state.subagents
    assert "no resolved parent" in caplog.text
    assert "toolu_missing" in caplog.text


def _batch_safe_handler(
    response_for: Callable[[dict[str, Any]], dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Mock transport that answers single events and event-array batches alike."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        response = (
            [response_for(item) for item in body] if isinstance(body, list) else response_for(body)
        )
        return httpx.Response(202, json=response)

    return handler


def _observe_subagent_scans(
    monkeypatch: pytest.MonkeyPatch,
    response_for: Callable[[dict[str, Any]], dict[str, Any]],
) -> asyncio.Event:
    """Record HTTP calls and signal completion of two real child-history scans."""
    completed = asyncio.Event()
    scans = 0
    original = forwarder._forward_available_subagents

    async def scan(**kwargs: Any) -> Any:
        nonlocal scans
        state = await original(**kwargs)
        scans += 1
        if scans >= 2:
            completed.set()
        return state

    handler = _batch_safe_handler(response_for)

    @contextlib.asynccontextmanager
    async def open_mock_client(*_args: Any, **_kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://ap"
        ) as client:
            yield client

    monkeypatch.setattr(forwarder, "_forward_available_subagents", scan)
    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_mock_client)
    return completed


async def test_subagent_watcher_skips_subagents_already_in_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    On forwarder restart, sub-agents already in
    ``subagent_forwarder.json`` are NOT re-registered (no second
    ``external_subagent_start`` POST), teammates included. This is the
    idempotency contract the cursor file is for — without it, a forwarder
    crash-loop would mint a new child Conversation per restart.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="c0ldc4t",
        agent_type="Explore",
        description="post-restart sub-agent",
        tool_use_id="toolu_qqq",
    )
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    (subagents_dir / "agent-t-mate.meta.json").write_text(
        json.dumps(_TEAMMATE_META), encoding="utf-8"
    )
    (subagents_dir / "agent-t-mate.jsonl").write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Pre-seed the cursor as if a previous forwarder ran already.
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "subagent_forwarder.json").write_text(
        json.dumps(
            {
                "subagents": {
                    "c0ldc4t": {
                        "child_conversation_id": "conv_child_existing",
                        "byte_offset": 0,
                        "last_activity_ts": None,
                        "last_status": None,
                    },
                    "t-mate": {
                        "child_conversation_id": "conv_teammate_existing",
                        "byte_offset": 0,
                        "last_activity_ts": None,
                        "last_status": None,
                    },
                },
                "updated_at": 0,
            }
        ),
        encoding="utf-8",
    )

    starts: list[dict[str, Any]] = []

    def response_for(body: dict[str, Any]) -> dict[str, Any]:
        """Capture any start events and fail the test loudly.

        :param body: Decoded request body.
        :returns: Response payload (unused, since we don't expect a
            start event in this scenario).
        """
        if body.get("type") == "external_subagent_start":
            starts.append(body)
            return {"queued": False, "child_session_id": "conv_unexpected"}
        return {}

    scanned = _observe_subagent_scans(monkeypatch, response_for)
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url="http://ap",
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        await asyncio.wait_for(scanned.wait(), timeout=5.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert starts == [], (
        f"forwarder re-registered a sub-agent that was already in state: {starts!r}"
    )


async def test_subagent_watcher_preserves_parked_sentinel_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A sub-agent that exhausted its permanent-failure budget is "parked"
    by writing an empty ``child_conversation_id`` sentinel into the
    cursor. On restart we must round-trip that sentinel — otherwise the
    parked sub-agent silently disappears from state and the next tick
    retries it (defeating the failure-budget cap).
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="parked-cat",
        agent_type="Explore",
        description="exhausted start retries last time",
        tool_use_id="toolu_parked",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Pre-seed the cursor with a parked entry — empty child id is the
    # sentinel ``_forward_available_subagents`` writes on exhaustion.
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "subagent_forwarder.json").write_text(
        json.dumps(
            {
                "subagents": {
                    "parked-cat": {
                        "child_conversation_id": "",
                        "byte_offset": 0,
                        "last_activity_ts": None,
                        "last_status": None,
                    }
                },
                "updated_at": 0,
            }
        ),
        encoding="utf-8",
    )

    starts: list[dict[str, Any]] = []

    def response_for(body: dict[str, Any]) -> dict[str, Any]:
        """Record any start POSTs — none should arrive for the parked id.

        :param body: Decoded request body.
        :returns: Response payload.
        """
        if body.get("type") == "external_subagent_start":
            starts.append(body)
            return {"queued": False, "child_session_id": "conv_should_not_be_used"}
        return {}

    scanned = _observe_subagent_scans(monkeypatch, response_for)
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url="http://ap",
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        await asyncio.wait_for(scanned.wait(), timeout=5.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert starts == [], f"forwarder retried a parked sub-agent after restart: {starts!r}"


def _spawn_record(tool_use_id: str, description: str, *, sidechain: bool) -> dict[str, Any]:
    """An assistant transcript row carrying one Agent spawn ``tool_use`` block."""
    return {
        "isSidechain": sidechain,
        "type": "assistant",
        "uuid": f"spawn-{tool_use_id}",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Agent",
                    "input": {"description": description},
                }
            ],
        },
    }


def _inherited_spawn_record(tool_use_id: str, description: str) -> dict[str, Any]:
    """A fork's own transcript row: a sidechain copy of the Agent record that
    spawned it, inherited when the fork cloned the parent conversation."""
    return _spawn_record(tool_use_id, description, sidechain=True)


def test_fork_inherited_spawn_record_resolves_to_the_real_parent(
    tmp_path: Path,
) -> None:
    """A fork's inherited spawn copy must not strand its own spawn id.

    Claude's ``fork`` agent clones the parent conversation, so the fork's own
    ``agent-<id>.jsonl`` carries a sidechain copy of the Agent/Task record that
    spawned it. The spawn id must still resolve to its real issuer (the root
    transcript, ``None`` here) and not be dropped as ambiguous — being dropped
    is what strands the fork with no parent.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-fork",
        agent_type="fork",
        description="continue in the background",
        tool_use_id="toolu_fork",
        transcript_records=[_inherited_spawn_record("toolu_fork", "continue in the background")],
    )

    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    owners = forwarder._subagent_parents_by_tool_use(transcript_path, subagents_dir)

    assert "toolu_fork" in owners, "fork spawn id was dropped as ambiguous"
    assert owners["toolu_fork"] is None, "fork should attach to the root transcript"


async def test_fork_subagent_inheriting_its_own_spawn_record_registers(
    tmp_path: Path,
) -> None:
    """End to end: a background ``fork`` sub-agent registers as a child row.

    Before the fix the fork's inherited copy of its own spawn record made its
    spawn id ambiguous, so correlation dropped it and the fork never appeared
    in the Agents rail — the OMNI-11924 symptom. A non-inheriting sub-agent
    (e.g. ``general-purpose``) was unaffected and is covered by the tests
    above.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-fork",
        agent_type="fork",
        description="continue in the background",
        tool_use_id="toolu_fork",
        transcript_records=[_inherited_spawn_record("toolu_fork", "continue in the background")],
    )

    start_paths: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") != "external_subagent_start":
            return httpx.Response(202, json={})
        subagent_id = body["data"]["subagent_id"]
        start_paths[subagent_id] = request.url.path
        return httpx.Response(
            202,
            json={"queued": False, "child_session_id": f"conv_{subagent_id}"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_root",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert start_paths == {"a-fork": "/v1/sessions/conv_root/events"}
    assert state.subagents["a-fork"].child_conversation_id == "conv_a-fork"
    assert state.subagents["a-fork"].parent_subagent_id is None


@pytest.mark.parametrize("spawn_owner", ["root", "helper"])
async def test_spawn_record_landing_after_idle_polls_registers_the_subagent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawn_owner: str,
) -> None:
    """Idle polls reuse unchanged transcripts; a later root or helper spawn registers."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    helper_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-finished",
        agent_type="general-purpose",
        description="finished helper",
        tool_use_id="toolu_finished",
    )
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    (subagents_dir / "agent-a-late.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "spawn record lands later",
                "toolUseId": "toolu_late",
            }
        ),
        encoding="utf-8",
    )

    def response_for(item: dict[str, Any]) -> dict[str, Any]:
        if item.get("type") != "external_subagent_start":
            return {"item_id": "ack"}
        return {"queued": False, "child_session_id": f"conv_{item['data']['subagent_id']}"}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_batch_safe_handler(response_for)),
        base_url="http://ap",
    ) as client:
        poll_kwargs: dict[str, Any] = {
            "client": client,
            "parent_session_id": "conv_root",
            "bridge_dir": bridge_dir,
            "transcript_path": transcript_path,
            "agent_name": "claude-native-ui",
            "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        }
        scans = _count_transcript_scans(monkeypatch)
        state = await forwarder._forward_available_subagents(
            state=forwarder.SubagentForwardState(subagents={}), **poll_kwargs
        )
        assert state.subagents["a-finished"].child_conversation_id == "conv_a-finished"
        assert scans == [transcript_path, helper_path]
        scans.clear()
        for _ in range(3):
            state = await forwarder._forward_available_subagents(state=state, **poll_kwargs)
            assert set(state.subagents) == {"a-finished"}
        assert scans == [], "idle polls re-scanned unchanged transcripts"

        spawn_path = transcript_path if spawn_owner == "root" else helper_path
        with spawn_path.open("a", encoding="utf-8") as handle:
            record = _spawn_record(
                "toolu_late", "spawn record lands later", sidechain=spawn_owner == "helper"
            )
            handle.write(json.dumps(record) + "\n")
        state = await forwarder._forward_available_subagents(state=state, **poll_kwargs)

    assert scans == [spawn_path], "only the changed transcript should be re-scanned"
    late = state.subagents["a-late"]
    assert late.child_conversation_id == "conv_a-late"
    assert late.parent_subagent_id == (None if spawn_owner == "root" else "a-finished")


def test_spawn_id_memo_rescans_only_changed_files_and_stays_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged transcript is served from the memo; a hit refreshes its recency;
    the least recently used entry is evicted past the cap; a vanished file is forgotten."""
    scans = _count_transcript_scans(monkeypatch)
    monkeypatch.setattr(forwarder, "_SPAWN_TOOL_USE_ID_CACHE_MAX_ENTRIES", 2)
    paths = []
    for index in range(3):
        path = tmp_path / f"transcript-{index}.jsonl"
        path.write_text(
            json.dumps(_spawn_record(f"toolu_{index}", "spawn", sidechain=False)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    first = forwarder._spawn_tool_use_ids_cached(paths[0], include_sidechains=False)
    assert first == {"toolu_0"}
    assert forwarder._spawn_tool_use_ids_cached(paths[0], include_sidechains=False) == first
    assert scans == [paths[0]], "an unchanged transcript was rescanned"

    with paths[0].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_spawn_record("toolu_0b", "spawn", sidechain=False)) + "\n")
    assert forwarder._spawn_tool_use_ids_cached(paths[0], include_sidechains=False) == {
        "toolu_0",
        "toolu_0b",
    }
    assert scans == [paths[0], paths[0]]

    # A same-size rewrite moves only the mtime, which must still invalidate the memo.
    before = paths[0].stat()
    paths[0].write_text(
        paths[0].read_text(encoding="utf-8").replace("toolu_0b", "toolu_0c"), encoding="utf-8"
    )
    os.utime(paths[0], ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    assert paths[0].stat().st_size == before.st_size
    assert forwarder._spawn_tool_use_ids_cached(paths[0], include_sidechains=False) == {
        "toolu_0",
        "toolu_0c",
    }
    assert scans == [paths[0]] * 3

    forwarder._spawn_tool_use_ids_cached(paths[1], include_sidechains=False)
    # Hitting the older entry makes it the most recently used, so the cap evicts
    # the untouched one instead.
    forwarder._spawn_tool_use_ids_cached(paths[0], include_sidechains=False)
    forwarder._spawn_tool_use_ids_cached(paths[2], include_sidechains=False)
    assert set(forwarder._SPAWN_TOOL_USE_ID_CACHE) == {paths[0], paths[2]}, (
        "the least recently used entry was not the one evicted past the cap"
    )
    assert scans == [paths[0]] * 3 + [paths[1], paths[2]]

    paths[2].unlink()
    assert forwarder._spawn_tool_use_ids_cached(paths[2], include_sidechains=False) == frozenset()
    assert set(forwarder._SPAWN_TOOL_USE_ID_CACHE) == {paths[0]}


def test_transient_read_failure_is_not_memoized_as_an_empty_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that fails after a successful stat must not be remembered as a
    transcript without spawns; the next poll re-reads the unchanged file and
    finds the parent."""
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-child",
        agent_type="general-purpose",
        description="child whose parent read fails once",
        tool_use_id="toolu_child",
    )
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    real_read_text = Path.read_text
    failures_left = [1]

    def flaky_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == transcript_path and failures_left[0]:
            failures_left[0] -= 1
            raise OSError(errno.EIO, "transient read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    assert forwarder._subagent_parents_by_tool_use(transcript_path, subagents_dir) == {}
    assert transcript_path not in forwarder._SPAWN_TOOL_USE_ID_CACHE
    assert forwarder._subagent_parents_by_tool_use(transcript_path, subagents_dir) == {
        "toolu_child": None
    }
