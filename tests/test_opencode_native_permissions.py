"""Tests for OpenCode permission normalization + policy/approval mapping."""

from __future__ import annotations

# normalize_for_policy's v2 resource-shape test is rewritten in Task 48.
from omnigent.harnesses.opencode_native.permissions import (  # noqa: F401
    OPENCODE_NATIVE_HARNESS,
    decision_to_reply,
    map_verdict_to_decision,
    normalize_for_policy,
    parse_permission_request,
    reply_body,
)


def _asked(**overrides: object) -> dict[str, object]:
    """A v2 ``permission.asked`` payload as captured from opencode 2.0.x."""
    data: dict[str, object] = {
        "id": "per_1",
        "sessionID": "ses_1",
        "action": "shell",
        "resources": ["rm -rf build"],
        "save": ["rm *"],
        "source": {"type": "tool", "messageID": "msg_1", "id": "call_1"},
    }
    data.update(overrides)
    return data


def test_parse_permission_request_reads_v2_fields() -> None:
    req = parse_permission_request(_asked(metadata={"filepath": "a.py"}, message="why"))
    assert req is not None
    assert req.request_id == "per_1"
    assert req.session_id == "ses_1"
    assert req.action == "shell"
    assert req.resources == ["rm -rf build"]
    assert req.metadata == {"filepath": "a.py"}
    assert req.source == {"type": "tool", "messageID": "msg_1", "id": "call_1"}
    assert req.tool_call_id == "call_1"
    assert req.message_id == "msg_1"
    assert req.message == "why"


def test_parse_permission_request_ignores_v1_fields() -> None:
    """v1 ``permission``/``patterns`` are no longer read: the action stays unset."""
    req = parse_permission_request(
        {"id": "per_v1", "sessionID": "ses_1", "permission": "bash", "patterns": ["ls"]}
    )
    assert req is not None
    assert req.action is None
    assert req.resources == []


def test_parse_permission_request_drops_non_string_resources() -> None:
    req = parse_permission_request(_asked(resources=["a.py", {"path": "b"}, 3]))
    assert req is not None
    assert req.resources == ["a.py"]


def test_parse_permission_request_without_source() -> None:
    req = parse_permission_request(_asked(source=None))
    assert req is not None
    assert req.source is None
    assert req.tool_call_id is None


def test_parse_permission_request_requires_id() -> None:
    assert parse_permission_request({"action": "shell"}) is None
    assert parse_permission_request({"requestID": "per_2", "action": "edit"}) is None


def test_map_verdict_allow_variants() -> None:
    assert map_verdict_to_decision({"decision": "allow"}) == "allow_once"
    assert map_verdict_to_decision({"action": "approve"}) == "allow_once"
    assert map_verdict_to_decision({"decision": "allow_always"}) == "allow_always"
    assert map_verdict_to_decision({"decision": "always"}) == "allow_always"


def test_map_verdict_deny_variants() -> None:
    assert map_verdict_to_decision({"decision": "deny"}) == "reject"
    assert map_verdict_to_decision({"verdict": "block"}) == "reject"


def test_map_verdict_unknown_fails_closed_to_ask() -> None:
    assert map_verdict_to_decision(None) == "ask"
    assert map_verdict_to_decision({}) == "ask"
    assert map_verdict_to_decision({"decision": "maybe"}) == "ask"


def test_decision_to_reply() -> None:
    assert decision_to_reply("allow_once") == "once"
    # allow_always must map to "once", NOT "always": an "always" reply makes
    # opencode persist the grant locally and stop emitting permission.asked,
    # bypassing the server policy engine and breaking live policy toggles.
    assert decision_to_reply("allow_always") == "once"
    assert decision_to_reply("reject") == "reject"
    # ask has no automatic reply (needs a human).
    assert decision_to_reply("ask") is None


def test_reply_body() -> None:
    assert reply_body("once") == {"reply": "once"}
    assert reply_body("reject", message="blocked by policy") == {
        "reply": "reject",
        "message": "blocked by policy",
    }
