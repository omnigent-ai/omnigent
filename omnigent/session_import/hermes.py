"""Hermes Agent session import (local state.db reader).

Hermes Agent (Nous Research) stores sessions and messages in a SQLite
database (``~/.hermes/state.db``; override with ``HERMES_HOME``). This reader
lists recent non-hidden parent sessions and normalizes one session's
user/assistant messages into import items. Tool activity is preserved only as
assistant-visible text where the transcript carries it — same fidelity tier as
Qwen/Kiro/Kimi imports.

The message table's ``role`` follows the chat-completions convention
(``user`` / ``assistant`` / ``tool`` / ``session_meta``); only ``user`` and
``assistant`` rows with non-empty content become items, keeping gateway
chats (Discord/Telegram/CLI) readable as a plain conversation.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from omnigent.session_import.models import (
    LocalSessionImport,
    SessionImportNotFoundError,
)

_ROLES_IMPORTABLE = ("user", "assistant")
_HERMES_COMMAND_TIMEOUT_SECONDS = 30.0


def hermes_state_db_path(hermes_home: Path | None = None) -> Path:
    """Resolve the Hermes state database path."""
    configured = os.environ.get("HERMES_HOME")
    home = hermes_home or (
        Path(configured).expanduser() if configured else Path.home() / ".hermes"
    )
    return home / "state.db"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SessionImportNotFoundError(f"Hermes state database not found at {path}")
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SessionImportNotFoundError(f"Hermes state database unreadable: {exc}") from exc


def list_recent_hermes_sessions(*, limit: int) -> list[tuple[str, float]]:
    """List recent ``(session_id, recency)`` pairs from the Hermes store.

    Skips hidden/archived rows and the high-churn ``cron`` source so an
    importer's "last N" surfaces interactive conversations.
    """
    try:
        db = _connect_readonly(hermes_state_db_path())
    except SessionImportNotFoundError:
        return []
    try:
        rows = db.execute(
            """
            SELECT id, last_activity_at FROM sessions
            WHERE hidden = 0 AND archived = 0 AND source != 'cron'
            ORDER BY last_activity_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [(str(row[0]), float(row[1])) for row in rows if row[0] and row[1]]


def load_hermes_session(
    session_id: str,
    *,
    hermes_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Hermes Agent session from the state database."""
    db = _connect_readonly(hermes_state_db_path(hermes_home))
    try:
        session_row = db.execute(
            "SELECT id, cwd, title, source FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session_row is None:
            raise SessionImportNotFoundError(f"Hermes session {session_id!r} was not found")
        _sid, cwd, title, _source = session_row
        message_rows = db.execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise SessionImportNotFoundError(
            f"Hermes session {session_id!r} could not be read: {exc}"
        ) from exc
    finally:
        db.close()

    from omnigent.entities import MessageData, NewConversationItem

    items: list[NewConversationItem] = []
    for row_number, (_mid, role, content) in enumerate(message_rows, start=1):
        if role not in _ROLES_IMPORTABLE or not isinstance(content, str) or not content.strip():
            continue
        # Hermes tool payloads arrive as JSON strings on tool rows; assistant
        # rows are plain text. Strip nothing — import the text verbatim.
        data = MessageData(
            role=role,
            content=[
                {
                    "type": "input_text" if role == "user" else "output_text",
                    "text": content,
                }
            ],
            is_meta=False,
            **({"agent": "hermes"} if role == "assistant" else {}),
        )
        items.append(
            NewConversationItem(
                type="message",
                response_id=f"hermes:{session_id}:{row_number}",
                data=data,
            )
        )
    if not items:
        raise SessionImportNotFoundError(
            f"Hermes session {session_id!r} has no importable history"
        )
    normalized = tuple(items)
    native_title = title.strip() if isinstance(title, str) and title.strip() else None
    workspace = cwd.strip() if isinstance(cwd, str) and cwd.strip() else None
    return LocalSessionImport(
        source="hermes",
        external_session_id=session_id,
        workspace=workspace,
        items=normalized,
        native_title=native_title,
    )
