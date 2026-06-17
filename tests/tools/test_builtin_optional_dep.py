"""Graceful degradation for built-ins backed by an optional extra.

The Hindsight memory tools need the optional ``omnigent[hindsight]`` extra; their
registry factories raise ``ImportError`` when ``hindsight-client`` isn't
installed. A bundled agent (polly / debby) that declares them must still LOAD on
a base install — the tool is skipped, not fatal. These tests pin that contract.
"""

from __future__ import annotations

import logging

import omnigent.tools.builtins as builtins_mod
from omnigent.spec.types import AgentSpec, BuiltinToolConfig, ToolsConfig
from omnigent.tools import ToolManager


def _spec_with(*builtins: tuple[str, dict[str, str]]) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        tools=ToolsConfig(builtins=[BuiltinToolConfig(name=n, config=c) for n, c in builtins]),
    )


def _boom() -> None:
    raise ImportError("hindsight-client is not installed")


def test_missing_optional_dep_skips_tool_without_breaking_load(monkeypatch, caplog) -> None:
    # Simulate `hindsight-client` being absent (CI installs it via the dev extra).
    monkeypatch.setattr(builtins_mod, "_require_hindsight", _boom)

    spec = _spec_with(
        ("hindsight_recall", {"bank_id": "polly"}),
        ("upload_file", {}),  # a normal builtin declared alongside it
    )

    with caplog.at_level(logging.WARNING):
        mgr = ToolManager(spec)  # must NOT raise

    names = mgr.get_tool_names()
    # The optional-dep tool is skipped...
    assert "hindsight_recall" not in names
    # ...but a co-declared normal builtin still registers, and the agent loaded.
    assert "upload_file" in names
    assert "hindsight_recall" in caplog.text
    assert "optional dependency" in caplog.text


def test_optional_dep_present_registers_the_tool(monkeypatch) -> None:
    # With the extra installed (no patch), the tool registers normally. CI has
    # hindsight-client in the dev set, so _require_hindsight() is a no-op here.
    spec = _spec_with(("hindsight_recall", {"bank_id": "polly"}))
    mgr = ToolManager(spec)
    assert "hindsight_recall" in mgr.get_tool_names()
