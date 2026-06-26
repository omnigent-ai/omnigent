"""Tests for OpenCode permission normalization + policy/approval mapping."""

from __future__ import annotations

from omnigent.opencode_native_permissions import (
    OPENCODE_NATIVE_HARNESS,
    decision_to_reply,
    map_verdict_to_decision,
    normalize_for_policy,
    parse_permission_request,
    reply_body,
)


def test_parse_permission_request_from_event_properties() -> None:
    req = parse_permission_request(
        {
            "id": "per_1",
            "sessionID": "ses_1",
            "action": "bash",
            "resources": [{"command": "rm -rf build"}],
            "metadata": {"path": "/repo/build"},
            "source": "tool",
        }
    )
    assert req is not None
    assert req.request_id == "per_1"
    assert req.session_id == "ses_1"
    assert req.action == "bash"
    assert req.source == "tool"


def test_parse_permission_request_v1_uses_permission_field() -> None:
    """opencode 1.17.x emits v1 ``permission.asked`` with the category in
    ``permission`` (not ``action``). Missing this left the policy tool name as
    the literal "permission" so no tool-name policy fired (e.g. "Require
    Approval for File & Shell Operations"). Live-verified payload shape.
    """
    req = parse_permission_request(
        {
            "id": "per_v1",
            "sessionID": "ses_1",
            "permission": "bash",
            "patterns": ["echo hello"],
            "metadata": {"command": "echo hello"},
            "always": ["echo *"],
            "tool": {"messageID": "msg_1", "callID": "call_1"},
        }
    )
    assert req is not None
    assert req.action == "bash"  # from the v1 ``permission`` field
    assert req.resources == ["echo hello"]  # from v1 ``patterns``


def test_parse_permission_request_accepts_request_id_alias() -> None:
    req = parse_permission_request({"requestID": "per_2", "action": "edit"})
    assert req is not None
    assert req.request_id == "per_2"


def test_parse_permission_request_requires_id() -> None:
    assert parse_permission_request({"action": "bash"}) is None


def test_normalize_for_policy_extracts_command_and_path() -> None:
    req = parse_permission_request(
        {
            "id": "per_1",
            "sessionID": "ses_1",
            "action": "bash",
            "resources": [{"command": "ls", "path": "/repo/x"}],
        }
    )
    assert req is not None
    normalized = normalize_for_policy(req, omnigent_session_id="conv_1", workspace="/repo")
    assert normalized["harness"] == OPENCODE_NATIVE_HARNESS
    assert normalized["action"] == "bash"
    assert normalized["command"] == "ls"
    assert normalized["path"] == "/repo/x"
    assert normalized["working_directory"] == "/repo"
    assert normalized["omnigent_session_id"] == "conv_1"
    assert normalized["opencode_session_id"] == "ses_1"


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
