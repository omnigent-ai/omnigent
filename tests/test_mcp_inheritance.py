"""User config resolution for strict MCP inheritance."""

import pytest
import yaml

from omnigent import mcp_inheritance
from omnigent.mcp_inheritance import McpInheritance, resolve_mcp_inheritance


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "user"))
    monkeypatch.setattr(mcp_inheritance, "_warned", set())
    (tmp_path / "user").mkdir()
    return tmp_path / "user"


def test_missing_config_inherits_everything():
    inheritance = resolve_mcp_inheritance("claude-native")
    assert inheritance == McpInheritance()
    assert inheritance.allows("anything")


def test_global_setting_applies_to_every_launch_executor():
    cfg = {"mcp": {"inherit": False, "allow": ["docs", "omnigent"]}}
    for executor in ("claude-native", "claude-sdk", "codex-native", "codex"):
        inheritance = resolve_mcp_inheritance(executor, cfg)
        assert inheritance == McpInheritance(False, frozenset({"docs", "omnigent"}))
        assert inheritance.allowed_names() == ("omnigent", "docs")
        assert inheritance.allows("omnigent") and inheritance.allows("docs")
        assert not inheritance.allows("mail")


def test_harness_entry_overrides_global_fields_and_canonicalizes_aliases():
    cfg = {
        "mcp": {"inherit": False, "allow": ["docs"]},
        "harness": {
            "default": "codex",
            "claude-native": {"args": ["--verbose"], "mcp": {"inherit": True}},
            # ``claude`` is the alias for claude-sdk, as for harness args.
            "claude": {"mcp": {"allow": ["search"]}},
            "codex-native": {"mcp": {"allow": []}},
        },
    }
    assert resolve_mcp_inheritance("claude-native", cfg).inherit
    assert resolve_mcp_inheritance("claude-sdk", cfg) == McpInheritance(
        False, frozenset({"search"})
    )
    assert resolve_mcp_inheritance("codex-native", cfg) == McpInheritance(False, frozenset())
    assert resolve_mcp_inheritance("codex", cfg) == McpInheritance(False, frozenset({"docs"}))
    strict_only_here = {"harness": {"codex": {"mcp": {"inherit": False}}}}
    assert not resolve_mcp_inheritance("codex", strict_only_here).inherit
    assert resolve_mcp_inheritance("codex-native", strict_only_here).inherit


@pytest.mark.parametrize(
    "cfg,inherit,warning",
    [
        ({"mcp": "strict"}, True, "config `mcp` must be a mapping"),
        ({"mcp": {"inherit": "false"}}, True, "config `mcp.inherit` must be true or false"),
        ({"mcp": {"inherit": False, "allow": "docs"}}, False, "config `mcp.allow` must be a list"),
        (
            {"mcp": {"inherit": False, "allow": ["docs", 1]}},
            False,
            "config `mcp.allow` must be a list",
        ),
        (
            {"harness": {"codex": {"mcp": ["x"]}}},
            True,
            "config `harness.codex.mcp` must be a mapping",
        ),
    ],
)
def test_malformed_fields_warn_once_and_are_ignored(cfg, inherit, warning, caplog):
    for _ in range(2):
        inheritance = resolve_mcp_inheritance("codex", cfg)
    assert inheritance == McpInheritance(inherit, frozenset())
    assert caplog.text.count(warning) == 1


def test_reads_user_config_and_ignores_project_config(config_home, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".omnigent").mkdir(parents=True)
    (project / ".omnigent/config.yaml").write_text(
        yaml.safe_dump(
            {"mcp": {"inherit": False}, "harness": {"codex": {"mcp": {"inherit": False}}}}
        )
    )
    monkeypatch.chdir(project)
    assert resolve_mcp_inheritance("codex").inherit
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"mcp": {"inherit": False, "allow": ["docs"]}})
    )
    assert resolve_mcp_inheritance("codex") == McpInheritance(False, frozenset({"docs"}))


def test_unreadable_user_config_warns_and_inherits(config_home, caplog):
    (config_home / "config.yaml").write_text("mcp: [unclosed\n")
    assert resolve_mcp_inheritance("claude-sdk").inherit
    assert "cannot read MCP inheritance" in caplog.text
