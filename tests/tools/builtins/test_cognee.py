"""Tests for the cognee knowledge-graph memory built-in tools.

The ``cognee`` package (the optional ``cognee`` extra) is not in the dev
set; the tools call the framework boundary in ``omnigent.runtime.memory``,
which is patched directly — no cognee import, no network. Covers schema
shape, dataset/scope resolution from ``ToolContext``, the cross-agent
shared-dataset exchange, invoke paths, and the optional-extra registry gate.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.cognee import CogneeRememberTool, CogneeSearchTool


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tool settings hermetic — never read the user's config.yaml."""
    import omnigent.runtime.memory as memory_mod

    monkeypatch.setattr(memory_mod, "cognee_settings", dict)


def _patch_search(monkeypatch: pytest.MonkeyPatch, results: list[str]) -> MagicMock:
    import omnigent.runtime.memory as memory_mod

    mock = MagicMock(return_value=results)
    monkeypatch.setattr(memory_mod, "memory_search", mock)
    return mock


def _patch_add(monkeypatch: pytest.MonkeyPatch, *, stored: bool = True) -> MagicMock:
    import omnigent.runtime.memory as memory_mod

    mock = MagicMock(return_value=stored)
    monkeypatch.setattr(memory_mod, "memory_add", mock)
    return mock


# ---------------------------------------------------------------------------
# Names, descriptions, schemas
# ---------------------------------------------------------------------------


def test_names_and_descriptions() -> None:
    assert CogneeSearchTool.name() == "cognee_search"
    assert CogneeRememberTool.name() == "cognee_remember"
    # description() must work without instantiation (used by tool discovery).
    assert "memory" in CogneeSearchTool.description().lower()
    assert "memory" in CogneeRememberTool.description().lower()


@pytest.mark.parametrize(
    ("cls", "param"),
    [
        (CogneeSearchTool, "query"),
        (CogneeRememberTool, "content"),
    ],
)
def test_schema_shape(cls: type[Any], param: str) -> None:
    schema = cls({}).get_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == cls.name()
    assert param in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == [param]
    assert "scope" in fn["parameters"]["properties"]


# ---------------------------------------------------------------------------
# Search: scope + dataset resolution
# ---------------------------------------------------------------------------


def test_search_defaults_to_private_dataset(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["likes tea", "lives in NYC"])
    result = CogneeSearchTool({}).invoke(json.dumps({"query": "about the user"}), tool_ctx)
    assert result == "- likes tea\n- lives in NYC"
    # conftest tool_ctx has agent_id="agent_test"
    assert search.call_args.args[1] == ["agent_test"]


