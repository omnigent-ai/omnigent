"""Private native settings preserve plugin intent, availability and policy precedence."""

import json
from pathlib import Path

import pytest

from omnigent.preference_startup import (
    PREFERENCE_SNAPSHOT_ENV,
)
from omnigent.preference_sync import (
    PreferenceNamespace,
    SharedPreferenceEntry,
    SharedPreferenceKey,
)
from tests._helpers.preferences import preference_snapshot


def plugin_snapshot(root: Path, ecosystem: str, values: dict[str, bool]) -> Path:
    return preference_snapshot(
        root,
        identity=PreferenceNamespace(
            authority_id="plugin-test", user_id="alice", workspace_id=root.name
        ),
        shared=[
            SharedPreferenceEntry(
                key=SharedPreferenceKey(
                    kind="plugin",
                    name={
                        "ecosystem": ecosystem,
                        "name": key.split("@")[0],
                        "source": key.split("@")[1],
                    },
                ),
                version=1,
                value=value,
            )
            for key, value in values.items()
        ],
    )


def test_claude_overlay_keeps_project_policy_and_missing_intent(tmp_path, monkeypatch):
    from omnigent.preference_plugins import claude_plugin_overlay, requested_plugins
    from omnigent.spec.skill_sources import SkillSourceContext, resolve_harness_skills
    from tests.spec.test_skill_sources import _claude_home_with_plugin

    home = _claude_home_with_plugin(
        tmp_path / "home", plugin="toolkit", marketplace="source", skill="check", enabled=True
    )
    config = home / ".claude"
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = plugin_snapshot(
        tmp_path / "one", "claude", {"toolkit@source": False, "missing@source": True}
    )
    monkeypatch.setenv(PREFERENCE_SNAPSHOT_ENV, str(path))
    ctx = SkillSourceContext(
        (workspace,), home, "all", None, claude_config_dir=config, is_native=True
    )
    before = (config / "settings.json").read_bytes()
    overlay = claude_plugin_overlay(ctx)
    assert overlay.enabled == {"toolkit@source": False, "missing@source": False}
    assert "not installed" in overlay.warnings[0]
    assert requested_plugins("claude", workspace)["missing@source"] is True
    assert resolve_harness_skills(ctx, "claude-native") == []
    (workspace / ".claude").mkdir()
    project = workspace / ".claude/settings.local.json"
    project.write_text(json.dumps({"enabledPlugins": {"toolkit@source": True}}))
    assert claude_plugin_overlay(ctx).enabled["toolkit@source"] is True
    assert "overrides" in claude_plugin_overlay(ctx).warnings[0]
    project.write_text(json.dumps({"enabledPlugins": {"toolkit@source": False}}))
    (config / "plugins/managed_plugins.json").write_text(
        json.dumps({"managed_plugins": ["toolkit@source"]})
    )
    assert claude_plugin_overlay(ctx).enabled["toolkit@source"] is True
    assert (config / "settings.json").read_bytes() == before


def test_codex_overlay_never_writes_source_or_installs_missing(tmp_path, monkeypatch):
    from omnigent.preference_plugins import apply_codex_plugin_overlay, requested_plugins

    source, private = tmp_path / "source", tmp_path / "private"
    source.mkdir()
    private.mkdir()
    (source / "config.toml").write_text("# original\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    path = plugin_snapshot(tmp_path / "one", "codex", {"missing@source": True})
    monkeypatch.setenv(PREFERENCE_SNAPSHOT_ENV, str(path))
    monkeypatch.setattr("omnigent.spec.codex_plugin_skills._inventory_plugins", lambda *a, **k: [])
    with pytest.raises(ValueError, match="private Codex"):
        apply_codex_plugin_overlay(source, source, tmp_path, codex_path="unused")
    result = apply_codex_plugin_overlay(private, source, tmp_path, codex_path="unused")
    assert result.enabled == {"missing@source": False}
    assert "not installed" in result.warnings[0]
    assert requested_plugins("codex", tmp_path)["missing@source"] is True
    assert not (private / "plugins").exists()
    assert (source / "config.toml").read_text() == "# original\n"
    (private / "config.toml").unlink()
    (private / "config.toml").symlink_to(source / "config.toml")
    with pytest.raises(ValueError, match="private Codex"):
        apply_codex_plugin_overlay(private, source, tmp_path, codex_path="unused")


def test_import_reads_user_toggles_without_project_or_managed_policy(tmp_path, monkeypatch):
    from omnigent.preference_plugins import import_plugin_values
    from omnigent.preferences import PluginIdentity, PreferenceKey, PreferenceKind

    claude, codex = tmp_path / "claude", tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    (claude / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"shared@source-a": True}})
    )
    (claude / "settings.local.json").write_text(
        json.dumps({"enabledPlugins": {"shared@source-a": False}})
    )
    (claude / "plugins").mkdir()
    (claude / "plugins/managed_plugins.json").write_text(
        json.dumps({"managed_plugins": ["managed@source"]})
    )
    (codex / "config.toml").write_text('[plugins."shared@source-b"]\nenabled = true\n')
    values = import_plugin_values(("claude", "codex"))
    assert values == {
        PreferenceKey(
            PreferenceKind.PLUGIN, PluginIdentity("claude", "source-a", "shared")
        ): False,
        PreferenceKey(PreferenceKind.PLUGIN, PluginIdentity("codex", "source-b", "shared")): True,
    }
