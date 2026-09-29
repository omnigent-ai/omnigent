"""Tests for OpenCode permission normalization + policy/approval mapping."""

from __future__ import annotations

import pytest

from omnigent.harnesses.opencode_native.permissions import (
    OPENCODE_NATIVE_HARNESS,
    decision_to_reply,
    map_verdict_to_decision,
    normalize_for_policy,
    parse_permission_request,
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


def test_v1_reply_body_is_gone() -> None:
    """The v2 client owns the reply body (``{decision, message}``)."""
    import omnigent.harnesses.opencode_native.permissions as permissions

    assert not hasattr(permissions, "reply_body")


@pytest.mark.parametrize(
    ("action", "resources", "metadata", "expected"),
    [
        ("shell", ["git status", "rm -rf x"], {}, {"command": "git status\nrm -rf x"}),
        ("read", ["src/a.py"], {}, {"path": "src/a.py"}),
        (
            "edit",
            ["a.py", "b.py"],
            {"filepath": "a.py, b.py"},
            {"path": "a.py", "paths": ["a.py", "b.py"]},
        ),
        ("external_directory", ["/etc/*"], {}, {"path": "/etc/*"}),
        ("grep", ["secret"], {"path": "src"}, {"pattern": "secret", "path": "src"}),
        ("glob", ["**/*.py"], {"path": None}, {"pattern": "**/*.py"}),
        ("webfetch", ["https://x.test"], {"url": "https://x.test"}, {"url": "https://x.test"}),
        ("websearch", ["opencode v2"], {}, {"query": "opencode v2"}),
        ("skill", ["deploy"], {}, {"skill": "deploy"}),
        ("subagent", ["explore"], {}, {"agent": "explore"}),
        ("omnigent_sys_session_list", ["*"], {}, {}),
        ("opencode_read_mcp_resource", ["srv:file://x"], {}, {"resources": ["srv:file://x"]}),
    ],
)
def test_normalize_for_policy_builds_action_arguments(
    action: str, resources: list[str], metadata: dict[str, object], expected: dict[str, object]
) -> None:
    req = parse_permission_request(
        {
            "id": "per_1",
            "sessionID": "ses_1",
            "action": action,
            "resources": resources,
            "metadata": metadata,
            "source": {"type": "tool", "messageID": "msg_1", "id": "call_9"},
        }
    )
    assert req is not None
    normalized = normalize_for_policy(req, omnigent_session_id="conv_1", workspace="/repo")
    assert normalized["arguments"] == expected
    assert normalized["action"] == action
    assert normalized["resources"] == resources
    assert normalized["tool_call_id"] == "call_9"
    assert normalized["harness"] == OPENCODE_NATIVE_HARNESS
    assert normalized["working_directory"] == "/repo"
    assert normalized["omnigent_session_id"] == "conv_1"
    assert normalized["opencode_session_id"] == "ses_1"


def test_normalize_for_policy_keeps_flat_command_path_url() -> None:
    """The flat keys stay for callers that predate ``arguments``."""
    shell = parse_permission_request({"id": "p", "action": "shell", "resources": ["ls"]})
    read = parse_permission_request({"id": "p", "action": "read", "resources": ["a.py"]})
    fetch = parse_permission_request({"id": "p", "action": "webfetch", "resources": ["https://u"]})
    assert shell and read and fetch
    assert normalize_for_policy(shell, omnigent_session_id="c", workspace=None)["command"] == "ls"
    assert normalize_for_policy(read, omnigent_session_id="c", workspace=None)["path"] == "a.py"
    assert (
        normalize_for_policy(fetch, omnigent_session_id="c", workspace=None)["url"] == "https://u"
    )
