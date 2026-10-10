"""Build Codex rollout JSONL files and thread-store rows for import tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import NamedTuple


def codex_message(role: str, text: str) -> dict[str, object]:
    """A Codex ``message`` response item, as a rollout's ``response_item`` payload."""
    content_type = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}


class CodexRollout:
    """One Codex rollout whose records carry consecutive ordinals.

    Numbering starts at ``start_ordinal``, else at the ``history_base`` cutoff (where Codex
    numbers a fork's records from), else at 0. ``meta`` fills the ``session_meta`` payload.
    """

    def __init__(
        self, thread_id: str, *, start_ordinal: int | None = None, **meta: object
    ) -> None:
        if start_ordinal is None:
            base = meta.get("history_base")
            cutoff = base.get("end_ordinal_exclusive") if isinstance(base, dict) else None
            start_ordinal = (
                cutoff if isinstance(cutoff, int) and not isinstance(cutoff, bool) else 0
            )
        self.next_ordinal = start_ordinal
        self.records: list[dict[str, object]] = []
        self.append("session_meta", {"id": thread_id, **meta})

    def append(self, kind: str, payload: dict[str, object]) -> None:
        """Append one record with the next ordinal."""
        self.records.append({"ordinal": self.next_ordinal, "type": kind, "payload": payload})
        self.next_ordinal += 1

    def turn(self, index: int, user_text: str, assistant_text: str) -> None:
        """Append a turn: its ``turn_context`` plus a user and an assistant message."""
        self.append("turn_context", {"turn_id": f"turn_{index}"})
        self.append("response_item", codex_message("user", user_text))
        self.append("response_item", codex_message("assistant", assistant_text))

    def write(self, path: Path) -> None:
        """Write the rollout as JSONL, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{json.dumps(r)}\n" for r in self.records), encoding="utf-8")


class CodexThreadRow(NamedTuple):
    """One row of Codex's ``threads`` table in ``state_<n>.sqlite``."""

    id: str
    title: str
    first_user_message: str
    rollout_path: Path | None = None
    archived: bool = False


def write_codex_thread_store(
    codex_home: Path, rows: list[CodexThreadRow], *, legacy_schema: bool = False
) -> None:
    """Write ``codex_home/state_5.sqlite`` with ``rows`` in its ``threads`` table.

    ``legacy_schema`` writes an older table without the ``rollout_path``/``archived`` columns.
    """
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        if legacy_schema:
            con.execute("CREATE TABLE threads (id TEXT, title TEXT, first_user_message TEXT)")
            con.executemany(
                "INSERT INTO threads VALUES (?, ?, ?)",
                [(row.id, row.title, row.first_user_message) for row in rows],
            )
        else:
            con.execute(
                "CREATE TABLE threads (id TEXT, title TEXT, first_user_message TEXT, "
                "rollout_path TEXT, archived INTEGER)"
            )
            con.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        row.id,
                        row.title,
                        row.first_user_message,
                        str(row.rollout_path) if row.rollout_path else None,
                        int(row.archived),
                    )
                    for row in rows
                ],
            )
        con.commit()
    finally:
        con.close()
