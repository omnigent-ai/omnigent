from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.hook_scripts import codex_router_hook as hook
from omnigent.inner.hook_scripts import subagent_router
from tests.inner.conftest import advertise_router

_ENCRYPTED_MESSAGE = "enc:AAAABBBBCCCC=="


def _payload(**tool_input: Any) -> dict[str, Any]:  # type: ignore[explicit-any]
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "collaborationspawn_agent",
        "model": "gpt-5-6-sol",
        "tool_input": {
            "task_name": "refactor-tests",
            "message": _ENCRYPTED_MESSAGE,
            **tool_input,
        },
    }


def _route(
    payload: dict[str, Any],  # type: ignore[explicit-any]
    *,
    router_dir: Path,
    session_id: str | None = None,
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    return subagent_router.route_pre_tool_use(
        payload,
        harness=hook.DEFAULT_HARNESS,
        router_dir=router_dir,
        session_id=session_id,
        **hook.ROUTE_SEAMS,
    )


def _build(
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    *,
    parent_model: str | None = None,
) -> dict[str, Any]:  # type: ignore[explicit-any]
    return subagent_router.build_route_request(
        tool_input,
        harness="codex-native",
        parent_model=parent_model,
        task_keys=hook.ROUTE_SEAMS["task_keys"],
        include_prompt=hook.ROUTE_SEAMS["include_prompt"],
    )


class _Router:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []  # type: ignore[explicit-any]
        self.response: dict[str, Any] | None = None  # type: ignore[explicit-any]

    def __call__(
        self,
        endpoint: Any,  # type: ignore[explicit-any]
        session_id: str,
        body: dict[str, Any],  # type: ignore[explicit-any]
        **kwargs: Any,  # type: ignore[explicit-any]
    ) -> dict[str, Any] | None:  # type: ignore[explicit-any]
        self.calls.append({"endpoint": endpoint, "session_id": session_id, "body": body})
        return self.response


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> _Router:
    fake = _Router()
    monkeypatch.setattr(subagent_router, "request_decision", fake)
    return fake


def test_is_spawn_agent_tool_matches_flattened_name() -> None:
    assert hook.is_spawn_agent_tool("collaborationspawn_agent")
    assert hook.is_spawn_agent_tool("spawn_agent")
    assert not hook.is_spawn_agent_tool("Bash")
    assert not hook.is_spawn_agent_tool(None)


def test_build_route_request_never_sends_the_encrypted_prompt() -> None:
    body = _build(_payload()["tool_input"], parent_model="gpt-5-6-sol")

    assert body == {
        "harness": "codex-native",
        "task_name": "refactor-tests",
        "prompt": None,
        "fork": False,
        "parent_model": "gpt-5-6-sol",
    }
    assert _ENCRYPTED_MESSAGE not in json.dumps(body)


@pytest.mark.parametrize(
    ("tool_input", "expected_task_name", "expected_fork"),
    [
        # Codex names the spawn ``agent_name`` on some paths, ``task_name`` on
        # others; the explicit ``task_name`` wins when both are present.
        ({"agent_name": "doc-writer", "message": _ENCRYPTED_MESSAGE}, "doc-writer", False),
        ({"task_name": "refactor-tests", "agent_name": "doc-writer"}, "refactor-tests", False),
        # The server supplies the placeholder task; the hook does not invent one.
        ({"message": _ENCRYPTED_MESSAGE}, "", False),
        # Fork is detected from the task name, never from a field codex does
        # not send — a stray boolean must not be trusted.
        ({"task_name": "planner-fork"}, "planner-fork", True),
        ({"fork": True, "task_name": "refactor-tests"}, "refactor-tests", False),
    ],
)
def test_build_route_request_derives_task_name_and_fork(
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    expected_task_name: str,
    expected_fork: bool,
) -> None:
    body = _build(tool_input)

    assert body["task_name"] == expected_task_name
    assert body["fork"] is expected_fork
    # The encrypted message is never forwarded on any of these paths.
    assert body["prompt"] is None


def test_rewrite_injects_model_and_passes_message_verbatim(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path)
    router.response = {
        "action": "rewrite",
        "model": "claude-sonnet-5",
        "rationale": "cheapest arm",
        "decision_id": "d1",
    }

    out = _route(_payload(), router_dir=tmp_path)

    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "task_name": "refactor-tests",
                "message": _ENCRYPTED_MESSAGE,
                "model": "claude-sonnet-5",
            },
            "permissionDecisionReason": "cheapest arm",
        },
        "systemMessage": "Using Smart Routing. Routing to claude-sonnet-5.",
    }
    assert router.calls[0]["session_id"] == "conv_abc"
    assert router.calls[0]["body"]["prompt"] is None


@pytest.mark.parametrize(
    ("response", "expected_notice"),
    [
        # A rewrite is otherwise invisible — codex reports no model change — so
        # the routed model is announced in the TUI.
        (
            {"action": "rewrite", "model": "gpt-5-6-luna", "rationale": "cheap"},
            "Using Smart Routing. Routing to gpt-5-6-luna.",
        ),
        # A deny routed to nothing, so there is no model to announce.
        ({"action": "deny", "rationale": "over budget"}, None),
    ],
)
def test_routing_notice_announces_only_a_routed_model(
    tmp_path: Path,
    router: _Router,
    response: dict[str, Any],  # type: ignore[explicit-any]
    expected_notice: str | None,
) -> None:
    advertise_router(tmp_path)
    router.response = response

    out = _route(_payload(), router_dir=tmp_path)

    assert out is not None
    if expected_notice is None:
        assert "systemMessage" not in out
    else:
        # Top level, alongside hookSpecificOutput — codex reads it there.
        assert out["systemMessage"] == expected_notice
        assert "systemMessage" not in out["hookSpecificOutput"]


