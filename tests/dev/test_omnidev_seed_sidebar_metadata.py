"""The omnidev sidebar seeder only rewrites the fixture sessions it is given."""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine

from omnigent.db.db_models import ConversationBase

_SCRIPT = Path(__file__).resolve().parents[2] / "dev/omnidev/fixtures/seed_sidebar_metadata.py"
_REPO_0 = "omnigent.sandbox.repo.0"
_REPO_1 = "omnigent.sandbox.repo.1"


def _seeder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_sidebar_metadata", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chat_db(tmp_path: Path) -> Path:
    path = tmp_path / "chat.db"
    ConversationBase.metadata.create_all(create_engine(f"sqlite:///{path}"))
    return path


def _add_session(
    conn: sqlite3.Connection, created_at: int, labels: dict[str, str] | None = None
) -> str:
    cid = uuid.uuid4()
    conn.execute(
        "INSERT INTO conversations (id, created_at, updated_at, root_conversation_id, agent_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (cid.bytes, created_at, created_at, cid.bytes, uuid.uuid4().bytes),
    )
    for key, value in (labels or {}).items():
        conn.execute(
            "INSERT INTO conversation_labels (conversation_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (cid.bytes, key, value, created_at),
        )
    return cid.hex


def _snapshot(db: Path) -> dict[str, tuple[int, list[tuple[str, str]]]]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT id, updated_at FROM conversations").fetchall()
        return {
            bytes(cid).hex(): (
                updated,
                conn.execute(
                    "SELECT key, value FROM conversation_labels "
                    "WHERE conversation_id = ? ORDER BY key",
                    (cid,),
                ).fetchall(),
            )
            for cid, updated in rows
        }
    finally:
        conn.close()


def test_leaves_every_other_session_untouched(tmp_path: Path) -> None:
    db = _chat_db(tmp_path)
    conn = sqlite3.connect(db)
    # A user's older two-repo sandbox session, well outside the newest few.
    two_repo = _add_session(
        conn,
        1_000,
        {_REPO_0: "https://github.com/acme/api#main", _REPO_1: "https://github.com/acme/web"},
    )
    fixtures = [_add_session(conn, 2_000 + i) for i in range(3)]
    # User sessions created after the fixtures, one with a sandbox repo of its own.
    newer = [_add_session(conn, 3_000 + i) for i in range(8)]
    newer.append(_add_session(conn, 4_000, {_REPO_0: "https://github.com/acme/cli"}))
    conn.commit()
    conn.close()
    before = _snapshot(db)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(db), *fixtures],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "on 3 session(s)" in result.stdout
    after = _snapshot(db)
    for session_id in [two_repo, *newer]:
        assert after[session_id] == before[session_id]
    assert all(after[f][0] != before[f][0] for f in fixtures)
    # LAYOUT gives the first and third sessions a repo.
    assert after[fixtures[0]][1] == [(_REPO_0, "https://github.com/omnigent-ai/omnigent#main")]
    assert after[fixtures[1]][1] == []
    assert after[fixtures[2]][1] == [
        (_REPO_0, "https://github.com/omnigent-ai/web-ui#fix/sidebar")
    ]


def test_keeps_a_label_the_session_already_has(tmp_path: Path) -> None:
    db = _chat_db(tmp_path)
    conn = sqlite3.connect(db)
    fixture = _add_session(conn, 2_000, {_REPO_0: "https://github.com/acme/api#main"})
    conn.commit()
    conn.close()

    _seeder().seed(str(db), [fixture], datetime.now(UTC))

    assert _snapshot(db)[fixture][1] == [(_REPO_0, "https://github.com/acme/api#main")]


def test_dates_land_on_their_calendar_day_just_after_midnight() -> None:
    seeder = _seeder()
    # Just after midnight on a DST-change day.
    now = datetime(2026, 3, 29, 0, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
    for days_ago, _repo in seeder.LAYOUT:
        stamped = datetime.fromtimestamp(seeder.updated_at(days_ago, now), tz=now.tzinfo)
        assert stamped.date() == now.date() - timedelta(days=days_ago)


def test_layout_puts_a_few_sessions_in_every_updated_group() -> None:
    def group(days_ago: int) -> str:
        # The sidebar's Updated groups (web/src/shell/sidebarNav.ts updatedBucket).
        if days_ago == 0:
            return "Today"
        if days_ago == 1:
            return "Yesterday"
        if days_ago <= 7:
            return "Previous 7 days"
        return "Previous 30 days" if days_ago <= 30 else "Older"

    counts: dict[str, int] = {}
    for days_ago, _repo in _seeder().LAYOUT:
        counts[group(days_ago)] = counts.get(group(days_ago), 0) + 1
    assert counts == {
        "Today": 2,
        "Yesterday": 2,
        "Previous 7 days": 2,
        "Previous 30 days": 2,
        "Older": 2,
    }
