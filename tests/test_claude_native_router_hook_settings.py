"""Tests for the subagent-router entry in generated Claude hook settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.claude_native_bridge import build_hook_settings, prepare_bridge_dir
from omnigent.inner.hook_scripts.subagent_router import AGENT_TOOL_MATCHER


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
    assert hook["timeout"] == 30


def test_router_hook_coexists_with_policy_hooks(tmp_path: Path) -> None:
    bridge_dir = _bridge_dir(tmp_path)
    settings = build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        subagent_router_dir=bridge_dir,
    )
    pre_tool_use = settings["hooks"]["PreToolUse"]
    matchers = [entry.get("matcher") for entry in pre_tool_use]
    assert matchers == ["AskUserQuestion", None, AGENT_TOOL_MATCHER]
