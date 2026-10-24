"""Tests for the mod turn-event schema and its incremental reader.

Covers :mod:`omnigent.harnesses.claude_native.mod_events`: validation of the
``mod.json`` heartbeat and ``mod_events.jsonl`` records, the freshness and pid
check, and the byte-cursor reader's handling of partial lines, unknown records,
truncation, and recreation.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.mod_events import (
    MOD_EVENTS_FILE_NAME,
    MOD_HEARTBEAT_FUTURE_SKEW_S,
    MOD_HEARTBEAT_MAX_AGE_S,
    ModEventsCursor,
    ModHeartbeat,
    ModTurnComplete,
    ModTurnStart,
    heartbeat_is_current,
    parse_mod_event,
    parse_mod_heartbeat,
    read_mod_events,
    read_server_pid,
)
from tests.harnesses.claude_native.mod_fixtures import (
    append_event_record,
    append_event_text,
    turn_complete_record,
    turn_start_record,
    write_server_state,
)

NOW = 1_785_480_000.0


def _heartbeat_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "mod_version": "0.1.0",
        "claude_version": "2.1.301",
        "claude_session_id": "claude-mod",
        "main_turn_id": None,
        "server_pid": 77,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return payload


def test_heartbeat_parses_contract_fields_and_defaults_optional_text() -> None:
    """A v1 heartbeat parses; missing diagnostic strings become ``None``."""
    heartbeat = parse_mod_heartbeat(
        {"v": 1, "server_pid": 77, "updated_at": NOW, "main_turn_id": None}
    )
    assert heartbeat == ModHeartbeat(
        mod_version=None,
        claude_version=None,
        claude_session_id=None,
        main_turn_id=None,
        server_pid=77,
        updated_at=NOW,
    )
    assert parse_mod_heartbeat(_heartbeat_payload()).server_pid == 77


@pytest.mark.parametrize(
    ("main_turn_id", "expected"),
    [(None, None), ("t9", "t9")],
    ids=["no-turn-running", "turn-running"],
)
def test_heartbeat_carries_the_main_turn_the_mod_believes_is_running(
    main_turn_id: str | None, expected: str | None
) -> None:
    """Null means the mod believes no main-loop turn is running; a string names the turn."""
    heartbeat = parse_mod_heartbeat(_heartbeat_payload(main_turn_id=main_turn_id))
    assert heartbeat is not None and heartbeat.main_turn_id == expected


def test_heartbeat_without_the_main_turn_field_is_not_usable() -> None:
    """A heartbeat that omits ``main_turn_id`` is rejected, so the mod is not used."""
    payload = {key: value for key, value in _heartbeat_payload().items() if key != "main_turn_id"}
    assert parse_mod_heartbeat(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        _heartbeat_payload(v=2),
        _heartbeat_payload(v=True),
        _heartbeat_payload(v="1"),
        _heartbeat_payload(server_pid=True),
        _heartbeat_payload(server_pid="77"),
        _heartbeat_payload(updated_at=None),
        _heartbeat_payload(updated_at=math.nan),
        _heartbeat_payload(updated_at=math.inf),
        _heartbeat_payload(main_turn_id=7),
        _heartbeat_payload(main_turn_id=True),
        "not-an-object",
    ],
    ids=[
        "future-version",
        "boolean-version",
        "string-version",
        "boolean-pid",
        "string-pid",
        "missing-time",
        "nan-time",
        "infinite-time",
        "numeric-turn-id",
        "boolean-turn-id",
        "not-a-mapping",
    ],
)
def test_heartbeat_rejects_incompatible_or_incomplete_payloads(payload: object) -> None:
    """Anything short of a v1 heartbeat with an int pid, finite time, and turn field is dropped."""
    assert parse_mod_heartbeat(payload) is None


def test_event_parses_turn_records_and_ignores_unknown_fields() -> None:
    """Known kinds parse to typed records; extra fields do not matter."""
    start = parse_mod_event({**turn_start_record("t1", at=10.0), "future_field": {"nested": True}})
    complete = parse_mod_event(
        turn_complete_record("t1", at=11.5, reason="aborted") | {"extra": 1}
    )
    assert start == ModTurnStart(
        at=10.0, claude_session_id="claude-mod", turn_id="t1", agent_id=None
    )
    assert complete == ModTurnComplete(
        at=11.5,
        claude_session_id="claude-mod",
        turn_id="t1",
        agent_id=None,
        reason="aborted",
    )


def test_event_keeps_subagent_identity_for_the_caller_to_filter() -> None:
    """The schema reports the agent id; filtering it out is the poller's decision."""
    event = parse_mod_event(turn_start_record("sub-1", at=3.0, agent_id="agent-7"))
    assert event is not None and event.agent_id == "agent-7"


