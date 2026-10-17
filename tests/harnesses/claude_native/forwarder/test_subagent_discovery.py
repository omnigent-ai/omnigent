"""Subagent discovery tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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
    _count_transcript_scans,
    _get_recorded_request,
    _poll_subagents,
    _registering_handler,
    _seed_subagent_on_disk,
    _set_aside_warnings,
    _start_recording_server_with_responses,
    _wait_for_json_state,
)


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A meta whose spawn record no transcript owns is deferred, then set aside.

    Claude can flush ``agent-<id>.meta.json`` before the spawning ``tool_use``
    record lands in a transcript. The watcher must skip such an agent (retry next
    tick) and log the miss so a spawn record that never arrives is diagnosable.
    Once its resolve budget is spent it stops re-reading the transcripts, is set
    aside, and warns exactly once.
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

    scans = _count_transcript_scans(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        state = await _poll_subagents(
            client,
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
        )
        assert starts == 0
        assert "orphan" not in state.subagents
        assert "no resolved parent" in caplog.text
        assert "toolu_missing" in caplog.text
        assert len(scans) == 1
        assert not _set_aside_warnings(caplog)

        budget = forwarder._SUBAGENT_RESOLVE_MAX_ATTEMPTS
        for _ in range(budget):
            state = await _poll_subagents(
                client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
            )

    assert starts == 0
    assert "orphan" not in state.subagents
    assert len(scans) == budget, (
        "an unowned spawn must stop re-reading the transcripts at its resolve budget"
    )
    assert state.resolve_misses == {"orphan": budget}
    assert len(_set_aside_warnings(caplog, "orphan", "toolu_missing")) == 1


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

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        response = (
            [response_for(item) for item in body] if isinstance(body, list) else response_for(body)
        )
        return httpx.Response(202, json=response)

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
    ``external_subagent_start`` POST). This is the idempotency
    contract the cursor file is for — without it, a forwarder
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
                    }
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


def _inherited_spawn_record(tool_use_id: str, description: str) -> dict[str, Any]:
    """A fork's own transcript row: a sidechain copy of the Agent record that
    spawned it, inherited when the fork cloned the parent conversation."""
    return {
        "isSidechain": True,
        "type": "assistant",
        "uuid": f"inherited-spawn-{tool_use_id}",
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


async def test_resolve_misses_survive_a_forwarder_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partly spent resolve budget reloads from disk and gets only its remaining
    attempts after a restart, a count whose meta file is gone is dropped, and a
    spent budget stays set aside with no further scan or WARNING."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    (subagents_dir / "agent-orphan.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "spawn is in no transcript",
                "toolUseId": "toolu_orphan",
            }
        ),
        encoding="utf-8",
    )
    (subagents_dir / "agent-orphan.jsonl").write_text("", encoding="utf-8")

    budget = forwarder._SUBAGENT_RESOLVE_MAX_ATTEMPTS
    spent = budget // 2
    # A previous forwarder process persisted a partly spent budget, plus a count
    # for a sub-agent whose meta file has since been removed.
    forwarder._write_subagent_forward_state(
        bridge_dir,
        forwarder.SubagentForwardState(
            subagents={}, resolve_misses={"orphan": spent, "gone": budget}
        ),
    )
    state = forwarder._read_subagent_forward_state(bridge_dir)
    assert state.resolve_misses == {"orphan": spent, "gone": budget}

    scans = _count_transcript_scans(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_registering_handler), base_url="http://ap"
    ) as client:
        state = await _poll_subagents(
            client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
        )
        assert state.resolve_misses == {"orphan": spent + 1}, "a vanished meta's count is dropped"
        assert forwarder._read_subagent_forward_state(bridge_dir) == state
        for _ in range(budget - 1):
            state = await _poll_subagents(
                client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
            )
        assert len(scans) == budget - spent, "only the remaining attempts may re-read transcripts"
        assert state.resolve_misses == {"orphan": budget}
        assert len(_set_aside_warnings(caplog, "orphan", "toolu_orphan")) == 1

        # The next restart finds the spent budget: no scan and no repeated WARNING.
        restarted = forwarder._read_subagent_forward_state(bridge_dir)
        assert restarted == state
        scans.clear()
        caplog.clear()
        state = await _poll_subagents(
            client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=restarted
        )

    assert "orphan" not in state.subagents
    assert not scans
    assert not _set_aside_warnings(caplog)


async def test_unreadable_meta_never_spends_the_resolve_budget(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A meta that stays unreadable past the resolve budget costs no transcript
    scan and no budget, so it still registers once it becomes readable."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    jsonl_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-worker",
        agent_type="Explore",
        description="meta still being written",
        tool_use_id="toolu_worker",
    )
    meta_path = jsonl_path.with_name("agent-a-worker.meta.json")
    valid_meta = meta_path.read_text(encoding="utf-8")
    meta_path.write_text(valid_meta[: len(valid_meta) // 2], encoding="utf-8")

    scans = _count_transcript_scans(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    state = forwarder.SubagentForwardState(subagents={})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_registering_handler), base_url="http://ap"
    ) as client:
        for _ in range(forwarder._SUBAGENT_RESOLVE_MAX_ATTEMPTS + 1):
            state = await _poll_subagents(
                client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
            )
        assert not scans, "an unreadable meta must not trigger a transcript scan"
        assert state.subagents == {}
        assert state.resolve_misses == {}
        assert not _set_aside_warnings(caplog)

        meta_path.write_text(valid_meta, encoding="utf-8")
        state = await _poll_subagents(
            client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
        )

    assert len(scans) == 1
    assert state.subagents["a-worker"].child_conversation_id == "conv_a-worker"
    assert state.resolve_misses == {}


async def test_late_spawn_record_registers_and_forgets_the_resolve_misses(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spawn record that lands after several unresolved polls still registers,
    and the registration clears the miss count in memory and on disk."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    (subagents_dir / "agent-late.meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": "spawn record not flushed yet",
                "toolUseId": "toolu_late",
            }
        ),
        encoding="utf-8",
    )
    (subagents_dir / "agent-late.jsonl").write_text("", encoding="utf-8")

    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    unresolved_polls = forwarder._SUBAGENT_RESOLVE_MAX_ATTEMPTS // 2
    state = forwarder.SubagentForwardState(subagents={})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_registering_handler), base_url="http://ap"
    ) as client:
        for _ in range(unresolved_polls):
            state = await _poll_subagents(
                client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
            )
        assert "late" not in state.subagents
        assert state.resolve_misses == {"late": unresolved_polls}
        assert forwarder._read_subagent_forward_state(bridge_dir).resolve_misses == {
            "late": unresolved_polls
        }
        assert not _set_aside_warnings(caplog)

        # The spawning Agent call finally lands in the root transcript.
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id="late",
            agent_type="Explore",
            description="spawn record not flushed yet",
            tool_use_id="toolu_late",
        )
        state = await _poll_subagents(
            client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
        )

    assert state.subagents["late"].child_conversation_id == "conv_late"
    assert state.resolve_misses == {}
    assert forwarder._read_subagent_forward_state(bridge_dir) == state


async def test_late_spawn_park_forgets_resolve_misses(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Parking a sub-agent whose registration is dropped for good clears the misses
    it accumulated while its spawn record was still missing."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    (subagents_dir / "agent-late.meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": "spawn record not flushed yet",
                "toolUseId": "toolu_late",
            }
        ),
        encoding="utf-8",
    )
    (subagents_dir / "agent-late.jsonl").write_text("", encoding="utf-8")

    def dropping_handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("type") == "external_subagent_start":
            return httpx.Response(400)
        return httpx.Response(202, json={})

    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    unresolved_polls = forwarder._SUBAGENT_RESOLVE_MAX_ATTEMPTS // 2
    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    state = forwarder.SubagentForwardState(subagents={})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(dropping_handler), base_url="http://ap"
    ) as client:
        for _ in range(unresolved_polls):
            state = await _poll_subagents(
                client,
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                start_retry_tracker=tracker,
            )
        assert state.resolve_misses == {"late": unresolved_polls}

        # The spawn record lands, but every registration attempt is rejected.
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id="late",
            agent_type="Explore",
            description="spawn record not flushed yet",
            tool_use_id="toolu_late",
        )
        for _ in range(forwarder._HTTP_POST_MAX_PERMANENT_FAILURES):
            state = await _poll_subagents(
                client,
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                start_retry_tracker=tracker,
            )

    assert state.subagents["late"].child_conversation_id == ""
    assert state.resolve_misses == {}
    assert forwarder._read_subagent_forward_state(bridge_dir) == state
    assert not _set_aside_warnings(caplog)


async def test_checkpointing_a_child_cursor_keeps_an_orphans_resolve_misses(
    tmp_path: Path,
) -> None:
    """Advancing a registered child's transcript cursor must keep an unresolved
    orphan's resolve count in the persisted state."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    worker_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="worker",
        agent_type="Explore",
        description="registered and talking",
        tool_use_id="toolu_worker",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "worker-says",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            }
        ],
    )
    (worker_jsonl.with_name("agent-orphan.meta.json")).write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "spawn is in no transcript",
                "toolUseId": "toolu_orphan",
            }
        ),
        encoding="utf-8",
    )
    worker_jsonl.with_name("agent-orphan.jsonl").write_text("", encoding="utf-8")
    registered = forwarder.SubagentForwardState(
        subagents={
            "worker": forwarder.SubagentEntry(
                subagent_id="worker", child_conversation_id="conv_worker"
            )
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_registering_handler), base_url="http://ap"
    ) as client:
        state = await _poll_subagents(
            client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=registered
        )

    assert state.subagents["worker"].byte_offset == worker_jsonl.stat().st_size
    assert state.resolve_misses == {"orphan": 1}
    assert forwarder._read_subagent_forward_state(bridge_dir) == state


