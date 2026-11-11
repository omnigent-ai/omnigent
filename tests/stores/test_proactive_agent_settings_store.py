"""Decoding of stored proactive-agent settings values."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from omnigent.db.compression import encode
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.proactive_agent_settings_store import (
    WORKSPACE_SETTINGS_USER,
    ProactiveAgentSettingsStore,
    _decode_enabled,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        (encode("true"), True),
        (encode("false"), False),
        (b"false", False),
        ("false", False),
        (memoryview(encode("false") or b""), False),
        # Unreadable values read as on: not JSON, the wrong JSON type, a
        # truncated frame, a corrupt compressed frame, and an oversized value.
        (b"not json", True),
        (encode('"yes"'), True),
        (encode("null"), True),
        (b"\x00", True),
        (b"\x00\x01not a zstd frame", True),
        (encode("[" + "0," * 100 + "0]"), True),
    ],
    ids=[
        "missing",
        "true",
        "false",
        "legacy-false",
        "sqlite-str-false",
        "memoryview-false",
        "not-json",
        "string",
        "null",
        "truncated-frame",
        "corrupt-compressed-frame",
        "oversized",
    ],
)
def test_decode_enabled(raw: bytes | str | memoryview | None, expected: bool) -> None:
    assert _decode_enabled(raw) is expected


def test_a_corrupt_stored_value_reads_as_on(db_uri: str) -> None:
    store = ProactiveAgentSettingsStore(db_uri)
    store.set_workspace_enabled(False, actor=None)
    assert store.read("alice") == (False, True)
    # Write past the column's codec: a framed value whose payload is not zstd.
    with Session(get_or_create_engine(db_uri)) as session:
        updated = session.execute(
            text(
                'UPDATE preferences SET value = :value WHERE user_id = :user_id AND "key" = :key'
            ),
            {
                "value": b"\x00\x01not a zstd frame",
                "user_id": WORKSPACE_SETTINGS_USER,
                "key": "proactive_agent.v1.enabled",
            },
        ).rowcount
        assert updated == 1
        session.commit()

    assert store.read("alice") == (True, True)
