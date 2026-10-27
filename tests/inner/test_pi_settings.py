"""Tests for managed Pi agent dir seeding (extensions / packages)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_prepare_managed_pi_agent_dir_links_only_supported_resources(tmp_path: Path) -> None:
    """Supported resources are linked; instructions and session state stay isolated."""
    global_agent = tmp_path / "global-agent"
    for directory in (
        "npm",
        "git",
        "skills/review",
        "extensions",
        "prompts",
        "themes",
        "sessions",
    ):
        (global_agent / directory).mkdir(parents=True)
    for name in (
        "mcp.json",
        "mcp-auth.json",
        "keybindings.json",
        "auth.json",
        "models.json",
        "AGENTS.md",
        "SYSTEM.md",
        "APPEND_SYSTEM.md",
    ):
        (global_agent / name).write_text("{}", encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    for name in (
        "npm",
        "git",
        "mcp.json",
        "mcp-auth.json",
        "skills",
        "extensions",
        "prompts",
        "themes",
        "keybindings.json",
    ):
        assert (managed / name).readlink() == global_agent / name
    for name in (
        "sessions",
        "auth.json",
        "models.json",
        "AGENTS.md",
        "SYSTEM.md",
        "APPEND_SYSTEM.md",
    ):
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


@pytest.mark.parametrize("parent_relative_root", [False, True])
def test_relative_settings_keep_their_original_root(
    tmp_path: Path, parent_relative_root: bool
) -> None:
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    settings = {
        "extensions": [
            "./custom/demo.js",
            "!custom/disabled.js",
            "-extensions/off.js",
            "-builtin:mcp",
            "-" + str(global_agent / "extensions/disabled.js"),
        ],
        "skills": ["../shared-skills", "/absolute/skills", "~/skills"],
        "packages": [
            "npm:@example/package",
            "./local-package",
            {"source": "../package", "skills": []},
        ],
    }
    settings_path = global_agent / "settings.json"
    settings_path.write_text(json.dumps(settings))
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "settings.json").symlink_to(settings_path)

    (tmp_path / "workspace").mkdir()
    root = tmp_path / "workspace/../global-agent" if parent_relative_root else global_agent
    prepare_managed_pi_agent_dir(managed, global_agent_dir=root)

    written = json.loads((managed / "settings.json").read_text())
    assert written["extensions"] == [
        str(global_agent / "custom/demo.js"),
        "!custom/disabled.js",
        "!" + str(global_agent / "custom/disabled.js"),
        "-extensions/off.js",
        "-" + str(global_agent / "extensions/off.js"),
        "-builtin:mcp",
        "-" + str(global_agent / "extensions/disabled.js"),
        "-" + str(managed / "extensions/disabled.js"),
    ]
    assert written["skills"] == [
        str(global_agent / "../shared-skills"),
        "/absolute/skills",
        "~/skills",
    ]
    assert written["packages"] == [
        "npm:@example/package",
        str(global_agent / "local-package"),
        {"source": str(global_agent / "../package"), "skills": []},
    ]
    assert json.loads(settings_path.read_text()) == settings
    assert not (managed / "settings.json").is_symlink()


@pytest.mark.parametrize("content", [b"{invalid", b"[]", b"\xff"])
def test_invalid_settings_do_not_hide_resources(tmp_path: Path, content: bytes) -> None:
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_bytes(content)
    (global_agent / "mcp.json").write_text("{malformed; Pi validates this}")
    managed = tmp_path / "managed"

    prepare_managed_pi_agent_dir(
        managed, global_agent_dir=global_agent, overlay={"retry": {"enabled": True}}
    )

    assert json.loads((managed / "settings.json").read_text()) == {"retry": {"enabled": True}}
    assert (managed / "mcp.json").is_symlink()


def test_source_is_never_rewritten_in_place(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    prepare_managed_pi_agent_dir(
        tmp_path, global_agent_dir=tmp_path, overlay={"retry": {"enabled": True}}
    )
    assert settings_path.read_text() == "{}"
