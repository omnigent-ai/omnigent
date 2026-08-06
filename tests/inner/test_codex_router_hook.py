from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.hook_scripts import codex_router_hook as hook
from omnigent.inner.hook_scripts import subagent_router
from tests.inner.conftest import advertise_relay_tools, advertise_router

# Delivered plaintext in hook payloads (measured live); the name survives
# from when it was assumed encrypted and now documents that either way the
# bytes are forwarded verbatim.
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
        prompt_keys=hook.ROUTE_SEAMS["prompt_keys"],
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


def test_build_route_request_sends_the_message_as_the_routing_prompt() -> None:
    # The spawn message arrives plaintext, so it is the routing signal —
    # without it every unnamed spawn scores the placeholder and lands the
    # router's default arm, never a delegate arm like glm.
    body = _build(_payload()["tool_input"], parent_model="gpt-5-6-sol")

    assert body == {
        "harness": "codex-native",
        "task_name": "refactor-tests",
        "prompt": _ENCRYPTED_MESSAGE,
        "parent_model": "gpt-5-6-sol",
        "requested_model": None,
    }


def test_build_route_request_forwards_an_explicit_model_ask() -> None:
    body = _build({"message": "fix the typo", "model": "system.ai.glm-5-2"})

    assert body["requested_model"] == "system.ai.glm-5-2"
    assert body["prompt"] == "fix the typo"


@pytest.mark.parametrize(
    ("tool_input", "expected_task_name"),
    [
        # Codex names the spawn ``agent_name`` on some paths, ``task_name`` on
        # others; the explicit ``task_name`` wins when both are present.
        ({"agent_name": "doc-writer", "message": _ENCRYPTED_MESSAGE}, "doc-writer"),
        ({"task_name": "refactor-tests", "agent_name": "doc-writer"}, "refactor-tests"),
        # The server supplies the placeholder task; the hook does not invent one.
        ({"message": _ENCRYPTED_MESSAGE}, ""),
    ],
)
def test_build_route_request_derives_task_name(
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    expected_task_name: str,
) -> None:
    body = _build(tool_input)

    assert body["task_name"] == expected_task_name
    # The message, when present, rides along as the routing prompt.
    assert body["prompt"] == tool_input.get("message")


def test_rewrite_injects_the_spawn_slug_and_passes_message_verbatim(
    tmp_path: Path,
    router: _Router,
) -> None:
    # ``spawn_agent`` validates ``model`` against codex's own catalog before
    # the request leaves the CLI, so the routed catalog id has to be spelled
    # in codex's slugs — injecting it verbatim fails the spawn.
    advertise_router(tmp_path)
    router.response = {
        "action": "rewrite",
        "model": "databricks-gpt-5-6-luna",
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
                "model": "gpt-5.6-luna",
            },
            "permissionDecisionReason": "cheapest arm (applied as 'gpt-5.6-luna')",
        },
        "systemMessage": "Using Smart Routing. Routing to gpt-5.6-luna.",
    }
    assert router.calls[0]["session_id"] == "conv_abc"
    assert router.calls[0]["body"]["prompt"] == _ENCRYPTED_MESSAGE


def test_glm_rewrite_spawns_under_the_gateways_own_spelling(
    tmp_path: Path,
    router: _Router,
) -> None:
    # GLM has no codex slug of its own; omnigent adds it to the session's
    # catalog under the exact id the gateway serves, so that id IS the slug.
    advertise_router(tmp_path)
    router.response = {"action": "rewrite", "model": "system.ai.glm-5-2", "rationale": "delegate"}

    out = _route(_payload(), router_dir=tmp_path)

    assert out is not None
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "system.ai.glm-5-2"


def test_glm_rewrite_clamps_an_effort_glm_refuses(
    tmp_path: Path,
    router: _Router,
) -> None:
    # Codex refuses the pairing client-side ("Reasoning effort `xhigh` is not
    # supported for model `system.ai.glm-5-2`"), so an inherited session
    # default would fail the spawn the router just approved.
    advertise_router(tmp_path)
    router.response = {"action": "rewrite", "model": "system.ai.glm-5-2", "rationale": "delegate"}

    out = _route(_payload(reasoning_effort="xhigh"), router_dir=tmp_path)

    assert out is not None
    assert out["hookSpecificOutput"]["updatedInput"]["reasoning_effort"] == "medium"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_an_effort_glm_accepts_is_left_alone(
    tmp_path: Path,
    router: _Router,
    effort: str,
) -> None:
    advertise_router(tmp_path)
    router.response = {"action": "rewrite", "model": "system.ai.glm-5-2", "rationale": "delegate"}

    out = _route(_payload(reasoning_effort=effort), router_dir=tmp_path)

    assert out is not None
    assert out["hookSpecificOutput"]["updatedInput"]["reasoning_effort"] == effort


def test_an_unset_effort_stays_unset(
    tmp_path: Path,
    router: _Router,
) -> None:
    # Codex then applies the model's own catalog default, which is inside its
    # ladder; inventing a value would override a user default needlessly.
    advertise_router(tmp_path)
    router.response = {"action": "rewrite", "model": "system.ai.glm-5-2", "rationale": "delegate"}

    out = _route(_payload(), router_dir=tmp_path)

    assert out is not None
    assert "reasoning_effort" not in out["hookSpecificOutput"]["updatedInput"]


