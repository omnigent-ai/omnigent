"""Wire validation supplements the existing preference-resolution matrix."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnigent.preference_sync import MAX_VALUE_BYTES, PreferenceMutation, SharedPreferenceKey


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol_version": 2},
        {"protocol_version": True},
        {"expected_version": True},
        {"expected_version": -1},
        {"identity": {"authority_id": "a", "user_id": "u", "workspace_id": " "}},
        {"operation_id": "not-a-uuid"},
        {"key": {"kind": "harness_args", "name": "default"}},
        {"key": {"kind": "model", "name": "token"}},
        {"key": {"kind": "plugin", "name": "ambiguous"}},
        {"value": False},
        {"value": ""},
        {"key": {"kind": "harness_args", "name": "codex-native"}, "value": [False]},
        {"key": {"kind": "harness_args", "name": "codex-native"}, "value": "--profile a"},
        {"value": "x" * 8192},
        {"scope": "machine"},
        {"resolves": ["00000000-0000-0000-0000-000000000001"] * 2},
    ],
)
def test_invalid_wire_operation_rejected(changes):
    body = {
        "protocol_version": 1,
        "identity": {"authority_id": "a", "user_id": "u", "workspace_id": "0"},
        "operation_id": str(uuid4()),
        "installation_id": "laptop",
        "key": {"kind": "model", "name": "model"},
        "expected_version": 0,
        "value": "model-a",
    }
    with pytest.raises(ValidationError):
        PreferenceMutation.model_validate({**body, **changes})


def test_aliases_share_storage_key():
    alias = SharedPreferenceKey(kind="harness_args", name="claude")
    canonical = SharedPreferenceKey(kind="harness_args", name="claude-sdk")
    assert alias == canonical
    assert (
        alias.storage_key()
        == canonical.storage_key()
        == ("25f0d34ecfddaff40d0ea0faf9834810ecf2579b6cce9513c47d3670fe888e5a")
    )
    plugin = SharedPreferenceKey(
        kind="plugin", name={"ecosystem": "claude", "source": "catalog", "name": "工具"}
    )
    assert plugin.storage_key() == (
        "264d16c6828ceb4bb8f938a5ae349358bd89719f0d115378412526ae5c1632e4"
    )


@pytest.mark.parametrize("character", ["x", "é"])
def test_value_envelope_accepts_exact_utf8_limit(character):
    value = character * ((MAX_VALUE_BYTES - len('{"value":""}')) // len(character.encode()))
    body = {
        "protocol_version": 1,
        "identity": {"authority_id": "a", "user_id": "u", "workspace_id": "0"},
        "operation_id": str(uuid4()),
        "installation_id": "laptop",
        "key": {"kind": "model", "name": "model"},
        "expected_version": 0,
        "value": value,
    }
    operation = PreferenceMutation.model_validate(body)
    assert len(operation.model_dump_json(include={"value"}).encode()) == MAX_VALUE_BYTES
    with pytest.raises(ValidationError, match="8192"):
        PreferenceMutation.model_validate({**body, "value": value + "x"})