def test_with_system_message_passes_no_opinion_through() -> None:
    assert hook.with_system_message(None) is None


def test_redirect_denies_with_sys_session_send_instruction(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path)
    router.response = {
        "action": "redirect",
        "harness": "claude-native",
        "model": "claude-opus-4-8",
    }

    out = _route(_payload(), router_dir=tmp_path)

    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Router selected claude-native/claude-opus-4-8. Use sys_session_send "
                "with args.harness=claude-native, args.model=claude-opus-4-8 instead."
            ),
        }
    }


def test_deny_uses_router_rationale(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path)
    router.response = {"action": "deny", "rationale": "router unavailable"}

    out = _route(_payload(), router_dir=tmp_path)

    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "router unavailable"


def test_allow_emits_no_opinion(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path)
    router.response = {"action": "allow", "rationale": "fork exempt"}

    assert _route(_payload(), router_dir=tmp_path) is None


def test_router_unreachable_allows_unchanged(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path)
    router.response = None

    assert _route(_payload(), router_dir=tmp_path) is None


def test_missing_advertisement_allows_unchanged(
    tmp_path: Path,
    router: _Router,
) -> None:
    assert _route(_payload(), router_dir=tmp_path) is None
    assert router.calls == []


def test_malformed_advertisement_allows_unchanged(
    tmp_path: Path,
    router: _Router,
) -> None:
    (tmp_path / subagent_router.ADVERTISEMENT_FILE).write_text("{not json")

    assert _route(_payload(), router_dir=tmp_path) is None
    assert router.calls == []


def test_unknown_session_allows_unchanged(
    tmp_path: Path,
    router: _Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(subagent_router.SESSION_ID_ENV_VAR, raising=False)
    monkeypatch.delenv(subagent_router.NATIVE_SESSION_ID_ENV_VAR, raising=False)
    advertise_router(tmp_path, session_id=None)

    assert _route(_payload(), router_dir=tmp_path) is None
    assert router.calls == []


def test_baked_session_id_used_when_advertisement_has_none(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path, session_id=None)
    router.response = {"action": "allow"}

    _route(_payload(), router_dir=tmp_path, session_id="conv_baked")

    assert router.calls[0]["session_id"] == "conv_baked"


def test_non_spawn_tool_is_ignored(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path)
    payload = _payload()
    payload["tool_name"] = "shell"

    assert _route(payload, router_dir=tmp_path) is None
    assert router.calls == []


def test_fork_spawn_reported(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path)
    router.response = {"action": "allow"}

    _route(_payload(task_name="planner-fork"), router_dir=tmp_path)

    assert router.calls[0]["body"]["fork"] is True


def test_parent_model_falls_back_to_payload_model(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_router(tmp_path)
    router.response = {"action": "allow"}

    _route(_payload(), router_dir=tmp_path)

    assert router.calls[0]["body"]["parent_model"] == "gpt-5-6-sol"


def test_route_subagent_without_a_bridge_dir_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(subagent_router.ROUTER_DIR_ENV_VAR, raising=False)
    monkeypatch.delenv(subagent_router.BRIDGE_DIR_ENV_VAR, raising=False)
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(json.dumps(_payload())))
    out = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", out)

    assert hook.main(["route-subagent"]) == 0
    assert out.getvalue() == ""


def test_route_subagent_tolerates_unknown_flags(
    tmp_path: Path,
    router: _Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advertise_router(tmp_path)
    router.response = {"action": "allow"}
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(json.dumps(_payload())))
    out = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", out)

    assert hook.main(["route-subagent", "--unknown-flag", "x", "--bridge-dir", str(tmp_path)]) == 0
    assert router.calls[0]["session_id"] == "conv_abc"


def test_session_canary_subcommand_writes_file(tmp_path: Path) -> None:
    assert hook.main(["session-canary", "--bridge-dir", str(tmp_path), "--session-id", "c1"]) == 0

    record = json.loads(hook.canary_path(tmp_path).read_text())
    assert record["session_id"] == "c1"


def test_session_canary_without_a_bridge_dir_is_a_no_op(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert hook.main(["session-canary"]) == 0
    assert "needs --bridge-dir" in capsys.readouterr().err


def test_record_subagent_subcommand_appends_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for agent_id, model in (("a1", "claude-sonnet-5"), ("a2", "gpt-5-6-sol")):
        monkeypatch.setattr(
            hook.sys,
            "stdin",
            _Stdin(json.dumps({"agent_id": agent_id, "model": model, "task_name": "t"})),
        )
        assert hook.main(["record-subagent", "--bridge-dir", str(tmp_path)]) == 0

    lines = hook.audit_path(tmp_path).read_text().splitlines()
    assert [json.loads(line)["agent_id"] for line in lines] == ["a1", "a2"]
    assert json.loads(lines[0])["model"] == "claude-sonnet-5"


def test_record_subagent_without_a_bridge_dir_is_a_no_op(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert hook.main(["record-subagent"]) == 0
    assert "needs --bridge-dir" in capsys.readouterr().err


def test_unknown_subcommand_is_a_no_op(capsys: pytest.CaptureFixture[str]) -> None:
    assert hook.main(["nope"]) == 0
    assert "unknown subcommand" in capsys.readouterr().err


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