def _seed_registration_chain(transcript_path: Path) -> None:
    """Seed grandparent -> parent -> child, each spawned from its parent's transcript."""
    grandparent_transcript = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="z-grandparent",
        agent_type="general-purpose",
        description="spawned from the root",
        tool_use_id="toolu_grandparent",
    )
    parent_transcript = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="m-parent",
        agent_type="general-purpose",
        description="spawned by the grandparent",
        tool_use_id="toolu_parent",
        spawn_transcript_path=grandparent_transcript,
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-child",
        agent_type="general-purpose",
        description="spawned by the parent",
        tool_use_id="toolu_child",
        spawn_transcript_path=parent_transcript,
    )


def _grandparent_failing_handler(
    status_code: int, failures: int
) -> Callable[[httpx.Request], httpx.Response]:
    """Fail the grandparent's start POST ``failures`` times; register everything else."""
    remaining = [failures]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if (
            body.get("type") == "external_subagent_start"
            and body["data"]["subagent_id"] == "z-grandparent"
            and remaining[0] > 0
        ):
            remaining[0] -= 1
            return httpx.Response(status_code)
        return _registering_handler(request)

    return handler


async def test_child_waits_while_its_grandparent_backs_off(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child whose parent is itself deferred behind a grandparent held in
    registration backoff waits at no budget cost and registers once the chain
    recovers."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_registration_chain(transcript_path)

    clock = {"now": 1000.0}
    monkeypatch.setattr(forwarder.time, "monotonic", lambda: clock["now"])
    failing = _grandparent_failing_handler(502, 1)
    grandparent_starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal grandparent_starts
        body = json.loads(request.content)
        if (
            body.get("type") == "external_subagent_start"
            and body["data"]["subagent_id"] == "z-grandparent"
        ):
            grandparent_starts += 1
        return failing(request)

    gated_polls = forwarder._SUBAGENT_RESOLVE_MAX_ATTEMPTS + 1
    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    tracker = forwarder._PostRetryTracker(base_delay_s=1.0)
    state = forwarder.SubagentForwardState(subagents={})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        # One failed start puts the grandparent into a one-second backoff; with the
        # clock frozen, every later poll skips it without another POST.
        for _ in range(1 + gated_polls):
            state = await _poll_subagents(
                client,
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                start_retry_tracker=tracker,
            )
            assert state.subagents == {}
            assert state.resolve_misses == {}
        assert grandparent_starts == 1
        assert tracker.retry_delay_s("subagent_start:z-grandparent") == pytest.approx(1.0)

        clock["now"] += 1.0
        state = await _poll_subagents(
            client,
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            start_retry_tracker=tracker,
        )

    assert grandparent_starts == 2
    assert state.subagents["z-grandparent"].child_conversation_id == "conv_z-grandparent"
    assert state.subagents["m-parent"].parent_subagent_id == "z-grandparent"
    assert state.subagents["a-child"].parent_subagent_id == "m-parent"
    assert state.subagents["a-child"].child_conversation_id == "conv_a-child"
    assert state.resolve_misses == {}
    assert not _set_aside_warnings(caplog)


async def test_chain_parked_under_a_dropped_grandparent_spends_no_resolve_budget(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the grandparent's registration is dropped for good, its descendants
    are parked under it on the following poll instead of spending resolve budget."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_registration_chain(transcript_path)

    dropping_polls = forwarder._HTTP_POST_MAX_PERMANENT_FAILURES
    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    state = forwarder.SubagentForwardState(subagents={})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_grandparent_failing_handler(400, dropping_polls)),
        base_url="http://ap",
    ) as client:
        for _ in range(dropping_polls):
            state = await _poll_subagents(
                client,
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                start_retry_tracker=tracker,
            )
        assert state.subagents["z-grandparent"].child_conversation_id == ""
        assert set(state.subagents) == {"z-grandparent"}
        assert state.resolve_misses == {}
        state = await _poll_subagents(
            client,
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            start_retry_tracker=tracker,
        )

    assert state.subagents["m-parent"].child_conversation_id == ""
    assert state.subagents["m-parent"].parent_subagent_id == "z-grandparent"
    assert state.subagents["a-child"].child_conversation_id == ""
    assert state.subagents["a-child"].parent_subagent_id == "m-parent"
    assert state.resolve_misses == {}
    assert not _set_aside_warnings(caplog)
    assert caplog.text.count("whose parent was dropped") == 2


