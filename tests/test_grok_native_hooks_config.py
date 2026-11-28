"""Unit tests for the per-session grok policy plugin writer."""

from __future__ import annotations

import json

from omnigent.harnesses.grok_native.hooks_config import write_grok_policy_plugin


def test_plugin_layout_discoverable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin_dir = write_grok_policy_plugin("sess-9")
    assert plugin_dir.parent == tmp_path / ".grok" / "plugins"
    assert plugin_dir.name == "omnigent-grok-native-sess-9"
    manifest = json.loads((plugin_dir / "plugin.json").read_text())
    assert manifest["name"] == "omnigent-grok-native"
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text())
    handler = hooks["PreToolUse"][0]["hooks"][0]
    assert handler["type"] == "command"
    script = plugin_dir / "omnigent_policy_hook.py"
    assert script.exists() and script.stat().st_mode & 0o111
    assert str(script) in handler["command"]


def test_hook_script_consults_env_endpoints(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin_dir = write_grok_policy_plugin("sess-9")
    script = (plugin_dir / "omnigent_policy_hook.py").read_text()
    assert "OMNIGENT_POLICY_EVALUATE_URL" in script
    assert "OMNIGENT_PERMISSION_URL" in script
    assert "permissionDecision" in script