@pytest.mark.parametrize(
    "record",
    [
        {"v": 1, "kind": "turn.paused", "at": 1.0, "turn_id": "t1", "agent_id": None},
        turn_start_record("t1", at=1.0) | {"v": 2},
        turn_start_record("t1", at=1.0) | {"v": False},
        {k: v for k, v in turn_start_record("t1", at=1.0).items() if k != "agent_id"},
        {k: v for k, v in turn_start_record("t1", at=1.0).items() if k != "turn_id"},
        turn_start_record("t1", at=1.0) | {"at": "soon"},
        turn_start_record("t1", at=1.0) | {"agent_id": 9},
        "turn.start",
    ],
    ids=[
        "unknown-kind",
        "future-version",
        "boolean-version",
        "missing-agent-id",
        "missing-turn-id",
        "string-time",
        "numeric-agent-id",
        "not-a-mapping",
    ],
)
def test_event_drops_unknown_kinds_and_incomplete_records(record: object) -> None:
    """Unknown kinds, other schema versions, and records missing a field are ignored."""
    assert parse_mod_event(record) is None


@pytest.mark.parametrize(
    ("offset_s", "server_pid", "expected"),
    [
        (0.0, 77, True),
        (MOD_HEARTBEAT_MAX_AGE_S, 77, True),
        (MOD_HEARTBEAT_MAX_AGE_S + 0.5, 77, False),
        (-MOD_HEARTBEAT_FUTURE_SKEW_S, 77, True),
        (-(MOD_HEARTBEAT_FUTURE_SKEW_S + 0.5), 77, False),
        (-MOD_HEARTBEAT_MAX_AGE_S, 77, False),
        (0.0, 99, False),
        (0.0, None, False),
    ],
    ids=[
        "fresh",
        "exactly-max-age",
        "stale",
        "ahead-within-skew",
        "ahead-beyond-skew",
        "far-in-the-future",
        "pid-mismatch",
        "server-pid-unreadable",
    ],
)
def test_heartbeat_is_current_only_when_fresh_and_from_the_named_server(
    offset_s: float, server_pid: int | None, expected: bool
) -> None:
    """A heartbeat drives status only while fresh and written by the server in server.json."""
    heartbeat = parse_mod_heartbeat(_heartbeat_payload())
    assert heartbeat is not None
    assert heartbeat_is_current(heartbeat, server_pid=server_pid, now=NOW + offset_s) is expected


def test_heartbeat_is_not_current_when_absent() -> None:
    """No heartbeat means the mod is not usable."""
    assert heartbeat_is_current(None, server_pid=77, now=NOW) is False


def test_server_pid_reads_only_an_integer_pid(tmp_path: Path) -> None:
    """``server.json`` contributes its pid when it is a real integer."""
    assert read_server_pid(tmp_path) is None
    write_server_state(tmp_path, pid=77)
    assert read_server_pid(tmp_path) == 77
    (tmp_path / "server.json").write_text('{"pid": "77"}', encoding="utf-8")
    assert read_server_pid(tmp_path) is None
    (tmp_path / "server.json").write_text("{truncated", encoding="utf-8")
    assert read_server_pid(tmp_path) is None


def test_reader_returns_nothing_for_a_missing_file(tmp_path: Path) -> None:
    """A missing events file yields no events and a reset cursor."""
    result = read_mod_events(tmp_path / MOD_EVENTS_FILE_NAME, ModEventsCursor(12, "stale"))
    assert result.events == []
    assert result.cursor == ModEventsCursor()