def test_a_pick_codex_cannot_spawn_allows_the_spawn_unchanged(
    tmp_path: Path,
    router: _Router,
) -> None:
    # A Claude id has no spawn slug at all. Falling open leaves the spawn on
    # the parent's model — a degraded spawn beats one the CLI kills.
    advertise_router(tmp_path)
    router.response = {
        "action": "rewrite",
        "model": "databricks-claude-sonnet-5",
        "rationale": "r",
    }

    assert _route(_payload(), router_dir=tmp_path) is None


@pytest.mark.parametrize(
    ("response", "expected_notice"),
    [
        # A rewrite is otherwise invisible — codex reports no model change — so
        # the routed model is announced in the TUI.
        (
            {"action": "rewrite", "model": "databricks-gpt-5-6-luna", "rationale": "cheap"},
            # The slug the spawn actually runs on, not the catalog id.
            "Using Smart Routing. Routing to gpt-5.6-luna.",
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


@pytest.mark.parametrize(
    ("asked", "expected_notice"),
    [
        # The parent asked for one arm and the router picked another. Codex
        # reports no model change of its own, so without naming the ask the
        # parent reads its own choice back and never learns it was substituted.
        ("gpt-5.6-sol", "Using Smart Routing. Requested gpt-5.6-sol; routing to gpt-5.6-luna."),
        # The router agreed with the ask — nothing was substituted.
        ("gpt-5.6-luna", "Using Smart Routing. Routing to gpt-5.6-luna."),
        # No ask at all.
        (None, "Using Smart Routing. Routing to gpt-5.6-luna."),
    ],
)
def test_the_notice_names_a_model_the_router_substituted(
    tmp_path: Path,
    router: _Router,
    asked: str | None,
    expected_notice: str,
) -> None:
    advertise_router(tmp_path)
    router.response = {"action": "rewrite", "model": "databricks-gpt-5-6-luna", "rationale": "r"}
    tool_input = {} if asked is None else {"model": asked}

    out = _route(_payload(**tool_input), router_dir=tmp_path)

    assert out is not None
    assert out["systemMessage"] == expected_notice


def test_finalize_spawn_input_passes_no_opinion_through() -> None:
    assert hook.finalize_spawn_input(None) is None


def _redirect_reason(tmp_path: Path, router: _Router) -> str:
    """Run a cross-harness redirect through the hook and return its deny reason."""
    advertise_router(tmp_path)
    router.response = {
        "action": "redirect",
        "harness": "claude-native",
        "model": "claude-opus-4-8",
    }

    out = _route(_payload(), router_dir=tmp_path)

    assert out is not None
    hook_output = out["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "deny"
    reason = hook_output["permissionDecisionReason"]
    assert isinstance(reason, str)
    return reason


def test_redirect_denies_with_bare_session_create_instruction(
    tmp_path: Path,
    router: _Router,
) -> None:
    advertise_relay_tools(tmp_path, "sys_session_create", "sys_agent_list")

    reason = _redirect_reason(tmp_path, router)

    # Codex addresses an MCP tool by its BARE name plus a separate namespace
    # field; the flattened ``omnigentsys_session_create`` spelling only shows up
    # in codex's own logs and hook payloads and is not callable. The codex
    # phrasing therefore quotes bare names and names the server as the
    # namespace, never a prefixed spelling.
    assert "sys_session_create" in reason
    assert "sys_agent_list" in reason
    assert "mcp__omnigent__" not in reason
    assert "omnigentsys_session_create" not in reason
    assert "omnigent" in reason
    assert "sys_session_send" not in reason
    assert "claude-opus-4-8" in reason
    assert "claude-native" in reason
    # Codex defers MCP schemas behind its own ``tool_search``, so the reason
    # must send the model looking rather than let it conclude "no such tool".
    assert "search" in reason.lower()


def test_redirect_without_the_spawn_tool_names_no_sys_session_tool(
    tmp_path: Path,
    router: _Router,
) -> None:
    """No advertised spawn tool → name none and hand the work back."""
    advertise_relay_tools(tmp_path, "sys_read_inbox")

    reason = _redirect_reason(tmp_path, router)

    assert "sys_session_" not in reason
    assert "sys_agent_list" not in reason
    assert "yourself" in reason
    assert "claude-opus-4-8" in reason


def test_redirect_without_a_relay_file_keeps_the_actionable_instruction(
    tmp_path: Path,
    router: _Router,
) -> None:
    """A missing relay file reads as "unknown", preserving today's instruction."""
    assert not (tmp_path / subagent_router._TOOL_RELAY_FILE).exists()

    reason = _redirect_reason(tmp_path, router)

    assert "sys_session_create" in reason
    assert "yourself" not in reason


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


def test_unknown_subcommand_is_a_no_op(capsys: pytest.CaptureFixture[str]) -> None:
    assert hook.main(["nope"]) == 0
    assert "unknown subcommand" in capsys.readouterr().err


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
