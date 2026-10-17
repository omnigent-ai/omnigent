"""A spawn dropped by a compaction rewrite stops triggering full transcript
re-scans after a bounded resolve budget and is set aside with a single WARNING."""

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
from tests.harnesses.claude_native.forwarder._support import (
    _count_transcript_scans,
    _poll_subagents,
    _registering_handler,
    _seed_subagent_on_disk,
    _set_aside_warnings,
)

_BUDGET = forwarder._SUBAGENT_RESOLVE_MAX_ATTEMPTS
# Past the budget, so a bounded scan leaves idle polls at the end.
_POLLS = _BUDGET + 2


async def _poll_until_steady(
    *,
    transcript_path: Path,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[int], list[forwarder.SubagentForwardState]]:
    """Run the real sub-agent poll ``_POLLS`` times over unchanged files.

    Returns the 1-based indices of the polls that performed a full transcript
    re-scan and the forwarder state after each poll.
    """
    scans = _count_transcript_scans(monkeypatch)
    rescan_polls: list[int] = []
    states: list[forwarder.SubagentForwardState] = []
    state = forwarder.SubagentForwardState(subagents={})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_registering_handler), base_url="http://ap"
    ) as client:
        for poll in range(1, _POLLS + 1):
            scans_before = len(scans)
            state = await _poll_subagents(
                client, bridge_dir=bridge_dir, transcript_path=transcript_path, state=state
            )
            if len(scans) > scans_before:
                rescan_polls.append(poll)
            states.append(state)
    return rescan_polls, states


async def test_compaction_dropped_spawn_stops_full_rereads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn dropped when the resume path rewrites across a compaction, and the
    sub-agents nested under it, are all set aside once and stop the re-reads."""
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

    # The compacted sub-agent's spawn id is now in no transcript, and the spawns
    # nested under it live in transcripts whose owners are never registered. The
    # descendants sort before their parents, the harder order for the sweep.
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
    nested_transcript = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="b-nested",
        agent_type="general-purpose",
        description="nested; parent never registered",
        tool_use_id="toolu_nested",
        spawn_transcript_path=subagents_dir / "agent-compacted.jsonl",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a-leaf",
        agent_type="general-purpose",
        description="nested two levels down",
        tool_use_id="toolu_leaf",
        spawn_transcript_path=nested_transcript,
    )

    caplog.set_level(logging.DEBUG, logger="omnigent.harnesses.claude_native.forwarder")
    rescan_polls, states = await _poll_until_steady(
        transcript_path=transcript_path, bridge_dir=tmp_path / "bridge", monkeypatch=monkeypatch
    )

    assert states[-1].subagents == {}
    assert rescan_polls == list(range(1, _BUDGET + 1)), (
        f"full transcript re-scans must stop exactly at the resolve budget: {rescan_polls}"
    )
    # The descendants wait for free while the root spends the budget, then the whole
    # chain is set aside on the poll that exhausts it.
    assert states[_BUDGET - 2].resolve_misses == {"compacted": _BUDGET - 1}
    assert states[_BUDGET - 1].resolve_misses == {
        "compacted": _BUDGET,
        "b-nested": _BUDGET,
        "a-leaf": _BUDGET,
    }
    assert len(_set_aside_warnings(caplog, "compacted", "toolu_precompact")) == 1
    assert len(_set_aside_warnings(caplog, "b-nested", "toolu_nested")) == 1
    assert len(_set_aside_warnings(caplog, "a-leaf", "toolu_leaf")) == 1