def test_reader_yields_only_records_appended_since_the_cursor(tmp_path: Path) -> None:
    """Each read returns the new records and the cursor moves past them."""
    path = tmp_path / MOD_EVENTS_FILE_NAME
    append_event_record(tmp_path, turn_start_record("t1", at=1.0))
    append_event_record(tmp_path, turn_complete_record("t1", at=2.0))
    first = read_mod_events(path, ModEventsCursor())
    assert [type(event) for event in first.events] == [ModTurnStart, ModTurnComplete]
    assert first.cursor.byte_offset == path.stat().st_size

    append_event_record(tmp_path, turn_start_record("t2", at=3.0))
    second = read_mod_events(path, first.cursor)
    assert [event.turn_id for event in second.events] == ["t2"]
    assert read_mod_events(path, second.cursor).events == []


def test_partial_trailing_record_waits_for_its_newline(tmp_path: Path) -> None:
    """A record without its newline is not consumed until the writer finishes it."""
    path = tmp_path / MOD_EVENTS_FILE_NAME
    line = json.dumps(turn_complete_record("t1", at=2.0))
    append_event_text(tmp_path, line[:20])
    partial = read_mod_events(path, ModEventsCursor())
    assert partial.events == []
    assert partial.cursor.byte_offset == 0

    append_event_text(tmp_path, line[20:] + "\n")
    completed = read_mod_events(path, partial.cursor)
    assert [type(event) for event in completed.events] == [ModTurnComplete]


def test_malformed_and_unknown_lines_are_skipped_and_consumed(tmp_path: Path) -> None:
    """Bad lines are passed over once, so they are not retried forever."""
    path = tmp_path / MOD_EVENTS_FILE_NAME
    append_event_record(tmp_path, turn_start_record("t1", at=1.0))
    append_event_text(tmp_path, "{not json\n")
    append_event_text(tmp_path, '{"v": 1, "kind": "turn.paused", "at": 1.5}\n')
    with path.open("ab") as handle:
        handle.write(b"\xff\xfe\n")
    append_event_record(tmp_path, turn_complete_record("t1", at=2.0, reason="refusal"))

    result = read_mod_events(path, ModEventsCursor())
    assert [type(event) for event in result.events] == [ModTurnStart, ModTurnComplete]
    assert result.events[1].reason == "refusal"
    assert read_mod_events(path, result.cursor).events == []


def test_truncated_file_is_read_again_from_the_beginning(tmp_path: Path) -> None:
    """A file shorter than the cursor restarts the read instead of seeking past its end."""
    path = tmp_path / MOD_EVENTS_FILE_NAME
    append_event_record(tmp_path, turn_start_record("old-turn", at=1.0))
    first = read_mod_events(path, ModEventsCursor())
    path.write_text("", encoding="utf-8")
    append_event_record(tmp_path, turn_start_record("new", at=9.0))

    result = read_mod_events(path, first.cursor)
    assert [event.turn_id for event in result.events] == ["new"]


def test_recreated_file_is_read_from_its_start_even_when_longer(tmp_path: Path) -> None:
    """A replacement that grows past the old cursor is still read in full.

    Without the fingerprint, the cursor would seek into the middle of the new
    file and drop its first record.
    """
    path = tmp_path / MOD_EVENTS_FILE_NAME
    append_event_record(tmp_path, turn_start_record("gone", at=1.0))
    first = read_mod_events(path, ModEventsCursor())
    path.unlink()
    append_event_record(tmp_path, turn_start_record("replacement-a", at=50.0))
    append_event_record(tmp_path, turn_complete_record("replacement-a", at=51.0))

    result = read_mod_events(path, first.cursor)
    assert [(type(event), event.turn_id) for event in result.events] == [
        (ModTurnStart, "replacement-a"),
        (ModTurnComplete, "replacement-a"),
    ]


def test_missing_file_resets_the_cursor_for_a_later_recreation(tmp_path: Path) -> None:
    """After the file disappears, its next incarnation is read from the start."""
    path = tmp_path / MOD_EVENTS_FILE_NAME
    append_event_record(tmp_path, turn_start_record("t1", at=1.0))
    first = read_mod_events(path, ModEventsCursor())
    path.unlink()
    gone = read_mod_events(path, first.cursor)
    assert gone.events == [] and gone.cursor == ModEventsCursor()

    append_event_record(tmp_path, turn_start_record("t2", at=2.0))
    back = read_mod_events(path, gone.cursor)
    assert [event.turn_id for event in back.events] == ["t2"]
