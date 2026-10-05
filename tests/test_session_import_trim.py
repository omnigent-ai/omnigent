"""Trim and budget tests for local session import (cap, budget, notice item).

A session with more items than the import cap keeps only the first items
with a visible notice item at the end. A Claude or Codex transcript larger
than the read budget (``IMPORT_READ_BUDGET_BYTES``) is parsed only until
the cap or the budget, so time and host memory don't grow with the file.
These tests cover the cap itself, budgeted read and its edge cases, then
real Claude/Codex transcripts read through ``load_local_session``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from omnigent.entities import ErrorData, MessageData, NewConversationItem, parse_item_data
from omnigent.harnesses.claude_native.bridge import read_transcript_items_from_offset
from omnigent.session_import import local as local_module
from omnigent.session_import.local import (
    IMPORT_MAX_ITEMS,
    IMPORT_TRIMMED_NOTICE_CODE,
    cap_import_items,
    load_local_session,
)

_CLAUDE_ID = "a1b2c3d4-1234-5678-9abc-def012345670"
_CODEX_ID = "0199a5c0-1234-7abc-8def-0123456789ab"


def _user(text: str, response_id: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _turn(n: int) -> list[NewConversationItem]:
    """user prompt, assistant text, tool call, tool output: one 4-item turn."""
    rid = f"r{n}"
    return [
        _user(f"turn {n}", rid),
        NewConversationItem(
            type="message",
            response_id=rid,
            data=parse_item_data(
                "message",
                {
                    "role": "assistant",
                    "agent": "a",
                    "content": [{"type": "output_text", "text": f"ok {n}"}],
                },
            ),
        ),
        NewConversationItem(
            type="function_call",
            response_id=rid,
            data=parse_item_data(
                "function_call",
                {"agent": "a", "name": "Bash", "arguments": "{}", "call_id": f"c{n}"},
            ),
        ),
        NewConversationItem(
            type="function_call_output",
            response_id=rid,
            data=parse_item_data(
                "function_call_output", {"call_id": f"c{n}", "output": f"out {n}"}
            ),
        ),
    ]


def _turns(count: int) -> list[NewConversationItem]:
    return [item for n in range(count) for item in _turn(n)]


def _err(item: NewConversationItem) -> ErrorData:
    assert isinstance(item.data, ErrorData), item
    return item.data


def _msg(item: NewConversationItem) -> MessageData:
    assert isinstance(item.data, MessageData), item
    return item.data


def _text(item: NewConversationItem) -> str:
    assert isinstance(item.data, MessageData)
    return str(item.data.content[0]["text"])


def _notice(session: Any) -> ErrorData:
    notice = _err(session.items[-1])
    assert notice.code == IMPORT_TRIMMED_NOTICE_CODE, notice
    return notice


def _has_notice(session: Any) -> bool:
    last = session.items[-1]
    return isinstance(last.data, ErrorData) and last.data.code == IMPORT_TRIMMED_NOTICE_CODE


class TestCapImportItems:
    def test_under_the_cap_is_unchanged(self) -> None:
        items = _turns(3)
        kept, dropped = cap_import_items(items, max_items=12)
        assert dropped == 0
        assert list(kept) == items

    def test_over_the_cap_keeps_the_first_items_and_ends_with_the_notice(self) -> None:
        # 10 turns = 40 items; a cap of 12 keeps the first 11 and the notice.
        items = _turns(10)
        kept, dropped = cap_import_items(items, max_items=12)
        assert len(kept) == 12
        assert list(kept[:11]) == items[:11]
        assert kept[-1].type == "error"
        assert dropped == 29
        # The cut may end on a tool call without its output.
        assert kept[-2].type == "function_call"

    def test_notice_is_a_visible_info_item_that_round_trips(self) -> None:
        kept, _ = cap_import_items(_turns(10), max_items=10)
        notice = kept[-1]
        assert isinstance(notice.data, ErrorData)
        assert notice.data.level == "info"
        assert notice.data.code == IMPORT_TRIMMED_NOTICE_CODE
        assert notice.data.title == "Later history not imported"
        assert notice.data.message == (
            "This session was too long to import in full, so only its first 9 items were "
            "imported; the 31 later items were left out."
        )
        # The CLI and host send items as JSON; the server re-parses them.
        wire = notice.data.model_dump(mode="json", exclude_none=True)
        assert parse_item_data(notice.type, wire) == notice.data

    def test_notice_wording_for_one_item_and_an_unknown_count(self) -> None:
        kept, _dropped = cap_import_items(_turns(1)[:3], max_items=2)
        assert _err(kept[-1]).message.endswith(
            "only its first 1 item was imported; the 2 later items were left out."
        )
        unknown = local_module._trimmed_history_notice(None, 1)
        assert _err(unknown).message.endswith(
            "only its first 1 item was imported; later history was left out."
        )

    def test_default_cap_is_large(self) -> None:
        assert IMPORT_MAX_ITEMS > 10000


def _claude_record(record: dict[str, Any], session_id: str, n: int) -> dict[str, Any]:
    return {
        "uuid": str(UUID(int=n + 1)),
        "parentUuid": str(UUID(int=n)) if n else None,
        "sessionId": session_id,
        "cwd": "/repo",
        "timestamp": "2026-10-01T00:00:00.000Z",
        "isSidechain": False,
        "userType": "external",
        "version": "2.1.0",
        **record,
    }


def write_claude_transcript(
    home: Path,
    session_id: str,
    *,
    turns: int,
    compact_after: int | None = None,
    title: str | None = None,
) -> Path:
    """A Claude Code transcript of ``turns`` 4-item turns (prompt "turn N")."""
    records: list[dict[str, Any]] = []
    for n in range(turns):
        records.append({"type": "user", "message": {"role": "user", "content": f"turn {n}"}})
        records.append(
            {
                "type": "assistant",
                "message": {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [
                        {"type": "text", "text": f"ok {n}"},
                        {
                            "type": "tool_use",
                            "id": f"toolu_{n}",
                            "name": "Bash",
                            "input": {"command": "true"},
                        },
                    ],
                },
            }
        )
        records.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": f"out {n}"}
                    ],
                },
            }
        )
        if compact_after is not None and n == compact_after:
            records.append(
                {
                    "type": "user",
                    "isCompactSummary": True,
                    "message": {
                        "role": "user",
                        "content": "This session is being continued from a previous conversation.",
                    },
                }
            )
    lines = [json.dumps(_claude_record(r, session_id, i)) for i, r in enumerate(records)]
    if title is not None:
        lines.append(json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": session_id}))
    path = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_codex_rollout(home: Path, session_id: str, *, turns: int) -> Path:
    """A Codex rollout of ``turns`` 4-item turns (prompt "turn N"), no native title."""

    def rec(kind: str, payload: dict[str, Any]) -> str:
        return json.dumps(
            {"timestamp": "2026-10-01T00:00:00.000Z", "type": kind, "payload": payload}
        )

    lines = [
        rec(
            "session_meta",
            {"id": session_id, "cwd": "/repo", "originator": "codex_cli_rs", "source": "cli"},
        )
    ]
    for n in range(turns):
        lines.append(rec("turn_context", {"turn_id": f"t{n}", "cwd": "/repo"}))
        lines.append(
            rec(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"turn {n}"}],
                },
            )
        )
        lines.append(
            rec(
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"ok {n}"}],
                },
            )
        )
        lines.append(
            rec(
                "response_item",
                {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": f"c{n}"},
            )
        )
        lines.append(
            rec(
                "response_item",
                {"type": "function_call_output", "call_id": f"c{n}", "output": f"out {n}"},
            )
        )
    path = (
        home
        / ".codex"
        / "sessions"
        / "2026"
        / "10"
        / "01"
        / f"rollout-2026-10-01T00-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def loader_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME with small import cap."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 10)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


class TestLoadLocalSessionTrim:
    def test_claude_history_keeps_its_first_turns(self, loader_home: Path) -> None:
        write_claude_transcript(loader_home, _CLAUDE_ID, turns=10)
        session = load_local_session("claude", _CLAUDE_ID)
        assert len(session.items) == 10
        assert _text(session.items[0]) == "turn 0"
        assert _notice(session).message.endswith("the 31 later items were left out.")
        assert (session.trimmed_item_count, session.later_history_omitted) == (31, False)
        assert session.native_title is None
        assert session.title == "turn 0"

    def test_codex_history_keeps_its_first_turns(self, loader_home: Path) -> None:
        write_codex_rollout(loader_home, _CODEX_ID, turns=10)
        session = load_local_session("codex", _CODEX_ID)
        assert len(session.items) == 10
        assert _text(session.items[0]) == "turn 0"
        assert session.items[-1].type == "error"
        assert session.title == "turn 0"

    def test_native_title_still_wins(self, loader_home: Path) -> None:
        write_claude_transcript(loader_home, _CLAUDE_ID, turns=10, title="My renamed session")
        session = load_local_session("claude", _CLAUDE_ID)
        assert session.trimmed_item_count > 0
        assert session.title == "My renamed session"

    def test_under_the_cap_imports_unchanged(self, loader_home: Path) -> None:
        write_claude_transcript(loader_home, _CLAUDE_ID, turns=2)
        session = load_local_session("claude", _CLAUDE_ID)
        assert session.trimmed_item_count == 0
        assert session.later_history_omitted is False
        assert len(session.items) == 8
        assert all(item.type != "error" for item in session.items)

    def test_compaction_trim_applies_first(
        self, loader_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Past the size threshold the loader starts at the last compaction
        # summary; the item cap then keeps the first items after it.
        write_claude_transcript(loader_home, _CLAUDE_ID, turns=20, compact_after=4)
        monkeypatch.setattr(local_module, "_IMPORT_COMPACT_TRIM_BYTES", 0)
        compacted = local_module.load_claude_session(_CLAUDE_ID)
        session = load_local_session("claude", _CLAUDE_ID)
        # 1 summary + 15 turns after it.
        assert len(compacted.items) == 1 + 15 * 4
        assert session.items[: 10 - 1] == compacted.items[: 10 - 1]
        assert session.trimmed_item_count == len(compacted.items) - (10 - 1)
        # The summary is meta, so the title is the first real prompt after it.
        assert session.title == "turn 5"


def _claude_turn(
    n: int, *, prompt: str | None = None, output: str | None = None
) -> list[dict[str, Any]]:
    """The three records of one 4-item Claude turn."""
    return [
        {"type": "user", "message": {"role": "user", "content": prompt or f"turn {n}"}},
        {
            "type": "assistant",
            "message": {
                "id": f"msg_{n}",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [
                    {"type": "text", "text": f"ok {n}"},
                    {
                        "type": "tool_use",
                        "id": f"toolu_{n}",
                        "name": "Bash",
                        "input": {"command": "true"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_{n}",
                        "content": output or f"out {n}",
                    }
                ],
            },
        },
    ]


def _claude_tool_step(n: int) -> list[dict[str, Any]]:
    """An assistant tool call and its result, with no prompt (one long turn)."""
    return _claude_turn(n)[1:]


def _summary(
    text: str = "This session is being continued from a previous conversation.",
) -> dict[str, Any]:
    return {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": text}}


def _write_claude(
    home: Path, session_id: str, records: list[dict[str, Any] | str], *, tail: str = ""
) -> Path:
    """Dicts become numbered Claude records; strings are written as raw lines."""
    lines = [
        r if isinstance(r, str) else json.dumps(_claude_record(r, session_id, n))
        for n, r in enumerate(records)
    ]
    path = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
    return path


def _codex_line(kind: str, payload: dict[str, Any]) -> str:
    return json.dumps({"timestamp": "2026-10-01T00:00:00.000Z", "type": kind, "payload": payload})


def _codex_turn(n: int, *, prompt: str | None = None) -> list[str]:
    return [
        _codex_line("turn_context", {"turn_id": f"t{n}", "cwd": "/repo"}),
        _codex_line(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt or f"turn {n}"}],
            },
        ),
        _codex_line(
            "response_item", {"type": "reasoning", "summary": [], "encrypted_content": "x"}
        ),
        _codex_line(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": f"ok {n}"}],
            },
        ),
        _codex_line(
            "response_item",
            {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": f"c{n}"},
        ),
        _codex_line(
            "response_item",
            {"type": "function_call_output", "call_id": f"c{n}", "output": f"out {n}"},
        ),
    ]


def _codex_compacted(summary: str) -> str:
    history = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": summary}]}
    ]
    return _codex_line("compacted", {"message": "", "replacement_history": history})


def _write_codex(
    home: Path,
    session_id: str,
    lines: list[str],
    *,
    cwd: str = "/repo",
    tail: str = "",
    internal: bool = True,
) -> Path:
    meta = _codex_line(
        "session_meta",
        {"id": session_id, "cwd": cwd, "originator": "codex_cli_rs", "source": "cli"},
    )
    context = _codex_line(
        "response_item",
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "<environment_context>\n</environment_context>"}
            ],
        },
    )
    path = (
        home
        / ".codex"
        / "sessions"
        / "2026"
        / "10"
        / "01"
        / f"rollout-2026-10-01T00-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([meta, *([context] if internal else []), *lines]) + "\n" + tail, encoding="utf-8"
    )
    return path


@pytest.fixture
def budget_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real transcripts with a small read budget; the cap is large."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 1000)
    monkeypatch.setattr(local_module, "IMPORT_READ_BUDGET_BYTES", 4096)
    # Fixtures are tiny; let the compaction trim apply as it would past 2 MiB.
    monkeypatch.setattr(local_module, "_IMPORT_COMPACT_TRIM_BYTES", 0)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


def _full_read(source: str, session_id: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The same transcript read whole (the budget made larger than the file)."""
    with patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", 1 << 40):
        return load_local_session(source, session_id)


