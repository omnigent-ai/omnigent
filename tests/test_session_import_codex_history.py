"""Codex imports read the thread Codex shows.

Laid out as Codex 0.163 writes it: rollouts under sessions/YYYY/MM/DD, a revert
or fork naming its new rollout ``rollout-<ts>-<thread>_<rollout>.jsonl`` with
``session_meta.history_base`` pointing at the base rollout id, and
``threads.rollout_path`` / ``threads.archived`` in ``state_<n>.sqlite``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities import CompactionData, MessageData, NewConversationItem
from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.server.routes.imports import _anchor_compactions
from omnigent.session_import import local as local_module
from omnigent.session_import.local import load_codex_session
from omnigent.session_import.models import SessionImportEmptyError, SessionImportNotFoundError

THREAD = "019f0001-0000-7000-8000-000000000001"
REVERT_ROLLOUT = "019f0001-0000-7000-8000-0000000000aa"
FORK = "019f0001-0000-7000-8000-000000000002"
SELF_BASED = "019f0001-0000-7000-8000-000000000003"


def _record(kind: str, payload: dict[str, Any], ordinal: int) -> str:
    return json.dumps(
        {"timestamp": "2026-10-21T00:00:00Z", "type": kind, "payload": payload, "ordinal": ordinal}
    )


def _meta(thread_id: str, ordinal: int = 0, history_base: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"id": thread_id, "cwd": "/repo", "source": "cli"}
    if history_base is not None:
        payload["history_base"] = history_base
    return _record("session_meta", payload, ordinal)


def _message(role: str, text: str, ordinal: int) -> str:
    part = "input_text" if role == "user" else "output_text"
    return _record(
        "response_item",
        {"type": "message", "role": role, "content": [{"type": part, "text": text}]},
        ordinal,
    )


def _compacted(ordinal: int, *history: dict[str, Any]) -> str:
    return _record(
        "compacted", {"message": "summary", "replacement_history": list(history)}, ordinal
    )


def _user_entry(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
        **extra,
    }


def _texts(items: tuple[NewConversationItem, ...]) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item.data, CompactionData):
            out.append("<compaction>")
        elif isinstance(item.data, MessageData) and not item.data.is_meta:
            parts = [
                c.get("text") if isinstance(c, dict) else getattr(c, "text", None)
                for c in item.data.content
            ]
            out.append(f"{item.data.role}:{''.join(p for p in parts if isinstance(p, str))}")
    return out


def _padding() -> list[str]:
    """Enough assistant output to push a rollout past the 2 MiB compaction trim."""
    return [_message("assistant", "x" * 1024, 10 + i) for i in range(2100)]


class _CodexHome:
    """A Codex home with rollouts and a ``state_5.sqlite`` thread store."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def rollout(
        self,
        name_ids: str,
        lines: list[str],
        *,
        mtime: float | None = None,
        archived: bool = False,
    ) -> Path:
        folder = self.path / ("archived_sessions" if archived else "sessions/2026/10/21")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-10-21T00-00-00-{name_ids}.jsonl"
        path.write_text("\n".join(lines) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def thread_row(
        self, thread_id: str, rollout: Path, *, archived: int = 0, title: str = ""
    ) -> None:
        con = sqlite3.connect(self.path / "state_5.sqlite")
        con.execute(
            "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT,"
            " archived INTEGER, title TEXT, first_user_message TEXT)"
        )
        con.execute(
            "INSERT OR REPLACE INTO threads VALUES (?, ?, ?, ?, ?)",
            (thread_id, str(rollout), archived, title, title),
        )
        con.commit()
        con.close()

    def reverted_thread(self) -> None:
        # Four records, then a revert to ordinal 3: Codex starts a new rollout
        # that inherits the first three and leaves the old file behind.
        self.rollout(
            THREAD,
            [
                _meta(THREAD),
                _message("user", "one", 1),
                _message("assistant", "1", 2),
                _message("user", "two", 3),
            ],
            mtime=2_000,
        )
        current = self.rollout(
            f"{THREAD}_{REVERT_ROLLOUT}",
            [
                _meta(THREAD, 3, {"thread_id": THREAD, "end_ordinal_exclusive": 3}),
                _message("user", "again", 4),
            ],
            mtime=1_000,
        )
        self.thread_row(THREAD, current)


@pytest.fixture
def codex(tmp_path: Path) -> _CodexHome:
    return _CodexHome(tmp_path / "codex")


def test_thread_store_rollout_wins_and_inherits_pre_revert_history(codex: _CodexHome) -> None:
    codex.reverted_thread()
    session = load_codex_session(THREAD, codex_home=codex.path)
    assert _texts(session.items) == ["user:one", "assistant:1", "user:again"]
    assert session.external_session_id == THREAD


def test_a_reverted_thread_is_listed_once_by_its_thread_id(
    codex: _CodexHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex.reverted_thread()
    monkeypatch.setattr(local_module, "_codex_storage_homes", lambda *_a, **_k: (codex.path,))
    listed = [
        sid for sid, _ in local_module._recent_local_sessions_with_recency("codex", limit=10)
    ]
    assert listed == [THREAD]


def test_without_sqlite_the_newest_filename_match_is_read(
    codex: _CodexHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex.reverted_thread()
    monkeypatch.setattr(local_module, "_codex_thread_row", lambda *_a: None)
    session = load_codex_session(THREAD, codex_home=codex.path)
    assert _texts(session.items) == ["user:one", "assistant:1", "user:two"]


def test_fork_inherits_its_base_up_to_the_cutoff(codex: _CodexHome) -> None:
    base = codex.rollout(
        THREAD, [_meta(THREAD), _message("user", "q", 1), _message("assistant", "a", 2)]
    )
    codex.thread_row(THREAD, base)
    fork = codex.rollout(
        FORK,
        [
            _meta(FORK, 2, {"thread_id": THREAD, "end_ordinal_exclusive": 2}),
            _message("user", "forked", 3),
        ],
    )
    codex.thread_row(FORK, fork)
    assert _texts(load_codex_session(FORK, codex_home=codex.path).items) == [
        "user:q",
        "user:forked",
    ]


def test_nested_cutoff_is_capped_by_the_outer_one_and_cycles_stop(codex: _CodexHome) -> None:
    codex.reverted_thread()
    fork = codex.rollout(
        FORK,
        [
            _meta(FORK, 2, {"thread_id": REVERT_ROLLOUT, "end_ordinal_exclusive": 2}),
            _message("user", "f", 3),
        ],
    )
    codex.thread_row(FORK, fork)
    # The base inherits ordinals < 3 from THREAD; the fork's cutoff of 2 caps that.
    assert _texts(load_codex_session(FORK, codex_home=codex.path).items) == ["user:one", "user:f"]
    cyclic = codex.rollout(
        SELF_BASED,
        [
            _meta(SELF_BASED, 0, {"thread_id": SELF_BASED, "end_ordinal_exclusive": 5}),
            _message("user", "self", 1),
        ],
    )
    codex.thread_row(SELF_BASED, cyclic)
    assert _texts(load_codex_session(SELF_BASED, codex_home=codex.path).items) == ["user:self"]


def test_under_two_mib_every_turn_and_compaction_imports(codex: _CodexHome) -> None:
    rollout = codex.rollout(
        THREAD,
        [
            _meta(THREAD),
            _message("user", "before", 1),
            _compacted(2, _user_entry("before")),
            _message("user", "after", 3),
        ],
    )
    codex.thread_row(THREAD, rollout)
    session = load_codex_session(THREAD, codex_home=codex.path)
    assert _texts(session.items) == ["user:before", "<compaction>", "user:after"]


def test_over_two_mib_starts_at_the_last_compaction(codex: _CodexHome) -> None:
    rollout = codex.rollout(
        THREAD,
        [
            _meta(THREAD),
            _message("user", "old", 1),
            _compacted(2, _user_entry("old")),
            *_padding(),
            _compacted(5000, _user_entry("recent")),
            _message("user", "after", 5001),
        ],
    )
    codex.thread_row(THREAD, rollout)
    assert _texts(load_codex_session(THREAD, codex_home=codex.path).items) == [
        "<compaction>",
        "user:after",
    ]


def test_a_thread_trimmed_to_its_compaction_keeps_its_thread_title(codex: _CodexHome) -> None:
    rollout = codex.rollout(
        THREAD,
        [
            _meta(THREAD),
            _message("user", "old", 1),
            *_padding(),
            _compacted(5000, _user_entry("s")),
        ],
    )
    codex.thread_row(THREAD, rollout, title="A long thread")
    session = load_codex_session(THREAD, codex_home=codex.path)
    assert _texts(session.items) == ["<compaction>"]
    assert session.native_title == "A long thread"


def test_compacted_messages_keep_only_model_input_entries_and_keys(codex: _CodexHome) -> None:
    rollout = codex.rollout(
        THREAD,
        [
            _meta(THREAD),
            _compacted(
                1,
                _user_entry(
                    "kept", id="codex-1", internal_chat_message_metadata_passthrough={"x": 1}
                ),
                {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
                {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": "{}",
                    "call_id": "c1",
                    "id": "fc-1",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            ),
        ],
    )
    codex.thread_row(THREAD, rollout)
    (compaction,) = load_codex_session(THREAD, codex_home=codex.path).items
    assert isinstance(compaction.data, CompactionData)
    assert compaction.data.compacted_messages == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "kept"}]},
        {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]


def test_archived_from_the_thread_store_or_archived_sessions(codex: _CodexHome) -> None:
    stored = codex.rollout(THREAD, [_meta(THREAD), _message("user", "hi", 1)])
    codex.thread_row(THREAD, stored, archived=1)
    assert load_codex_session(THREAD, codex_home=codex.path).archived
    moved = codex.rollout(FORK, [_meta(FORK), _message("user", "hi", 1)], archived=True)
    codex.thread_row(FORK, moved)
    assert load_codex_session(FORK, codex_home=codex.path).archived


def test_omnigent_session_id_resolves_through_the_native_bridge(
    codex: _CodexHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollout = codex.rollout(THREAD, [_meta(THREAD), _message("user", "native", 1)])
    codex.thread_row(THREAD, rollout)
    bridges = tmp_path / "bridges"
    (bridges / "b1").mkdir(parents=True)
    codex_bridge.write_bridge_state(
        bridges / "b1",
        codex_bridge.CodexNativeBridgeState(
            session_id="1615373409700099",
            socket_path="/tmp/x.sock",
            thread_id=THREAD,
            codex_home=str(codex.path),
        ),
    )
    monkeypatch.setattr(codex_bridge, "_BRIDGE_ROOT", bridges)
    monkeypatch.setenv("CODEX_HOME", str(codex.path))
    session = load_codex_session("1615373409700099")
    with pytest.raises(SessionImportNotFoundError):
        load_codex_session("1615373409700100")
    assert session.external_session_id == THREAD
    assert _texts(session.items) == ["user:native"]


def test_only_hidden_context_is_empty(codex: _CodexHome) -> None:
    codex.rollout(THREAD, [_meta(THREAD)])
    with pytest.raises(SessionImportEmptyError):
        load_codex_session(THREAD, codex_home=codex.path)


def test_each_compaction_points_at_the_item_before_it() -> None:
    def message(text: str) -> NewConversationItem:
        return NewConversationItem(
            type="message",
            response_id="r",
            data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
        )

    compaction = NewConversationItem(
        type="compaction",
        response_id="codex:compaction",
        data=CompactionData(
            summary="s", last_item_id="codex:compaction", token_count=0, compacted_messages=[]
        ),
    )
    first, before, second = _anchor_compactions([compaction, message("a"), compaction])
    assert isinstance(first.data, CompactionData) and isinstance(second.data, CompactionData)
    assert first.data.last_item_id == first.stable_id
    assert before.stable_id is not None
    assert second.data.last_item_id == before.stable_id
