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


def test_prepare_managed_pi_agent_dir_links_only_packages_and_mcp(tmp_path: Path) -> None:
    """MCP config/auth and install trees are linked; other user entries stay out."""
    global_agent = tmp_path / "global-agent"
    for directory in ("npm", "git", "skills/review"):
        (global_agent / directory).mkdir(parents=True)
    for name in ("mcp.json", "mcp-auth.json", "AGENTS.md", "SYSTEM.md", "APPEND_SYSTEM.md"):
        (global_agent / name).write_text("{}", encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    for name in ("npm", "git", "mcp.json", "mcp-auth.json"):
        assert (managed / name).readlink() == global_agent / name
    for name in ("skills", "AGENTS.md", "SYSTEM.md", "APPEND_SYSTEM.md"):
        assert not (managed / name).exists(), name
    assert not (managed / "settings.json").is_symlink()


def test_prepare_managed_pi_agent_dir_replaces_stale_links_on_reuse(tmp_path: Path) -> None:
    """A resumed session drops links to a previous root or removed sources."""
    previous_root = tmp_path / "previous-agent"
    previous_root.mkdir()
    (previous_root / "mcp.json").write_text("{}", encoding="utf-8")
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "mcp.json").write_text("{}", encoding="utf-8")
    (global_agent / "mcp-auth.json").write_text("{}", encoding="utf-8")

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "mcp.json").symlink_to(previous_root / "mcp.json")
    (managed / "mcp-auth.json").symlink_to(tmp_path / "removed-auth.json")
    (managed / "git").symlink_to(global_agent / "git", target_is_directory=True)
    (managed / "npm").mkdir()

    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    assert (managed / "mcp.json").readlink() == global_agent / "mcp.json"
    assert (managed / "mcp-auth.json").readlink() == global_agent / "mcp-auth.json"
    assert not (managed / "git").is_symlink() and not (managed / "git").exists()
    assert not (managed / "npm").is_symlink() and (managed / "npm").is_dir()
