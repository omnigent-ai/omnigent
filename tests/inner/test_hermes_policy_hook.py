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


def _run(monkeypatch: pytest.MonkeyPatch, tool_name: str) -> tuple[dict, bool]:
    """Run the hook with *tool_name*; return (stdout_json, server_was_called)."""
    called = {"hit": False}

    def _spy(*_args, **_kwargs):
        called["hit"] = True

        class _R:
            def json(self) -> dict:
                return {"result": "POLICY_ACTION_ALLOW"}

        return _R()

    monkeypatch.setattr("omnigent.native_policy_hook.post_evaluate_with_retry", _spy, raising=True)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"tool_name": tool_name, "tool_input": {}})),
    )
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    hermes_policy_hook.main()
    return json.loads(out.getvalue() or "{}"), called["hit"]


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