@pytest.mark.parametrize("parent_meta", ["absent", "unreadable"])
async def test_child_of_an_unregistrable_parent_spends_its_own_budget(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    parent_meta: str,
) -> None:
    """A child whose parent has no meta file, or one that never becomes readable,
    has no parent that can register, so it spends its own budget and stops
    re-reading the transcripts once it is set aside."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-child",
        agent_type="general-purpose",
        description="spawned by a sub-agent that cannot register",
        tool_use_id="toolu_child",
        spawn_transcript_path=subagents_dir / "agent-ghost.jsonl",
    )
    if parent_meta == "unreadable":
        (subagents_dir / "agent-ghost.meta.json").write_text(
            '{"agentType": "gen', encoding="utf-8"
        )

    budget = forwarder._SUBAGENT_RESOLVE_MAX_ATTEMPTS
    scans = _count_transcript_scans(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    state = forwarder.SubagentForwardState(subagents={})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_registering_handler), base_url="http://ap"
    ) as client:
        for _ in range(budget + 1):
            state = await _poll_subagents(
                client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
            )

    assert state.subagents == {}
    assert state.resolve_misses == {"a-child": budget}
    assert len(scans) == budget, (
        "a child with no registrable parent must stop scanning at its budget"
    )
    assert len(_set_aside_warnings(caplog, "a-child", "toolu_child", "ghost")) == 1
