"""Tests for the enum name↔int codecs.

These codecs are the single translation point between the string enum
names (the contract above the store layer) and the ``SMALLINT`` codes
persisted in the DB. A regression here would silently corrupt stored
enum values, so we assert round-trips, stability of the shipped codes,
fail-loud behaviour on unknown values, and — critically — that the item
type codes stay in lock-step with the app's item-type registry.
"""

from __future__ import annotations

import pytest

from omnigent.db import enum_codecs as ec
from omnigent.entities.conversation import ITEM_TYPE_TO_DATA_CLS

# Each entry: (name→code table, encode fn, decode fn).
_CODECS = [
    (ec.CONVERSATION_KIND, ec.encode_conversation_kind, ec.decode_conversation_kind),
    (ec.ITEM_TYPE, ec.encode_item_type, ec.decode_item_type),
    (ec.ITEM_STATUS, ec.encode_item_status, ec.decode_item_status),
    (ec.COMMENT_STATUS, ec.encode_comment_status, ec.decode_comment_status),
    (ec.ACCOUNT_TOKEN_KIND, ec.encode_account_token_kind, ec.decode_account_token_kind),
    (ec.POLICY_TYPE, ec.encode_policy_type, ec.decode_policy_type),
    (ec.POLICY_SCOPE, ec.encode_policy_scope, ec.decode_policy_scope),
    (ec.HOST_STATUS, ec.encode_host_status, ec.decode_host_status),
    (ec.AGENT_KIND, ec.encode_agent_kind, ec.decode_agent_kind),
    (
        ec.SCHEDULED_TASK_STATE,
        ec.encode_scheduled_task_state,
        ec.decode_scheduled_task_state,
    ),
    (
        ec.SCHEDULED_TASK_EXECUTION_TARGET,
        ec.encode_scheduled_task_execution_target,
        ec.decode_scheduled_task_execution_target,
    ),
    (
        ec.SCHEDULED_TASK_RUN_STATUS,
        ec.encode_scheduled_task_run_status,
        ec.decode_scheduled_task_run_status,
    ),
]


@pytest.mark.parametrize("table, encode, decode", _CODECS)
def test_round_trip_every_value(table, encode, decode) -> None:
    """Every name encodes to its code and decodes back unchanged."""
    for name, code in table.items():
        assert encode(name) == code
        assert decode(code) == name


@pytest.mark.parametrize("table, encode, decode", _CODECS)
def test_codes_are_unique(table, encode, decode) -> None:
    """Codes within a table are distinct (no two names share a code)."""
    assert len(set(table.values())) == len(table)


@pytest.mark.parametrize("table, encode, decode", _CODECS)
def test_unknown_name_raises(table, encode, decode) -> None:
    """Encoding an unknown name fails loud rather than persisting garbage."""
    with pytest.raises(ValueError):
        encode("definitely-not-a-real-value")


@pytest.mark.parametrize("table, encode, decode", _CODECS)
def test_unknown_code_raises(table, encode, decode) -> None:
    """Decoding an unknown code fails loud rather than returning None."""
    with pytest.raises(ValueError):
        decode(9999)


def test_item_type_codes_cover_data_classes() -> None:
    """
    ``ITEM_TYPE`` must cover exactly the app's item-type registry.

    A new item type added to ``ITEM_TYPE_TO_DATA_CLS`` without a code here
    would fail to persist; a stale code would mask a removed type. Keeping
    the key sets identical is the guard.
    """
    assert set(ec.ITEM_TYPE) == set(ITEM_TYPE_TO_DATA_CLS)


def test_shipped_codes_are_stable() -> None:
    """
    Pin the shipped codes so a reorder/renumber is caught in review.

    Codes are persisted on disk; changing one silently reinterprets every
    existing row. This test is the tripwire — update it only alongside a
    migration that rewrites the affected column.
    """
    assert ec.CONVERSATION_KIND == {"default": 1, "sub_agent": 2}
    assert ec.ITEM_TYPE == {
        "message": 1,
        "function_call": 2,
        "function_call_output": 3,
        "reasoning": 4,
        "error": 5,
        "compaction": 6,
        "native_tool": 7,
        "resource_event": 8,
        "routing_decision": 9,
        "slash_command": 10,
        "terminal_command": 11,
    }
    assert ec.ITEM_STATUS == {"completed": 1, "in_progress": 2, "incomplete": 3, "failed": 4}
    assert ec.COMMENT_STATUS == {"draft": 1, "addressed": 2}
    assert ec.ACCOUNT_TOKEN_KIND == {"invite": 1, "magic": 2}
    assert ec.POLICY_TYPE == {"python": 1, "url": 2}
    assert ec.POLICY_SCOPE == {"default": 1, "session": 2}
    assert ec.HOST_STATUS == {"online": 1, "offline": 2}
    assert ec.AGENT_KIND == {"template": 1, "session": 2}
    assert ec.SCHEDULED_TASK_STATE == {
        "active": 1,
        "paused": 2,
        "deleted": 3,
    }
    assert ec.SCHEDULED_TASK_EXECUTION_TARGET == {
        "connected_host": 1,
        "managed_sandbox": 2,
    }
    assert ec.SCHEDULED_TASK_RUN_STATUS == {
        "scheduled": 1,
        "running": 2,
        "succeeded": 3,
        "failed": 4,
        "skipped": 5,
    }
