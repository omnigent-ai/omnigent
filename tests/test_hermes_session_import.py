"""Hermes Agent session-import tests (state.db reader)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omnigent.session_import.hermes import (
    hermes_state_db_path,
    list_recent_hermes_sessions,
    load_hermes_session,
)
from omnigent.session_import.models import SessionImportNotFoundError

_SESSION_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    cwd TEXT,
    title TEXT,
    hidden INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    last_activity_at REAL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    content TEXT
);
"""


def _make_store(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    db = sqlite3.connect(db_path)
    db.executescript(_SESSION_DDL)
    db.execute(
        "INSERT INTO sessions (id, source, cwd, title, hidden, archived, last_activity_at)"
        " VALUES ('s_live', 'discord', '/work/repo', 'My chat', 0, 0, 2000.0)"
    )
    db.execute(
        "INSERT INTO sessions (id, source, hidden, archived, last_activity_at)"
        " VALUES ('s_cron', 'cron', 0, 0, 3000.0)"
    )
    db.execute(
        "INSERT INTO sessions (id, source, hidden, archived, last_activity_at)"
        " VALUES ('s_hidden', 'cli', 1, 0, 4000.0)"
    )
    db.execute(
        "INSERT INTO messages (session_id, role, content) VALUES ('s_live', 'user', 'hello')"
    )
    db.execute(
        "INSERT INTO messages (session_id, role, content)"
        " VALUES ('s_live', 'assistant', 'hi there')"
    )
    db.execute("INSERT INTO messages (session_id, role, content) VALUES ('s_live', 'tool', '{}')")
    db.execute("INSERT INTO messages (session_id, role, content) VALUES ('s_live', 'user', '')")
    db.commit()
    db.close()
    return db_path


def test_state_db_path_honors_hermes_home(tmp_path: Path) -> None:
    assert hermes_state_db_path(tmp_path) == tmp_path / "state.db"


def test_list_recent_sessions_skips_cron_and_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_store(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    listed = list_recent_hermes_sessions(limit=5)
    # s_hidden (hidden) and s_cron (cron source) are excluded; only s_live.
    assert listed == [("s_live", 2000.0)]


def test_list_recent_sessions_missing_db_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "absent"))
    assert list_recent_hermes_sessions(limit=5) == []


def test_load_session_normalizes_visible_messages(tmp_path: Path) -> None:
    _make_store(tmp_path)
    imported = load_hermes_session("s_live", hermes_home=tmp_path)
    assert imported.source == "hermes"
    assert imported.external_session_id == "s_live"
    assert imported.workspace == "/work/repo"
    assert imported.native_title == "My chat"
    roles = [item.data.role for item in imported.items]
    # The tool row and the empty user row are dropped.
    assert roles == ["user", "assistant"]
    user = imported.items[0].data
    assert user.content == [{"type": "input_text", "text": "hello"}]
    assistant = imported.items[1].data
    assert assistant.content == [{"type": "output_text", "text": "hi there"}]


def test_load_session_unknown_id_raises(tmp_path: Path) -> None:
    _make_store(tmp_path)
    with pytest.raises(SessionImportNotFoundError):
        load_hermes_session("missing", hermes_home=tmp_path)


def test_load_session_without_visible_messages_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = sqlite3.connect(db_path)
    db.executescript(_SESSION_DDL)
    db.execute(
        "INSERT INTO sessions (id, source, last_activity_at) VALUES ('s_empty', 'cli', 1.0)"
    )
    db.execute("INSERT INTO messages (session_id, role, content) VALUES ('s_empty', 'tool', '{}')")
    db.commit()
    db.close()
    with pytest.raises(SessionImportNotFoundError):
        load_hermes_session("s_empty", hermes_home=tmp_path)
