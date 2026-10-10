"""Regression coverage for OMNI-10204 (native forwarders re-read whole files
every poll and retry unresolvable items forever).

When a Claude sub-agent's spawn ``toolUseId`` is in no transcript -- a background
spawn whose ``Agent`` call was never written, or a spawn dropped when the resume
path rewrites the transcript across a compaction -- ``_subagent_parents_by_tool_use``
finds no owner, so ``_forward_available_subagents`` re-reads the main transcript
and every sub-agent transcript in full on every 0.25s poll, forever, logging only
at DEBUG. The forwarder should instead set such an item aside after a bounded
number of attempts, log it once at WARNING, and stop the full re-reads.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
import omnigent.harnesses.claude_native.main as claude_main
from tests.harnesses.claude_native.forwarder._support import _seed_subagent_on_disk

_POLLS = 8


async def _poll_until_steady(
    *,
    transcript_path: Path,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[int], forwarder.SubagentForwardState]:
    """Run the real sub-agent poll ``_POLLS`` times over unchanged files.

    Returns the 1-based indices of the polls that performed a full transcript
    re-scan (``_subagent_parents_by_tool_use``) and the final forwarder state.
    """
    rescan_polls: list[int] = []
    current_poll = 0
    original_scan = forwarder._subagent_parents_by_tool_use

    def counting_scan(*args: Any, **kwargs: Any) -> Any:
        rescan_polls.append(current_poll)
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(forwarder, "_subagent_parents_by_tool_use", counting_scan)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={})

    state = forwarder.SubagentForwardState(subagents={})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for current_poll in range(1, _POLLS + 1):
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_root",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            )
    return rescan_polls, state


def _warnings_naming(caplog: pytest.LogCaptureFixture, *needles: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and any(needle in record.getMessage() for needle in needles)
    ]


async def test_orphan_subagent_meta_is_set_aside_and_stops_full_rereads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``.meta.json`` whose spawn id is in no transcript must stop driving
    full re-reads after a few polls and be logged once at WARNING."""
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    (subagents_dir / "agent-orphan.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "background spawn, no Agent call on disk",
                "toolUseId": "toolu_orphan",
            }
        ),
        encoding="utf-8",
    )
    (subagents_dir / "agent-orphan.jsonl").write_text("", encoding="utf-8")

    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    rescan_polls, state = await _poll_until_steady(
        transcript_path=transcript_path, bridge_dir=tmp_path / "bridge", monkeypatch=monkeypatch
    )

    assert "orphan" not in state.subagents
    assert len(rescan_polls) < _POLLS, (
        f"unresolvable meta re-scanned all transcripts on every poll: {rescan_polls}"
    )
    assert _POLLS not in rescan_polls, "the final poll still re-read every transcript"
    assert _warnings_naming(caplog, "orphan", "toolu_orphan"), (
        "unresolvable meta was never set aside with a WARNING"
    )


async def test_compaction_dropped_spawn_stops_full_rereads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn dropped when the resume path rewrites across a compaction (plus a
    sub-agent nested under it) must stop driving full re-reads on later polls."""
    transcript_path = tmp_path / "session.jsonl"
    external_sid = str(uuid.uuid4())
    items: list[dict[str, Any]] = [
        {
            "type": "function_call",
            "name": "Agent",
            "call_id": "toolu_precompact",
            "arguments": json.dumps({"description": "background work"}),
        },
        {
            "type": "compaction",
            "compacted_messages": [
                {"type": "message", "role": "user", "content": "compaction summary"}
            ],
            "token_count": 1234,
        },
    ]
    records = claude_main._claude_transcript_records_from_session_items(
        items,
        session_id="conv_root",
        external_session_id=external_sid,
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
    )
    body = "\n".join(json.dumps(record) for record in records) + "\n"
    transcript_path.write_text(body, encoding="utf-8")
    assert "toolu_precompact" not in body, "the resume rewrite unexpectedly kept the spawn id"

    # The sub-agent whose spawn was cleared survives on disk; its own spawn id
    # is now in no transcript. It spawned a nested sub-agent, whose spawn tool
    # call lives in the compacted sub-agent's own transcript -- so the nested
    # one resolves to a parent that is itself never registered.
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    (subagents_dir / "agent-compacted.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "spawned before the last compaction",
                "toolUseId": "toolu_precompact",
            }
        ),
        encoding="utf-8",
    )
    (subagents_dir / "agent-compacted.jsonl").write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="nested",
        agent_type="general-purpose",
        description="nested; parent never registered",
        tool_use_id="toolu_nested",
        spawn_transcript_path=subagents_dir / "agent-compacted.jsonl",
    )

    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    rescan_polls, state = await _poll_until_steady(
        transcript_path=transcript_path, bridge_dir=tmp_path / "bridge", monkeypatch=monkeypatch
    )

    assert "compacted" not in state.subagents
    assert "nested" not in state.subagents
    assert len(rescan_polls) < _POLLS, (
        f"compaction-orphaned sub-agents re-scanned every transcript on every poll: {rescan_polls}"
    )
    assert _POLLS not in rescan_polls, "the final poll still re-read every transcript"
