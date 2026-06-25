"""Unit tests for the goose-native post-hoc tool-result policy audit."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from omnigent import goose_native_audit as a

_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '');
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL
);
"""


def _request_part(rid: str, name: str, args: dict) -> dict:
    return {
        "type": "toolRequest",
        "id": rid,
        "toolCall": {"status": "success", "value": {"name": name, "arguments": args}},
        "_meta": {"goose_extension": "developer"},
    }


def _response_part(rid: str, text: str) -> dict:
    return {
        "type": "toolResponse",
        "id": rid,
        "toolResult": {
            "status": "success",
            "value": {"content": [{"type": "text", "text": text}], "isError": False},
        },
    }


def _seed(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO sessions(id, name) VALUES('gs1', 'omni-1')")
    con.execute(
        "INSERT INTO messages(session_id, role, content_json) VALUES (?,?,?)",
        ("gs1", "assistant", json.dumps([_request_part("t1", "shell", {"command": "ls"})])),
    )
    con.execute(
        "INSERT INTO messages(session_id, role, content_json) VALUES (?,?,?)",
        ("gs1", "user", json.dumps([_response_part("t1", "file-a\nfile-b")])),
    )
    con.commit()
    con.close()


def test_read_new_tool_results_correlates_request_and_response(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)
    results = a.read_new_tool_results(db, "gs1", 0)
    assert len(results) == 1
    msg_id, name, args, text = results[0]
    assert name == "shell"
    assert args == {"command": "ls"}
    assert text == "file-a\nfile-b"
    # The high-water id is the response row's id (the later of the two).
    assert msg_id == 2


def test_read_new_tool_results_respects_last_id(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)
    assert a.read_new_tool_results(db, "gs1", 2) == []  # nothing past the response row


def test_read_new_tool_results_skips_uncorrelated_response(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO sessions(id, name) VALUES('gs1', 'omni-1')")
    # A response with no matching request in the window → nothing to audit against.
    con.execute(
        "INSERT INTO messages(session_id, role, content_json) VALUES (?,?,?)",
        ("gs1", "user", json.dumps([_response_part("orphan", "x")])),
    )
    con.commit()
    con.close()
    assert a.read_new_tool_results(db, "gs1", 0) == []


def test_result_text_flattens_content() -> None:
    part = _response_part("t1", "hello world")
    assert a._result_text(part) == "hello world"
    assert a._result_text({"type": "toolResponse", "id": "t1"}) == ""


class _Resp:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self.content = json.dumps(payload).encode()

    def json(self) -> dict:
        return json.loads(self.content)


class _Client:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _Resp(self._payload)


async def test_audit_one_denies_logs_warning(caplog) -> None:
    client = _Client({"result": "POLICY_ACTION_DENY", "reason": "no secrets"})
    with caplog.at_level("WARNING"):
        await a._audit_one(
            client,
            session_id="c",
            tool_name="shell",
            arguments={"command": "cat .env"},
            result_text="KEY=...",
        )
    assert client.posts[0][0].endswith("/policies/evaluate")
    assert client.posts[0][1]["event"]["type"] == "PHASE_TOOL_RESULT"
    assert any("result-phase policy POLICY_ACTION_DENY" in r.message for r in caplog.records)


async def test_audit_one_allow_is_silent(caplog) -> None:
    client = _Client({"result": "POLICY_ACTION_ALLOW"})
    with caplog.at_level("WARNING"):
        await a._audit_one(
            client, session_id="c", tool_name="shell", arguments={}, result_text="ok"
        )
    assert not [r for r in caplog.records if "result-phase policy" in r.message]


def test_last_id_roundtrip_and_clear(tmp_path: Path) -> None:
    a._write_last_id(tmp_path, 7)
    assert a._read_last_id(tmp_path) == 7
    a.clear_goose_audit_state(tmp_path)
    assert a._read_last_id(tmp_path) == 0