def test_search_includes_granted_shared_dataset_by_default(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    tool = CogneeSearchTool({"shared_dataset": "Team Knowledge"})
    tool.invoke(json.dumps({"query": "q"}), tool_ctx)
    assert search.call_args.args[1] == ["agent_test", "team_knowledge"]


def test_search_scope_agent_excludes_shared(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    tool = CogneeSearchTool({"shared_dataset": "team"})
    tool.invoke(json.dumps({"query": "q", "scope": "agent"}), tool_ctx)
    assert search.call_args.args[1] == ["agent_test"]


def test_search_scope_shared_only(monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext) -> None:
    search = _patch_search(monkeypatch, ["m"])
    tool = CogneeSearchTool({"shared_dataset": "team"})
    tool.invoke(json.dumps({"query": "q", "scope": "shared"}), tool_ctx)
    assert search.call_args.args[1] == ["team"]


def test_search_scope_shared_without_grant_explains(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    result = CogneeSearchTool({}).invoke(json.dumps({"query": "q", "scope": "shared"}), tool_ctx)
    assert "shared_dataset" in result
    search.assert_not_called()


def test_search_config_dataset_overrides_agent_id(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    CogneeSearchTool({"dataset": "custom-DS"}).invoke(json.dumps({"query": "q"}), tool_ctx)
    assert search.call_args.args[1] == ["custom_ds"]


def test_search_empty_returns_fallback(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    _patch_search(monkeypatch, [])
    result = CogneeSearchTool({}).invoke(json.dumps({"query": "anything"}), tool_ctx)
    assert result == "No relevant memories found."


def test_search_missing_query_returns_error(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    _patch_search(monkeypatch, ["m"])
    assert "query" in CogneeSearchTool({}).invoke(json.dumps({}), tool_ctx).lower()


def test_search_passes_top_k_and_search_type(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    tool = CogneeSearchTool({"top_k": "5", "search_type": "CHUNKS"})
    tool.invoke(json.dumps({"query": "q"}), tool_ctx)
    assert search.call_args.kwargs["top_k"] == 5
    assert search.call_args.kwargs["settings"]["search_type"] == "CHUNKS"


# ---------------------------------------------------------------------------
# Tier pools: user / team / org
# ---------------------------------------------------------------------------


def test_search_tier_scopes_target_their_pools(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    config = {"user_dataset": "u_v", "team_dataset": "t_p", "org_dataset": "o_a"}
    for scope, expected in (("user", "u_v"), ("team", "t_p"), ("org", "o_a")):
        CogneeSearchTool(config).invoke(json.dumps({"query": "q", "scope": scope}), tool_ctx)
        assert search.call_args.args[1] == [expected]


def test_search_all_scope_layers_tiers_narrowest_first(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    config = {"user_dataset": "u_v", "team_dataset": "t_p", "org_dataset": "o_a"}
    CogneeSearchTool(config).invoke(json.dumps({"query": "q"}), tool_ctx)
    assert search.call_args.args[1] == ["agent_test", "u_v", "t_p", "o_a"]


def test_search_tier_scope_without_grant_explains(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    result = CogneeSearchTool({}).invoke(json.dumps({"query": "q", "scope": "org"}), tool_ctx)
    assert "org_dataset" in result
    search.assert_not_called()


def test_tiers_inherit_from_global_cognee_settings(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    import omnigent.runtime.memory as memory_mod

    monkeypatch.setattr(memory_mod, "cognee_settings", lambda: {"org_dataset": "acme"})
    search = _patch_search(monkeypatch, ["m"])
    CogneeSearchTool({}).invoke(json.dumps({"query": "q", "scope": "org"}), tool_ctx)
    assert search.call_args.args[1] == ["acme"]


def test_remember_tier_scope_stores_to_pool(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    add = _patch_add(monkeypatch)
    tool = CogneeRememberTool({"team_dataset": "t_p"})
    result = tool.invoke(json.dumps({"content": "x", "scope": "team"}), tool_ctx)
    assert add.call_args.args == ("x", "t_p")
    assert "t_p" in result


def test_remember_tier_scope_without_grant_explains(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    add = _patch_add(monkeypatch)
    result = CogneeRememberTool({}).invoke(json.dumps({"content": "x", "scope": "user"}), tool_ctx)
    assert "user_dataset" in result
    add.assert_not_called()


# ---------------------------------------------------------------------------
# Search: peer-agent grants
# ---------------------------------------------------------------------------


def test_search_all_scope_includes_granted_peer_datasets(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    tool = CogneeSearchTool({"shared_dataset": "team", "read_datasets": "ag_researcher"})
    tool.invoke(json.dumps({"query": "q"}), tool_ctx)
    assert search.call_args.args[1] == ["agent_test", "team", "ag_researcher"]


def test_search_scope_peers_only(monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext) -> None:
    search = _patch_search(monkeypatch, ["m"])
    tool = CogneeSearchTool({"read_datasets": "ag_researcher, ag_writer"})
    tool.invoke(json.dumps({"query": "q", "scope": "peers"}), tool_ctx)
    assert search.call_args.args[1] == ["ag_researcher", "ag_writer"]


def test_search_scope_peers_without_grant_explains(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    result = CogneeSearchTool({}).invoke(json.dumps({"query": "q", "scope": "peers"}), tool_ctx)
    assert "read_datasets" in result
    search.assert_not_called()


def test_search_dataset_param_targets_granted_peer(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    tool = CogneeSearchTool({"read_datasets": "ag_researcher"})
    tool.invoke(json.dumps({"query": "q", "dataset": "ag_Researcher"}), tool_ctx)
    assert search.call_args.args[1] == ["ag_researcher"]


def test_search_dataset_param_denied_when_not_granted(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    search = _patch_search(monkeypatch, ["m"])
    result = CogneeSearchTool({}).invoke(
        json.dumps({"query": "q", "dataset": "ag_other"}), tool_ctx
    )
    assert "not granted" in result
    assert "agent_test" in result  # the error lists what IS granted
    search.assert_not_called()


# ---------------------------------------------------------------------------
# Remember: scope + exchange semantics
# ---------------------------------------------------------------------------


def test_remember_stores_to_private_dataset(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    add = _patch_add(monkeypatch)
    result = CogneeRememberTool({}).invoke(json.dumps({"content": "prefers dark mode"}), tool_ctx)
    assert result == "Stored to long-term memory."
    assert add.call_args.args == ("prefers dark mode", "agent_test")


def test_remember_shared_scope_publishes_to_exchange_dataset(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    add = _patch_add(monkeypatch)
    tool = CogneeRememberTool({"shared_dataset": "team"})
    result = tool.invoke(json.dumps({"content": "x", "scope": "shared"}), tool_ctx)
    assert "'team'" in result
    assert "granted access" in result
    assert add.call_args.args == ("x", "team")


def test_remember_shared_scope_without_grant_explains(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    add = _patch_add(monkeypatch)
    result = CogneeRememberTool({}).invoke(
        json.dumps({"content": "x", "scope": "shared"}), tool_ctx
    )
    assert "shared_dataset" in result
    add.assert_not_called()


def test_remember_tags_conversation_node_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add = _patch_add(monkeypatch)
    ctx = ToolContext(task_id="t", agent_id="a", conversation_id="conv_9")
    CogneeRememberTool({}).invoke(json.dumps({"content": "x"}), ctx)
    assert add.call_args.kwargs["node_set"] == ["conv_9"]


def test_remember_store_unavailable_reports_failure(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    _patch_add(monkeypatch, stored=False)
    result = CogneeRememberTool({}).invoke(json.dumps({"content": "x"}), tool_ctx)
    assert "failed" in result.lower()
    assert result != "Stored to long-term memory."


def test_remember_missing_content_returns_error(
    monkeypatch: pytest.MonkeyPatch, tool_ctx: ToolContext
) -> None:
    _patch_add(monkeypatch)
    assert "content" in CogneeRememberTool({}).invoke(json.dumps({}), tool_ctx).lower()


# ---------------------------------------------------------------------------
# Optional-extra gating
# ---------------------------------------------------------------------------


def test_cognee_tools_registered_only_when_package_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry includes the cognee tools iff the package import probe
    passes — pretend it is installed, reload, then pretend it is absent."""
    import importlib
    import importlib.util
    from importlib.machinery import ModuleSpec

    import omnigent.tools.builtins as builtins_mod

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
        if name == "cognee":
            return ModuleSpec("cognee", loader=None)
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.delenv("OMNIGENT_DISABLE_COGNEE", raising=False)
    importlib.reload(builtins_mod)
    try:
        assert "cognee_search" in builtins_mod.BUILTIN_NAMES
        assert "cognee_remember" in builtins_mod.BUILTIN_NAMES
        assert isinstance(builtins_mod.get_builtin_tool("cognee_search", {}), CogneeSearchTool)
    finally:
        monkeypatch.undo()
        importlib.reload(builtins_mod)


def test_cognee_tools_absent_when_kill_switched(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import importlib.util
    from importlib.machinery import ModuleSpec

    import omnigent.tools.builtins as builtins_mod

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
        if name == "cognee":
            return ModuleSpec("cognee", loader=None)
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setenv("OMNIGENT_DISABLE_COGNEE", "1")
    importlib.reload(builtins_mod)
    try:
        assert "cognee_search" not in builtins_mod.BUILTIN_NAMES
        assert "cognee_remember" not in builtins_mod.BUILTIN_NAMES
        assert builtins_mod.get_builtin_tool("cognee_search", {}) is None
    finally:
        monkeypatch.undo()
        importlib.reload(builtins_mod)
