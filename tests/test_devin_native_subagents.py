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
    SUBAGENT_ROOT_PREFIX,
    chain_index_for_report,
    completed_agent_ids,
    completed_agent_reports,
    devin_sessions_db_path,
    final_assistant_text,
    load_message_nodes,
    parse_spawned_agent_id,
    reconstruct_transcript_chains,
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
            tc["name"] for node in chain for tc in (node["chat_message"].get("tool_calls") or [])
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
        chain = reconstruct_transcript_nodes(_NODES, _ONE_TASK)
        items = transcript_items(chain, "devin-native-ui")
        assert "You are a subagent" not in json.dumps(items)

    def test_empty_chain_yields_no_items(self) -> None:
        assert transcript_items([], "devin-native-ui") == []

    def test_every_emitted_item_validates_for_the_events_api(self) -> None:
        # The child mirror POSTs each item to /events, which validates it against
        # its ItemData model; an invalid item 400s and aborts the transcript,
        # leaving the child with only the (repeated) task. A reasoning node is
        # included because ReasoningData requires `summary` — a content-only
        # reasoning item is exactly the bug this guards.
        from omnigent.entities.conversation import parse_item_data

        chain = [
            {"chat_message": {"role": "user", "content": "do the thing"}},
            {
                "chat_message": {
                    "role": "assistant",
                    "content": "done",
                    "thinking": {"thinking": "let me reason about it"},
                    "tool_calls": [{"id": "c1", "name": "write", "arguments": {"file_path": "x"}}],
                }
            },
            {"chat_message": {"role": "tool", "tool_call_id": "c1", "content": "ok"}},
        ]
        items = transcript_items(chain, "devin-native-ui")
        assert any(kind == "reasoning" for kind, _ in items)
        for kind, data in items:
            parse_item_data(kind, {"type": kind, **data})  # raises if the shape is invalid


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


def _node(node_id: int, parent: int | None, role: str, content: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "parent_node_id": parent,
        "chat_message": {"role": role, "content": content},
    }


# Two delegates launched with the SAME task text (Devin's `run_subagent` fan-out
# of one identical prompt), plus the two artifacts the real forest carries: a
# duplicate streaming leaf off one chain, and a bare task node never answered.
_SHARED_TASK = "Tell me a single short joke. Reply with just the joke."
_JOKE_A = "Why did the scarecrow win an award? Because he was outstanding in his field."
_JOKE_B = "Why don't scientists trust atoms? Because they make up everything."
_SAME_TASK_NODES = [
    _node(1, None, "system", f"{SUBAGENT_ROOT_PREFIX}. Work autonomously."),
    _node(2, 1, "user", _SHARED_TASK),
    _node(3, 2, "assistant", _JOKE_A),
    _node(4, 2, "assistant", _JOKE_A),
    _node(5, None, "system", f"{SUBAGENT_ROOT_PREFIX}. Work autonomously."),
    _node(6, 5, "user", _SHARED_TASK),
    _node(7, 6, "assistant", _JOKE_B),
    _node(8, None, "user", _SHARED_TASK),
]
# `aaa11111` was spawned first but answered with the SECOND chain's joke, so
# completion order is not spawn order and only the text identifies the chain.
_NOTIFICATIONS = (
    "<subagent_completion_notification>\n"
    f"[Background subagent with agent_id=aaa11111 completed]\n\n{_JOKE_B}\n"
    "</subagent_completion_notification>\n"
    "<subagent_completion_notification>\n"
    f"[Background subagent with agent_id=bbb22222 completed]\n\n{_JOKE_A}\n"
    "</subagent_completion_notification>"
)


class TestSameTaskSubagents:
    """Delegates sharing one task text must not share one transcript."""

    def test_each_same_task_delegate_keeps_its_own_chain(self) -> None:
        chains = reconstruct_transcript_chains(_SAME_TASK_NODES, _SHARED_TASK)
        assert len(chains) == 2
        assert {final_assistant_text(chain) for chain in chains} == {_JOKE_A, _JOKE_B}

    def test_duplicate_leaf_and_unanswered_stub_are_not_extra_chains(self) -> None:
        # node 4 duplicates a leaf off chain one; node 8 is a task never answered.
        chains = reconstruct_transcript_chains(_SAME_TASK_NODES, _SHARED_TASK)
        assert len(chains) == 2
        assert all(final_assistant_text(chain) for chain in chains)

    def test_the_report_picks_the_chain_not_spawn_order(self) -> None:
        reports = completed_agent_reports(_NOTIFICATIONS)
        chains = reconstruct_transcript_chains(_SAME_TASK_NODES, _SHARED_TASK)
        claimed: set[int] = set()
        picked: dict[str, str] = {}
        for agent_id in ("aaa11111", "bbb22222"):
            index = chain_index_for_report(chains, reports[agent_id], claimed)
            assert index is not None
            claimed.add(index)
            picked[agent_id] = final_assistant_text(chains[index])
        assert picked == {"aaa11111": _JOKE_B, "bbb22222": _JOKE_A}

    def test_unmatchable_reports_still_claim_distinct_chains(self) -> None:
        chains = reconstruct_transcript_chains(_SAME_TASK_NODES, _SHARED_TASK)
        claimed: set[int] = set()
        for _agent in range(2):
            index = chain_index_for_report(chains, "nothing like the transcript", claimed)
            assert index is not None
            claimed.add(index)
        assert claimed == {0, 1}

    def test_no_chain_left_to_claim_is_none(self) -> None:
        chains = reconstruct_transcript_chains(_SAME_TASK_NODES, _SHARED_TASK)
        assert chain_index_for_report(chains, _JOKE_A, {0, 1}) is None

    def test_singular_helper_still_returns_one_chain(self) -> None:
        chain = reconstruct_transcript_nodes(_SAME_TASK_NODES, _SHARED_TASK)
        assert final_assistant_text(chain) in {_JOKE_A, _JOKE_B}


class TestCompletionReports:
    """The notification is the only place an agent_id meets its own output."""

    def test_pairs_each_id_with_its_report(self) -> None:
        assert completed_agent_reports(_NOTIFICATIONS) == {
            "aaa11111": _JOKE_B,
            "bbb22222": _JOKE_A,
        }

    def test_ids_still_parse_alongside_the_reports(self) -> None:
        assert completed_agent_ids(_NOTIFICATIONS) == ["aaa11111", "bbb22222"]

    def test_no_notification_is_empty(self) -> None:
        assert completed_agent_reports(None) == {}
        assert completed_agent_reports("just some assistant text") == {}
