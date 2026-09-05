"""Tests for the subagent-router entry in generated Claude hook settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.claude_native_bridge import build_hook_settings, prepare_bridge_dir
from omnigent.inner.hook_scripts.subagent_router import AGENT_TOOL_MATCHER

#: Claude Code's default command-hook timeout for ``UserPromptSubmit``, which is
#: shorter than the default for other events. Not importable — it is the CLI's,
#: not ours — so it is pinned here as the number the registration must undercut.
_CLAUDE_USER_PROMPT_SUBMIT_DEFAULT_TIMEOUT_S = 30


@pytest.fixture(autouse=True)
def _trust_tmp_bridge_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Treat each test's temp dir as the Claude bridge root.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)


def _bridge_dir(tmp_path: Path) -> Path:
    return prepare_bridge_dir("conv_abc", bridge_id="bridge_test", workspace=tmp_path)


def _router_entries(settings: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in settings["hooks"].get("PreToolUse", [])
        if entry.get("matcher") == AGENT_TOOL_MATCHER
    ]


def test_router_hook_absent_when_routing_off(tmp_path: Path) -> None:
    settings = build_hook_settings(_bridge_dir(tmp_path))
    assert _router_entries(settings) == []
    assert "claude_router_hook" not in str(settings)


def test_router_hook_registered_when_router_dir_set(tmp_path: Path) -> None:
    bridge_dir = _bridge_dir(tmp_path)
    settings = build_hook_settings(bridge_dir, subagent_router_dir=bridge_dir)
    entries = _router_entries(settings)
    assert len(entries) == 1
    hook = entries[0]["hooks"][0]
    assert hook["type"] == "command"
    assert "omnigent.inner.hook_scripts.claude_router_hook" in hook["command"]
    assert f"--bridge-dir {bridge_dir}" in hook["command"]
    assert f"--router-dir {bridge_dir}" in hook["command"]
    # Derived from the hook script's own request budget, so it always exceeds
    # it and the script's fail-open branch runs before Claude kills the hook.
    from omnigent.inner.hook_scripts.subagent_router import HOOK_TIMEOUT_S

    assert hook["timeout"] == int(HOOK_TIMEOUT_S)


def test_router_hook_coexists_with_policy_hooks(tmp_path: Path) -> None:
    bridge_dir = _bridge_dir(tmp_path)
    settings = build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        subagent_router_dir=bridge_dir,
        turn_routing=True,
    )
    pre_tool_use = settings["hooks"]["PreToolUse"]
    matchers = [entry.get("matcher") for entry in pre_tool_use]
    assert matchers == ["AskUserQuestion", None, AGENT_TOOL_MATCHER]


def _commands(settings: dict[str, Any], event: str) -> list[str]:
    return [
        hook.get("command", "")
        for entry in settings["hooks"].get(event, [])
        for hook in entry.get("hooks", [])
    ]


def test_both_routing_hooks_coexist_with_the_policy_hooks(tmp_path: Path) -> None:
    """
    Add #24: one settings dict carries both routing hooks and both policy hooks.

    The two routing hooks live on different events — ``route-turn`` on
    ``UserPromptSubmit``, ``route-subagent`` on ``PreToolUse`` — and each event
    already carries omnigent's own hooks. A hook registered by assignment rather
    than append silently drops whatever was there, which is invisible until a
    live session stops routing (or stops enforcing policy).
    """
    bridge_dir = _bridge_dir(tmp_path)
    settings = build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        subagent_router_dir=bridge_dir,
        turn_routing=True,
    )

    prompt_submit = _commands(settings, "UserPromptSubmit")
    # Order is load-bearing: the transcript forwarder's status hook first, the
    # routing gate next (it may block the prompt), the request-phase policy gate
    # last — for a native session that gate is the sole request gate.
    assert len(prompt_submit) == 3
    assert "omnigent.claude_native_hook" in prompt_submit[0]
    assert "route-turn" in prompt_submit[1]
    assert "evaluate-policy" in prompt_submit[2]
    # Neither routing hook leaks onto the other's event.
    assert not any("route-subagent" in c or "claude_router_hook" in c for c in prompt_submit)

    pre_tool_use = _commands(settings, "PreToolUse")
    assert any("claude_router_hook" in c for c in pre_tool_use)
    assert any("evaluate-policy" in c for c in pre_tool_use)
    assert not any("route-turn" in c for c in pre_tool_use)


def test_the_route_turn_hook_is_registered_above_its_own_request_budget(
    tmp_path: Path,
) -> None:
    """
    Add #23: claude's ``UserPromptSubmit`` default is explicitly overridden.

    The entry must carry our own number rather than inherit Claude Code's,
    for two reasons that now pull in the same direction. It has to sit ABOVE
    the hook script's own HTTP budget, or Claude kills the hook before its
    fail-open branch runs — and a killed ``UserPromptSubmit`` hook is exactly
    the case that can eat a typed prompt. And it has to sit BELOW the CLI's
    own default, because the default is what a wedged hook would cost the
    user: routing is not allowed to hold a prompt for half a minute.
    """
    from omnigent.runner.turn_routing import HARNESS_HOOK_TIMEOUT_S, HOOK_REQUEST_TIMEOUT_S

    settings = build_hook_settings(_bridge_dir(tmp_path), turn_routing=True)
    entries = [
        hook
        for entry in settings["hooks"]["UserPromptSubmit"]
        for hook in entry.get("hooks", [])
        if "route-turn" in hook.get("command", "")
    ]
    assert len(entries) == 1
    registered = entries[0]["timeout"]
    assert registered == HARNESS_HOOK_TIMEOUT_S
    assert registered > HOOK_REQUEST_TIMEOUT_S
    assert registered < _CLAUDE_USER_PROMPT_SUBMIT_DEFAULT_TIMEOUT_S


def test_the_subagent_router_hook_is_registered_above_its_own_request_budget(
    tmp_path: Path,
) -> None:
    """Add #23, the other claude hook: same relation, its own two constants."""
    from omnigent.inner.hook_scripts.subagent_router import REQUEST_TIMEOUT_S

    bridge_dir = _bridge_dir(tmp_path)
    settings = build_hook_settings(bridge_dir, subagent_router_dir=bridge_dir)
    hook = _router_entries(settings)[0]["hooks"][0]
    assert hook["timeout"] > REQUEST_TIMEOUT_S
