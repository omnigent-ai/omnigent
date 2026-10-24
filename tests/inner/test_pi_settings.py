"""Tests for managed Pi agent dir seeding (extensions / packages)."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.inner.pi_settings import prepare_managed_pi_agent_dir


def test_prepare_managed_pi_agent_dir_copies_settings_and_symlinks_npm(
    tmp_path: Path,
) -> None:
    """Gateway managed dir gets global settings metadata and npm install tree."""
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_text(
        json.dumps(
            {
                "extensions": ["/tmp/my-ext.ts"],
                "packages": ["npm:@foo/bar"],
            }
        ),
        encoding="utf-8",
    )
    npm_dir = global_agent / "npm" / "foo"
    npm_dir.mkdir(parents=True)

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "models.json").write_text("{}", encoding="utf-8")

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"retry": {"maxRetries": 5}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written["extensions"] == ["/tmp/my-ext.ts"]
    assert written["packages"] == ["npm:@foo/bar"]
    assert written["retry"] == {"maxRetries": 5}
    assert (managed / "npm").is_symlink()
    assert (managed / "npm").resolve() == (global_agent / "npm").resolve()


def test_prepare_managed_pi_agent_dir_empty_global_writes_overlay_only(
    tmp_path: Path,
) -> None:
    """Missing global settings still writes the Omnigent overlay."""
    global_agent = tmp_path / "empty-global"
    global_agent.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"retry": {"enabled": True}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written == {"retry": {"enabled": True}}


def test_prepare_managed_pi_agent_dir_deep_merges_nested_overlay(tmp_path: Path) -> None:
    """Nested settings (e.g. compaction) merge like Pi project overrides."""
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_text(
        json.dumps({"compaction": {"enabled": True, "reserveTokens": 16384}}),
        encoding="utf-8",
    )
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"compaction": {"reserveTokens": 8192}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written["compaction"] == {
        "enabled": True,
        "reserveTokens": 8192,
    }


def test_prepare_managed_pi_agent_dir_links_global_entries_except_unshared(
    tmp_path: Path,
) -> None:
    """MCP config, skills and other user entries are linked; Omnigent-owned state is not."""
    global_agent = tmp_path / "global-agent"
    for directory in ("skills/review", "extensions", "npm", "sessions", "tmp"):
        (global_agent / directory).mkdir(parents=True)
    shared_files = ("mcp.json", "mcp-auth.json", "AGENTS.md", "keybindings.json")
    unshared_files = (
        "auth.json",
        "oauth.json",
        "models-store.json",
        "mcp.log",
        "crashes.json",
        "pi-debug.log",
    )
    for name in (*shared_files, *unshared_files, "settings.json", "models.json"):
        (global_agent / name).write_text("{}", encoding="utf-8")

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "models.json").write_text('{"providers": {}}', encoding="utf-8")

    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    for name in (*shared_files, "skills", "extensions", "npm"):
        assert (managed / name).is_symlink(), name
        assert (managed / name).resolve() == (global_agent / name).resolve()
    for name in (*unshared_files, "sessions", "tmp"):
        assert not (managed / name).exists(), name
    assert not (managed / "models.json").is_symlink()
    assert (managed / "models.json").read_text(encoding="utf-8") == '{"providers": {}}'
    assert not (managed / "settings.json").is_symlink()


def test_prepare_managed_pi_agent_dir_keeps_existing_managed_entries(
    tmp_path: Path,
) -> None:
    """A relaunch reuses whatever the managed dir already holds, dangling links included."""
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "keybindings.json").write_text("{}", encoding="utf-8")
    (global_agent / "mcp.json").write_text("{}", encoding="utf-8")

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "keybindings.json").write_text('{"managed": true}', encoding="utf-8")
    dangling_target = tmp_path / "removed-mcp.json"
    (managed / "mcp.json").symlink_to(dangling_target)

    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    assert not (managed / "keybindings.json").is_symlink()
    assert (managed / "keybindings.json").read_text(encoding="utf-8") == '{"managed": true}'
    assert (managed / "mcp.json").readlink() == dangling_target


def test_prepare_managed_pi_agent_dir_without_global_dir(tmp_path: Path) -> None:
    """A user who never ran ``pi`` gets settings only and no links."""
    managed = tmp_path / "managed"

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"retry": {"enabled": True}},
        global_agent_dir=tmp_path / "missing-global",
    )

    assert sorted(entry.name for entry in managed.iterdir()) == ["settings.json"]