def _assert_importable(session: Any) -> None:
    """What the CLI and the host would send passes the server's own validation."""
    assert len(session.items) <= IMPORT_MAX_ITEMS


class TestUnderBudget:
    def test_a_file_within_the_budget_takes_the_full_read_unchanged(
        self, budget_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_claude(
            budget_home, _CLAUDE_ID, [r for n in range(3) for r in _claude_turn(n)]
        )
        rollout = _write_codex(
            budget_home, _CODEX_ID, [line for n in range(3) for line in _codex_turn(n)]
        )
        monkeypatch.setattr(
            local_module,
            "IMPORT_READ_BUDGET_BYTES",
            max(path.stat().st_size, rollout.stat().st_size),
        )
        with (
            patch.object(
                local_module, "_load_claude_budgeted", side_effect=AssertionError("budgeted")
            ),
            patch.object(
                local_module, "_load_codex_budgeted", side_effect=AssertionError("budgeted")
            ),
        ):
            claude = load_local_session("claude", _CLAUDE_ID)
            codex = load_local_session("codex", _CODEX_ID)
        assert len(claude.items) == 12 and not _has_notice(claude)
        assert (claude.trimmed_item_count, claude.later_history_omitted) == (0, False)
        assert len(codex.items) == 13 and not _has_notice(codex)

    def test_budgeted_parser_matches_the_offset_reader(self, budget_home: Path) -> None:
        # Same records, items, ids and order as the reader the full load uses.
        records: list[dict[str, Any] | str] = [r for n in range(30) for r in _claude_turn(n)]
        records[40:40] = ['{"type":"ai-title","aiTitle":"t"}', "not json", "[1, 2]"]
        path = _write_claude(budget_home, _CLAUDE_ID, records, tail='{"partial":')
        with patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", 1 << 40):
            items, stopped_early = local_module._read_claude_head(path, 0)
            expected = read_transcript_items_from_offset(
                path, 0, start_line=0, agent_name="claude-native-ui"
            ).items
            assert items == expected and stopped_early is False


class TestClaudeBudget:
    def test_reading_stops_at_the_budget_with_a_notice_at_the_end(
        self, budget_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_claude(
            budget_home, _CLAUDE_ID, [r for n in range(200) for r in _claude_turn(n)]
        )
        session = load_local_session("claude", _CLAUDE_ID)
        full = _full_read("claude", _CLAUDE_ID, monkeypatch)
        kept = len(session.items) - 1
        assert 0 < kept < len(full.items)
        assert session.items[:kept] == full.items[:kept]
        assert _notice(session).message == (
            f"This session was too long to import in full, so only its first {kept:,} items were "
            "imported; later history was left out."
        )
        assert (session.trimmed_item_count, session.later_history_omitted) == (0, True)
        assert session.title == "turn 0"
        # Only about the budget was parsed: the kept records fit in it.
        assert kept * 100 < path.stat().st_size
        _assert_importable(session)

    def test_reading_stops_at_the_item_cap(
        self, budget_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_claude(budget_home, _CLAUDE_ID, [r for n in range(200) for r in _claude_turn(n)])
        monkeypatch.setattr(local_module, "IMPORT_READ_BUDGET_BYTES", 100_000)
        monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 10)
        session = load_local_session("claude", _CLAUDE_ID)
        full = _full_read("claude", _CLAUDE_ID, monkeypatch)
        assert len(session.items) == 10
        assert session.items[:9] == full.items[:9]
        assert session.later_history_omitted is True

    def test_a_full_cap_says_later_history_only_when_items_follow(
        self, budget_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A cap of 5 leaves room for 4 items + the notice. Reading continues past
        # the cap only to see whether anything importable follows.
        monkeypatch.setattr(local_module, "IMPORT_READ_BUDGET_BYTES", 1 << 20)
        records: list[dict[str, Any] | str] = [
            *_claude_turn(0),
            '{"type":"ai-title","aiTitle":"Only"}',
        ]
        path = _write_claude(budget_home, _CLAUDE_ID, records)
        more = _write_claude(
            budget_home, "b2c3d4e5-1234-5678-9abc-def012345671", [*records, *_claude_turn(1)]
        )
        monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 5)
        items, stopped_early = local_module._read_claude_head(path, 0)
        assert (len(items), stopped_early) == (4, False)
        items, stopped_early = local_module._read_claude_head(more, 0)
        assert (len(items), stopped_early) == (4, True)

    def test_a_giant_first_record_is_imported_whole(self, budget_home: Path) -> None:
        giant = "pasted " + "A" * 20_000
        records = [
            *_claude_turn(0, prompt=giant),
            *[r for n in range(1, 50) for r in _claude_turn(n)],
        ]
        _write_claude(budget_home, _CLAUDE_ID, records)
        session = load_local_session("claude", _CLAUDE_ID)
        assert _text(session.items[0]) == giant
        assert session.later_history_omitted is True
        _assert_importable(session)

    def test_a_giant_later_record_ends_the_read_before_it(self, budget_home: Path) -> None:
        records = [
            *_claude_turn(0),
            {"type": "user", "message": {"role": "user", "content": "B" * 20_000}},
        ]
        records += [r for n in range(1, 5) for r in _claude_turn(n)]
        _write_claude(budget_home, _CLAUDE_ID, records)
        session = load_local_session("claude", _CLAUDE_ID)
        assert [i.type for i in session.items] == [
            "message",
            "message",
            "function_call",
            "function_call_output",
            "error",
        ]
        assert session.later_history_omitted is True

    def test_a_partial_trailing_line_past_the_budget_only_ends_the_read(
        self, budget_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # (Within the budget a partial line is skipped like the offset reader
        # does; see test_budgeted_parser_matches_the_offset_reader.)
        records = [r for n in range(5) for r in _claude_turn(n)]
        path = _write_claude(
            budget_home, _CLAUDE_ID, records, tail='{"type":"assistant","message":{"role":"assis'
        )
        monkeypatch.setattr(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size - 10)
        session = load_local_session("claude", _CLAUDE_ID)
        assert len(session.items) == 21 and session.later_history_omitted is True
        _assert_importable(session)

    def test_one_giant_turn_is_cut_at_the_end(
        self, budget_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = [_claude_turn(0)[0], *[r for n in range(300) for r in _claude_tool_step(n)]]
        _write_claude(budget_home, _CLAUDE_ID, records)
        for cap in (10, 11):
            monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", cap)
            monkeypatch.setattr(local_module, "IMPORT_READ_BUDGET_BYTES", 1 << 20)
            session = load_local_session("claude", _CLAUDE_ID)
            assert _text(session.items[0]) == "turn 0" and session.title == "turn 0"
            assert len(session.items) == cap
            # cap 10 ends on a call whose output was left out; both still import.
            assert session.items[-2].type == (
                "function_call" if cap == 10 else "function_call_output"
            )
            _assert_importable(session)

    def test_a_session_of_only_tool_items_imports_untitled(self, budget_home: Path) -> None:
        _write_claude(
            budget_home, _CLAUDE_ID, [r for n in range(300) for r in _claude_tool_step(n)]
        )
        session = load_local_session("claude", _CLAUDE_ID)
        assert not any(
            isinstance(i.data, MessageData) and i.data.role == "user" for i in session.items
        )
        assert session.title is None
        _assert_importable(session)

    def test_starts_at_the_last_compaction_like_the_full_read(self, budget_home: Path) -> None:
        records = [r for n in range(100) for r in _claude_turn(n)]
        records[60:60] = [_summary("first summary")]
        records[-30:-30] = [_summary("last summary")]
        path = _write_claude(budget_home, _CLAUDE_ID, records)
        with patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size // 2):
            session = load_local_session("claude", _CLAUDE_ID)
            full = _full_read("claude", _CLAUDE_ID, patch)
        assert _text(session.items[0]) == "last summary" and _msg(session.items[0]).is_meta
        assert not _has_notice(session)
        assert [(i.type, i.data) for i in session.items] == [(i.type, i.data) for i in full.items]
        assert session.title == full.title

    def test_compaction_markers_that_are_not_boundaries_are_skipped(
        self, budget_home: Path
    ) -> None:
        quoted = {
            "type": "user",
            "message": {"role": "user", "content": 'look: {"isCompactSummary":true}'},
        }
        sidechain = {**_summary("sidechain summary"), "isSidechain": True}
        records = [r for n in range(50) for r in _claude_turn(n)]
        records[60:60] = [_summary("real summary")]
        records[100:100] = [quoted, sidechain]
        path = _write_claude(budget_home, _CLAUDE_ID, records)
        assert local_module._last_claude_compaction_offset(path) > 0
        with patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size // 2):
            session = load_local_session("claude", _CLAUDE_ID)
        assert _text(session.items[0]) == "real summary"

    def test_compaction_then_budget(self, budget_home: Path) -> None:
        records = [r for n in range(200) for r in _claude_turn(n)]
        records[30:30] = [_summary("summary")]
        _write_claude(budget_home, _CLAUDE_ID, records)
        session = load_local_session("claude", _CLAUDE_ID)
        assert _text(session.items[0]) == "summary"
        assert session.later_history_omitted is True
        assert session.title == "turn 10"

    def test_native_title_comes_from_the_whole_file(self, budget_home: Path) -> None:
        records: list[dict[str, Any] | str] = [r for n in range(200) for r in _claude_turn(n)]
        records.append(
            json.dumps({"type": "custom-title", "customTitle": "Renamed", "sessionId": _CLAUDE_ID})
        )
        _write_claude(budget_home, _CLAUDE_ID, records)
        session = load_local_session("claude", _CLAUDE_ID)
        assert session.later_history_omitted is True
        assert session.title == "Renamed"


class TestCodexBudget:
    def test_codex_stops_at_the_budget(
        self, budget_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_codex(budget_home, _CODEX_ID, [line for n in range(150) for line in _codex_turn(n)])
        session = load_local_session("codex", _CODEX_ID)
        full = _full_read("codex", _CODEX_ID, monkeypatch)
        kept = len(session.items) - 1
        assert 0 < kept < len(full.items)
        assert session.items[:kept] == full.items[:kept]
        assert _notice(session).message.endswith("later history was left out.")
        # The injected <environment_context> message is meta, so the title is the first prompt.
        assert session.title == "turn 0" and session.workspace == "/repo"
        _assert_importable(session)

    def test_codex_stops_at_the_item_cap(
        self, budget_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_codex(budget_home, _CODEX_ID, [line for n in range(150) for line in _codex_turn(n)])
        monkeypatch.setattr(local_module, "IMPORT_READ_BUDGET_BYTES", 50_000)
        monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 6)
        session = load_local_session("codex", _CODEX_ID)
        full = _full_read("codex", _CODEX_ID, monkeypatch)
        assert session.items[:5] == full.items[:5] and len(session.items) == 6
        assert session.later_history_omitted is True

    def test_codex_starts_at_the_last_compaction_like_the_full_read(
        self, budget_home: Path
    ) -> None:
        lines = [line for n in range(150) for line in _codex_turn(n)]
        lines[100:100] = [_codex_compacted("first summary")]
        lines[-30:-30] = [_codex_compacted("last summary")]
        lines.append(_codex_line("session_meta", {"id": _CODEX_ID, "cwd": "/moved"}))
        path = _write_codex(budget_home, _CODEX_ID, lines)
        with patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size // 2):
            session = load_local_session("codex", _CODEX_ID)
            full = _full_read("codex", _CODEX_ID, patch)
        assert _text(session.items[0]) == "last summary" and not _has_notice(session)
        # Same items and turn ids, and the last session_meta cwd, as the full read.
        assert session.items == full.items
        assert session.workspace == full.workspace == "/moved"

    def test_codex_giant_first_record_and_partial_tail(self, budget_home: Path) -> None:
        lines = [
            *_codex_turn(0, prompt="pasted " + "D" * 20_000),
            *[line for n in range(1, 60) for line in _codex_turn(n)],
        ]
        _write_codex(
            budget_home,
            _CODEX_ID,
            lines,
            tail='{"timestamp":"x","type":"response_item","payl',
            internal=False,
        )
        session = load_local_session("codex", _CODEX_ID)
        assert _text(session.items[0]).startswith("pasted DDD")
        assert session.later_history_omitted is True
        _assert_importable(session)

    def test_codex_giant_later_record_ends_the_read_before_it(self, budget_home: Path) -> None:
        lines = [*_codex_turn(0), *_codex_turn(1, prompt="E" * 20_000), *_codex_turn(2)]
        _write_codex(budget_home, _CODEX_ID, lines)
        session = load_local_session("codex", _CODEX_ID)
        assert len(session.items) == 1 + 4 + 1 and session.later_history_omitted is True

    def test_codex_tool_items_only(self, budget_home: Path) -> None:
        steps = [
            _codex_line(
                "response_item",
                {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": f"c{n}"},
            )
            for n in range(400)
        ]
        _write_codex(budget_home, _CODEX_ID, steps)
        session = load_local_session("codex", _CODEX_ID)
        assert session.title is None
        _assert_importable(session)

    def test_codex_native_title_wins(self, budget_home: Path) -> None:
        _write_codex(budget_home, _CODEX_ID, [line for n in range(150) for line in _codex_turn(n)])
        index = budget_home / ".codex" / "session_index.jsonl"
        index.write_text(
            json.dumps({"id": _CODEX_ID, "thread_name": "Codex rename"}) + "\n", encoding="utf-8"
        )
        assert load_local_session("codex", _CODEX_ID).title == "Codex rename"
