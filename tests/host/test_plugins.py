"""Installed plugin inventory uses the shared scope rules and exports only metadata."""

import json
from pathlib import Path

import pytest

from omnigent.host.plugins import MAX_PLUGIN_DESCRIPTION, discover_plugins
from omnigent.spec.skill_sources import _plugin_asset_id


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_installed_plugins_scope_metadata_and_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    root = config / "plugins"
    entries = {}
    for name in ("tools", "hooks-only", "disabled", "managed"):
        directory = root / "cache" / name
        _json(
            directory / ".claude-plugin" / "plugin.json",
            {
                "version": "1.2.3",
                "description": "A\nplugin\x00",
                "hooks": {"command": "synthetic-secret"},
            },
        )
        entries[f"{name}@market"] = [{"installPath": str(directory)}]
    entries["outside@market"] = [{"installPath": str(tmp_path / "private")}]
    (tmp_path / "private").mkdir()
    _json(root / "installed_plugins.json", {"plugins": entries})
    _json(root / "managed_plugins.json", {"managed_plugins": ["managed@market"]})
    _json(config / "settings.json", {"enabledPlugins": dict.fromkeys(entries, True)})
    _json(
        config / "settings.local.json",
        {"enabledPlugins": {"disabled@market": False, "managed@market": False}},
    )
    _json(
        tmp_path / ".claude" / "settings.local.json",
        {"enabledPlugins": {"tools@market": False, "disabled@market": True}},
    )
    tools = root / "cache" / "tools"
    (tools / "skills" / "review").mkdir(parents=True)
    (tools / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\nprivate instructions"
    )
    (tools / "skills" / "broken").mkdir()
    (tools / "skills" / "broken" / "SKILL.md").write_text("no frontmatter")
    (tools / "commands").mkdir()
    _json(
        tools / ".mcp.json",
        {
            "mcpServers": {
                "docs": {
                    "command": "synthetic-command",
                    "args": ["synthetic-secret"],
                    "env": {"TOKEN": "synthetic-secret"},
                }
            }
        },
    )

    plugins = {plugin["name"]: plugin for plugin in discover_plugins()}
    assert set(plugins) == {"tools", "hooks-only", "disabled", "managed"}
    assert plugins["disabled"]["enabled"] is False
    assert plugins["managed"]["enabled"] is True
    assert plugins["hooks-only"]["skills"] == []
    assert plugins["tools"] == {
        "id": _plugin_asset_id("tools@market", "plugin"),
        "skill_entries": [
            {"id": _plugin_asset_id("tools@market", "skill", "skills/review"), "name": "review"}
        ],
        "mcp_entries": [{"id": _plugin_asset_id("tools@market", "mcp", "docs"), "name": "docs"}],
        "harness": "claude",
        "name": "tools",
        "marketplace": "market",
        "version": "1.2.3",
        "description": "A plugin",
        "enabled": True,
        "skills": ["review"],
        "mcp_servers": ["docs"],
        "has_hooks": True,
        "has_commands": True,
    }
    payload = json.dumps(plugins)
    for private in (
        str(tmp_path),
        "synthetic-secret",
        "synthetic-command",
        "private instructions",
        "installPath",
    ):
        assert private not in payload


def test_missing_and_bounded_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert discover_plugins() == []
    root = tmp_path / ".claude" / "plugins"
    _json(
        root / "installed_plugins.json",
        {"plugins": {"tool@market": [{"installPath": "cache/tool"}]}},
    )
    _json(
        root / "cache" / "tool" / ".claude-plugin" / "plugin.json",
        {"description": "x" * 5000, "version": {"token": "secret"}},
    )
    plugin = discover_plugins()[0]
    assert len(plugin["description"]) == MAX_PLUGIN_DESCRIPTION
    assert plugin["version"] is None
    assert plugin["enabled"] is False
