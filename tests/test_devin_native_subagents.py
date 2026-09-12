"""Tests for reconstructing Devin sub-agent transcripts from ``message_nodes``.

The node fixture (``tests/data/devin_subagent_nodes.json``) is a trimmed capture
from devin 3000.10.21 — a turn that spawned two parallel sub-agents writing
``one.txt`` and ``two.txt``. System-prompt text is truncated for size, but the
sub-agent chains, their compaction snapshots, the ``run_subagent`` results and
the ``<subagent_completion_notification>`` bodies are the vendor's real wire
shape, so the reconstruction is pinned against reality rather than a guess.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from omnigent.harnesses.devin_native.subagents import (
    completed_agent_ids,
    devin_sessions_db_path,
    load_message_nodes,
    parse_spawned_agent_id,
    reconstruct_transcript_nodes,
    transcript_items,
)

_NODES = json.loads((Path(__file__).parent / "data" / "devin_subagent_nodes.json").read_text())
# The one.txt sub-agent's task — verbatim the run_subagent `task` a hook delivers.
_ONE_TASK = (
    "Write a file at /tmp/devin-sub2/work/one.txt whose contents are exactly:\n\n"
    "ONE\n\nUse your file-writing tool to create it. Report back when done."
)


class TestParsers:
    """The spawned/completed ids ride only free text, so parse them defensively."""

    def test_spawned_agent_id(self) -> None:
        assert (
            parse_spawned_agent_id(
                "Background subagent started with agent_id=690d786b. You can wait…"
            )
            == "690d786b"
        )

    def test_spawned_agent_id_absent(self) -> None:
        assert parse_spawned_agent_id("no id here") is None
        assert parse_spawned_agent_id(None) is None

    def test_completed_ids(self) -> None:
        text = (
            "<subagent_completion_notification>\n"
            "[Background subagent with agent_id=690d786b completed]\n\nDone."
        )
        assert completed_agent_ids(text) == ["690d786b"]

    def test_completed_ids_none(self) -> None:
        assert completed_agent_ids("nothing here") == []


class TestDbPath:
    """Read the same store Devin wrote, keyed off the launch env."""

    def test_xdg_wins(self) -> None:
        assert devin_sessions_db_path({"XDG_DATA_HOME": "/x", "HOME": "/h"}) == Path(
            "/x/devin/cli/sessions.db"
        )

    def test_home_fallback(self) -> None:
        assert devin_sessions_db_path({"HOME": "/h"}) == Path(
            "/h/.local/share/devin/cli/sessions.db"
        )

    def test_unlocatable(self) -> None:
        assert devin_sessions_db_path({}) is None


class TestReconstruct:
    """A leaf→root walk keyed on the task recovers the executed chain."""

    def test_recovers_the_one_txt_subagent_chain(self) -> None:
        chain = reconstruct_transcript_nodes(_NODES, _ONE_TASK)
        # Opens at the sub-agent's own prompt, not the "You are a subagent" prefix.
        assert chain[0]["chat_message"]["role"] == "user"
        assert chain[0]["chat_message"]["content"] == _ONE_TASK
        # Reaches the sub-agent's final report.
        assert chain[-1]["chat_message"]["role"] == "assistant"
        assert "one.txt" in chain[-1]["chat_message"]["content"]
        # The full internal tool work is present — this is the whole point.
        tool_names = [
            tc["name"]
            for node in chain
            for tc in (node["chat_message"].get("tool_calls") or [])
        ]
        assert "write" in tool_names

    def test_longest_chain_beats_compaction_snapshots(self) -> None:
        # The forest holds 2-node snapshot dead-ends for the same task; the
        # executed chain is far longer, so it must win.
        assert len(reconstruct_transcript_nodes(_NODES, _ONE_TASK)) >= 6

    def test_two_subagents_are_distinct(self) -> None:
        two_task = _ONE_TASK.replace("one.txt", "two.txt").replace("ONE", "TWO")
        one = reconstruct_transcript_nodes(_NODES, _ONE_TASK)
        two = reconstruct_transcript_nodes(_NODES, two_task)
        assert "two.txt" in two[-1]["chat_message"]["content"]
        assert {n["node_id"] for n in one}.isdisjoint({n["node_id"] for n in two})

    def test_unknown_task_is_empty(self) -> None:
        assert reconstruct_transcript_nodes(_NODES, "no such task") == []


class TestTranscriptItems:
    """Node chains convert to the forwarder's own conversation-item shapes."""

    def test_maps_chain_to_conversation_items(self) -> None:
        chain = reconstruct_transcript_nodes(_NODES, _ONE_TASK)
        items = transcript_items(chain, "devin-native-ui")
        # First item is the sub-agent's task, as a user message.
        assert items[0] == (
            "message",
            {"role": "user", "content": [{"type": "input_text", "text": _ONE_TASK}]},
        )
        # A write call carries its real arguments and pairs with an output.
        calls = [data for kind, data in items if kind == "function_call"]
        write = next(c for c in calls if c["name"] == "write")
        assert json.loads(write["arguments"])["file_path"].endswith("one.txt")
        outputs = [data for kind, data in items if kind == "function_call_output"]
        assert any(o["call_id"] == write["call_id"] for o in outputs)
        # Ends on the sub-agent's final assistant message.
        assert items[-1][0] == "message"
        assert items[-1][1]["role"] == "assistant"
        assert items[-1][1]["agent"] == "devin-native-ui"

    def test_system_boilerplate_is_dropped(self) -> None:
        items = transcript_items(reconstruct_transcript_nodes(_NODES, _ONE_TASK), "devin-native-ui")
        assert "You are a subagent" not in json.dumps(items)

    def test_empty_chain_yields_no_items(self) -> None:
        assert transcript_items([], "devin-native-ui") == []


class TestLoadMessageNodes:
    """The only I/O: read one session's forest, read-only, failing soft."""

    def test_reads_only_the_named_session(self, tmp_path: Path) -> None:
        db = tmp_path / "sessions.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE message_nodes "
            "(session_id TEXT, node_id INT, parent_node_id INT, chat_message TEXT)"
        )
        con.execute(
            "INSERT INTO message_nodes VALUES ('s1', 0, NULL, ?)",
            (json.dumps({"role": "user", "content": "hi"}),),
        )
        con.execute(
            "INSERT INTO message_nodes VALUES ('s2', 0, NULL, ?)",
            (json.dumps({"role": "user", "content": "other"}),),
        )
        con.commit()
        con.close()
        nodes = load_message_nodes(db, "s1")
        assert [n["chat_message"]["content"] for n in nodes] == ["hi"]

    def test_missing_db_is_empty(self, tmp_path: Path) -> None:
        assert load_message_nodes(tmp_path / "absent.db", "s1") == []
