"""Tests for the Hermes pre_tool_call policy hook's relay-tool skip.

Omnigent relay tools (surfaced into Hermes as ``mcp_omnigent_*``) are gated when
the relay dispatches them back through the server's tool path. The hook must NOT
gate them a second time (that parks a duplicate approval card whose long-poll
hangs, wedging the turn). Hermes' own tools are still gated here.
"""

from __future__ import annotations

import io
import json

import pytest

from omnigent.inner import hermes_policy_hook


@pytest.fixture
def wired_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_OMNIGENT_SERVER_URL", "http://localhost:6767")
    monkeypatch.setenv("_OMNIGENT_SESSION_ID", "conv_test")


_ALLOW_RESULT = {"result": "POLICY_ACTION_ALLOW"}


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    result: object = _ALLOW_RESULT,
) -> tuple[dict, bool]:
    """Run the hook; return its output and whether policy evaluation ran."""
    called = {"hit": False}

    def _spy(*_args, **_kwargs):
        called["hit"] = True

        class _R:
            def json(self) -> object:
                return result

        return _R(), None

    monkeypatch.setattr("omnigent.native_policy_hook.post_evaluate_with_retry", _spy, raising=True)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps(payload)),
    )
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    hermes_policy_hook.main()
    return json.loads(out.getvalue() or "{}"), called["hit"]


def _run(monkeypatch: pytest.MonkeyPatch, tool_name: str) -> tuple[dict, bool]:
    return _invoke(monkeypatch, {"tool_name": tool_name, "tool_input": {}})


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("missing_context", "Omnigent policy context is unavailable"),
        ("malformed_input", "Malformed Hermes tool hook input"),
        ("non_object_input", "Malformed Hermes tool hook input"),
        ("evaluation_error", "Omnigent policy evaluation failed"),
    ],
)
def test_authoritative_hook_failures_block(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    reason: str,
) -> None:
    monkeypatch.setenv("_OMNIGENT_SERVER_URL", "http://localhost:6767")
    monkeypatch.setenv("_OMNIGENT_SESSION_ID", "conv_test")
    payload = json.dumps({"tool_name": "terminal", "tool_input": {}})

    if failure == "missing_context":
        monkeypatch.delenv("_OMNIGENT_SESSION_ID")
    elif failure == "malformed_input":
        payload = "not json"
    elif failure == "non_object_input":
        payload = "[]"
    else:

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("private failure detail")

        monkeypatch.setattr(
            "omnigent.native_policy_hook.post_evaluate_with_retry",
            _raise,
        )

    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    hermes_policy_hook.main()

    assert json.loads(out.getvalue()) == {"decision": "block", "reason": reason}


def test_non_object_policy_response_blocks(
    monkeypatch: pytest.MonkeyPatch,
    wired_env: None,
) -> None:
    output, _called = _invoke(
        monkeypatch,
        {"tool_name": "terminal", "tool_input": {}},
        [],
    )
    assert output == {
        "decision": "block",
        "reason": "Malformed policy response",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": ["terminal"], "tool_input": {}},
        {"tool_name": "", "tool_input": {}},
        {"tool_name": "terminal"},
        {"tool_name": "terminal", "tool_input": None},
        {"tool_name": "terminal", "tool_input": []},
    ],
)
def test_malformed_tool_fields_block(
    monkeypatch: pytest.MonkeyPatch,
    wired_env: None,
    payload: dict[str, object],
) -> None:
    output, _called = _invoke(monkeypatch, payload)
    assert output == {
        "decision": "block",
        "reason": "Malformed Hermes tool hook input",
    }


@pytest.mark.parametrize("result", [{}, {"result": "garbage"}, {"result": []}])
def test_malformed_policy_action_blocks(
    monkeypatch: pytest.MonkeyPatch,
    wired_env: None,
    result: dict[str, object],
) -> None:
    output, _called = _invoke(
        monkeypatch,
        {"tool_name": "terminal", "tool_input": {}},
        result,
    )
    assert output == {
        "decision": "block",
        "reason": "Malformed policy response",
    }


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp_omnigent_sys_session_get_info",  # hermes single-underscore form
        "mcp_omnigent_sys_os_write",
        "mcp__omnigent__list_comments",  # native double-underscore form
    ],
)
def test_relay_tools_are_skipped(
    monkeypatch: pytest.MonkeyPatch, wired_env: None, tool_name: str
) -> None:
    result, server_called = _run(monkeypatch, tool_name)
    # Allow (empty object) WITHOUT hitting /policies/evaluate — the dispatch
    # gate is the single authoritative gate for these tools.
    assert result == {}
    assert server_called is False


@pytest.mark.parametrize(
    "tool_name", ["terminal", "str_replace_editor", "mcp_github_create_issue"]
)
def test_native_and_other_mcp_tools_are_still_gated(
    monkeypatch: pytest.MonkeyPatch, wired_env: None, tool_name: str
) -> None:
    _result, server_called = _run(monkeypatch, tool_name)
    # Hermes' own tools (and non-Omnigent MCP servers) do NOT round-trip the
    # relay, so the hook stays their policy gate.
    assert server_called is True
