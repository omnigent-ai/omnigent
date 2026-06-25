"""Unit tests for the goose-native usage forwarder.

Seeds a minimal goose ``sessions`` row (the ``accumulated_*`` + cost columns the
poller reads) and exercises the read → post-body → dedup path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from omnigent import goose_native_usage as u

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    accumulated_input_tokens INTEGER,
    accumulated_output_tokens INTEGER,
    accumulated_total_tokens INTEGER,
    accumulated_cost REAL,
    model_config_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_MODEL_CFG = json.dumps({"model_name": "claude-haiku-4-5-20251001", "context_limit": 200000})


def _seed(path: Path, **cols: object) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    row = {
        "id": "gs1",
        "name": "omni-1",
        "accumulated_input_tokens": 21052,
        "accumulated_output_tokens": 262,
        "accumulated_total_tokens": 21314,
        "accumulated_cost": 0.022362,
        "model_config_json": _MODEL_CFG,
    }
    row.update(cols)
    con.execute(
        "INSERT INTO sessions(id, name, accumulated_input_tokens, accumulated_output_tokens, "
        "accumulated_total_tokens, accumulated_cost, model_config_json) VALUES (?,?,?,?,?,?,?)",
        (
            row["id"],
            row["name"],
            row["accumulated_input_tokens"],
            row["accumulated_output_tokens"],
            row["accumulated_total_tokens"],
            row["accumulated_cost"],
            row["model_config_json"],
        ),
    )
    con.commit()
    con.close()


def test_read_session_usage_reads_accumulated_columns(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)
    usage = u.read_session_usage(db, "gs1")
    assert usage is not None
    assert usage.input_tokens == 21052
    assert usage.output_tokens == 262
    assert usage.total_tokens == 21314
    assert usage.cost == 0.022362
    assert usage.model == "claude-haiku-4-5-20251001"
    assert usage.has_usage()


def test_read_session_usage_missing_row_is_none(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)
    assert u.read_session_usage(db, "nope") is None


def test_read_session_usage_handles_nulls(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(
        db,
        accumulated_input_tokens=None,
        accumulated_output_tokens=None,
        accumulated_total_tokens=None,
        accumulated_cost=None,
        model_config_json=None,
    )
    usage = u.read_session_usage(db, "gs1")
    assert usage is not None
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cost is None
    assert usage.model is None
    assert not usage.has_usage()  # nothing to report yet


def test_model_from_config_json() -> None:
    assert u._model_from_config_json(_MODEL_CFG) == "claude-haiku-4-5-20251001"
    assert u._model_from_config_json(json.dumps({"model": "gpt-5"})) == "gpt-5"
    assert u._model_from_config_json("not json") is None
    assert u._model_from_config_json(None) is None


def test_usage_post_body_forwards_cost_and_tokens() -> None:
    usage = u.GooseUsage(input_tokens=100, output_tokens=20, total_tokens=120, cost=0.5, model="m")
    body = u._usage_post_body(usage)
    assert body["cumulative_input_tokens"] == 100
    assert body["cumulative_output_tokens"] == 20
    # goose's authoritative cost drives BOTH display and the budget gate.
    assert body["cumulative_cost_usd"] == 0.5
    assert body["policy_cost_usd"] == 0.5
    assert body["model"] == "m"


def test_usage_post_body_omits_cost_when_absent() -> None:
    usage = u.GooseUsage(input_tokens=5, output_tokens=1, total_tokens=6, cost=None, model=None)
    body = u._usage_post_body(usage)
    assert "cumulative_cost_usd" not in body
    assert "policy_cost_usd" not in body
    assert "model" not in body
    assert body["cumulative_input_tokens"] == 5


def test_post_key_changes_drive_reposts() -> None:
    a = u.GooseUsage(1, 2, 3, 0.1, "m").post_key()
    b = u.GooseUsage(1, 2, 3, 0.1, "m").post_key()
    c = u.GooseUsage(1, 2, 4, 0.1, "m").post_key()  # tokens advanced
    assert a == b  # identical totals → no repost
    assert a != c  # advanced totals → repost


def test_last_key_roundtrip_and_clear(tmp_path: Path) -> None:
    key = u.GooseUsage(1, 2, 3, 0.1, "m").post_key()
    u._write_last_key(tmp_path, key)
    assert u._read_last_key(tmp_path) == key
    u.clear_goose_usage_state(tmp_path)
    assert u._read_last_key(tmp_path) is None
