"""Enabled Claude Code plugins reach managed Pi agent dirs through Pi settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.inner.pi_claude_plugins import (
    PI_CLAUDE_PLUGIN_RESOURCES_DIR,
    add_claude_plugin_resources,
)
from omnigent.server.feature_flags import FEATURES_ENV_VAR


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Claude config dir with the pi_claude_plugins feature enabled."""
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv(FEATURES_ENV_VAR, "pi_claude_plugins")
    return config


def _install(
    claude: Path,
    key: str,
    *,
    files: dict[str, str],
    enabled: bool = True,
) -> Path:
    """Install a plugin under the cache and register it as enabled or disabled."""
    name, _, marketplace = key.partition("@")
    root = claude / "plugins" / "cache" / marketplace / name / "1.0.0"
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    installed_path = claude / "plugins" / "installed_plugins.json"
    installed = json.loads(installed_path.read_text()) if installed_path.exists() else {}
    installed.setdefault("plugins", {})[key] = [{"installPath": str(root)}]
    _write_json(installed_path, installed)
    settings_path = claude / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings.setdefault("enabledPlugins", {})[key] = enabled
    _write_json(settings_path, settings)
    return root.resolve()


def _skill(name: str) -> dict[str, str]:
    return {"skills/" + name + "/SKILL.md": f"---\nname: {name}\ndescription: d\n---\nBody\n"}


def test_flag_off_leaves_settings_untouched(
    claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the feature, no plugin entries and no generated prompts are written."""
    monkeypatch.delenv(FEATURES_ENV_VAR)
    _install(claude_dir, "dp@market", files=_skill("review"))
    settings = {"skills": ["/user/skills"]}

    managed = tmp_path / "managed"
    assert add_claude_plugin_resources(settings, managed) == {"skills": ["/user/skills"]}
    assert not (managed / PI_CLAUDE_PLUGIN_RESOURCES_DIR).exists()


def test_enabled_plugin_skills_and_namespaced_commands(claude_dir: Path, tmp_path: Path) -> None:
    """Skills are linked by directory; commands get Claude's ``<plugin>:<command>`` names."""
    root = _install(
        claude_dir,
        "dp@market",
        files={
            **_skill("code-review"),
            "commands/review.md": "---\ndescription: r\nallowed-tools: Bash(git:*)\n---\n"
            "Focus $ARGUMENTS from ${CLAUDE_PLUGIN_ROOT}\n",
            "commands/sub/deep.md": "Deep $ARGUMENTS\n",
        },
    )
    managed = tmp_path / "managed"

    result = add_claude_plugin_resources({}, managed)

    assert result["skills"] == [str(root / "skills")]
    prompts = managed / PI_CLAUDE_PLUGIN_RESOURCES_DIR / "prompts"
    assert result["prompts"] == [str(prompts)]
    assert sorted(p.name for p in prompts.iterdir()) == ["dp:review.md", "dp:sub:deep.md"]
    review = (prompts / "dp:review.md").read_text(encoding="utf-8")
    assert f"from {root}" in review
    assert "${CLAUDE_PLUGIN_ROOT}" not in review
    assert "allowed-tools" in review


def test_disabled_and_unknown_plugins_are_not_loaded(claude_dir: Path, tmp_path: Path) -> None:
    """Only plugins Claude Code has enabled, or policy force-enables, are exposed."""
    _install(claude_dir, "off@market", files=_skill("off"), enabled=False)
    _install(claude_dir, "on@market", files=_skill("on"))
    _write_json(
        claude_dir / "plugins" / "managed_plugins.json",
        {"managed_plugins": ["managed@market"]},
    )
    _install(claude_dir, "managed@market", files=_skill("managed"))
    # Removing enablement for the managed plugin: policy still force-enables it.
    settings_path = claude_dir / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["enabledPlugins"].pop("managed@market")
    _write_json(settings_path, settings)

    result = add_claude_plugin_resources({}, tmp_path / "managed")

    paths = result["skills"]
    assert any("/on/" in p for p in paths)
    assert any("/managed/" in p for p in paths)
    assert not any("/off/" in p for p in paths)


def test_pi_manifest_plugin_loads_as_package_and_keeps_commands(
    claude_dir: Path, tmp_path: Path
) -> None:
    """A plugin declaring a ``pi`` manifest loads through Pi packages, not a skills bridge."""
    root = _install(
        claude_dir,
        "ext@market",
        files={
            "package.json": json.dumps({"name": "ext", "pi": {"extensions": ["./x.js"]}}),
            "skills/unused/SKILL.md": "---\nname: unused\ndescription: d\n---\n",
            "commands/go.md": "Go $ARGUMENTS\n",
        },
    )

    result = add_claude_plugin_resources({}, tmp_path / "managed")

    assert result["packages"] == [str(root)]
    assert "skills" not in result
    assert (
        tmp_path / "managed" / PI_CLAUDE_PLUGIN_RESOURCES_DIR / "prompts" / "ext:go.md"
    ).exists()


def test_user_choices_are_preserved_without_duplicates(claude_dir: Path, tmp_path: Path) -> None:
    """A path the user names or excludes is not added again; user entries stay in order."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    skills_dir = str(root / "skills")
    settings = {
        "skills": ["/user/skills", skills_dir],
        "packages": [{"source": str(root)}],
    }

    result = add_claude_plugin_resources(settings, tmp_path / "managed")

    assert result["skills"] == ["/user/skills", skills_dir]
    assert result["packages"] == [{"source": str(root)}]


def test_user_exclusions_block_plugin_entries(claude_dir: Path, tmp_path: Path) -> None:
    """A ``-path`` or ``!glob`` the user wrote keeps that plugin's skills out of the session."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    excluded = {"skills": [f"-{root / 'skills'}"]}
    assert add_claude_plugin_resources(excluded, tmp_path / "a")["skills"] == excluded["skills"]

    glob = {"skills": [f"!{root.parent}/*/skills"]}
    assert add_claude_plugin_resources(glob, tmp_path / "b")["skills"] == glob["skills"]


def test_non_list_user_setting_is_left_alone(claude_dir: Path, tmp_path: Path) -> None:
    """A malformed user array is not overwritten; other resource kinds still apply."""
    _install(claude_dir, "dp@market", files={**_skill("review"), "commands/c.md": "C\n"})

    result = add_claude_plugin_resources({"skills": "not-a-list"}, tmp_path / "managed")

    assert result["skills"] == "not-a-list"
    assert "prompts" in result


def test_same_command_from_two_marketplaces_keeps_the_first(
    claude_dir: Path, tmp_path: Path
) -> None:
    """Two plugins that share a name cannot both own ``<name>:<command>``."""
    _install(claude_dir, "dp@first", files={"commands/go.md": "FIRST\n"})
    _install(claude_dir, "dp@second", files={"commands/go.md": "SECOND\n"})
    prompts = tmp_path / "managed" / PI_CLAUDE_PLUGIN_RESOURCES_DIR / "prompts"

    add_claude_plugin_resources({}, tmp_path / "managed")

    assert (prompts / "dp:go.md").read_text(encoding="utf-8") == "FIRST\n"


def test_relaunch_drops_commands_of_removed_plugins(claude_dir: Path, tmp_path: Path) -> None:
    """Each launch regenerates the command dir, so stale prompts do not survive."""
    _install(claude_dir, "dp@market", files={"commands/go.md": "Go\n"})
    managed = tmp_path / "managed"
    add_claude_plugin_resources({}, managed)
    prompts = managed / PI_CLAUDE_PLUGIN_RESOURCES_DIR / "prompts"
    assert (prompts / "dp:go.md").exists()

    _install(claude_dir, "dp@market", files={}, enabled=False)
    result = add_claude_plugin_resources({}, managed)

    assert "prompts" not in result
    assert not (managed / PI_CLAUDE_PLUGIN_RESOURCES_DIR).exists()


def test_symlinked_command_files_are_not_read(claude_dir: Path, tmp_path: Path) -> None:
    """A command symlink, or a commands/ root that links out, cannot expose outside files."""
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET_OUTSIDE_PLUGIN", encoding="utf-8")
    root = _install(claude_dir, "dp@market", files={"commands/ok.md": "OK\n"})
    (root / "commands" / "leak.md").symlink_to(secret)
    managed = tmp_path / "managed"

    add_claude_plugin_resources({}, managed)

    prompts = managed / PI_CLAUDE_PLUGIN_RESOURCES_DIR / "prompts"
    assert sorted(p.name for p in prompts.iterdir()) == ["dp:ok.md"]

    outside = tmp_path / "outside-commands"
    outside.mkdir()
    (outside / "x.md").write_text("SECRET_OUTSIDE_PLUGIN", encoding="utf-8")
    other = _install(claude_dir, "linked@market", files={"placeholder": ""})
    (other / "commands").symlink_to(outside, target_is_directory=True)

    add_claude_plugin_resources({}, managed)

    assert not (prompts / "linked:x.md").exists()
    assert "SECRET_OUTSIDE_PLUGIN" not in "".join(p.read_text() for p in prompts.iterdir())


def test_unsafe_plugin_names_are_not_bridged(claude_dir: Path, tmp_path: Path) -> None:
    """A plugin name becomes a filename, so names with separators or spaces are skipped."""
    _install(claude_dir, "bad name@market", files={"commands/go.md": "X\n", **_skill("x")})

    result = add_claude_plugin_resources({}, tmp_path / "managed")

    assert result == {}


def test_user_package_entry_decides_plugin_skills(claude_dir: Path, tmp_path: Path) -> None:
    """A user package entry for the plugin with ``skills: []`` keeps its skills out."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    settings = {"packages": [{"source": str(root), "skills": []}]}

    result = add_claude_plugin_resources(settings, tmp_path / "managed")

    assert "skills" not in result
    assert result["packages"] == [{"source": str(root), "skills": []}]


def test_native_provider_launch_writes_plugin_entries(claude_dir: Path, tmp_path: Path) -> None:
    """The ``omnigent pi`` terminal launch seeds the same managed settings as the gateway."""
    from omnigent.harnesses.pi_native.credentials import (
        PiProviderConfig,
        pi_native_provider_launch,
    )

    root = _install(claude_dir, "dp@market", files=_skill("review"))
    provider = PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
    )
    agent_dir = tmp_path / "pi-agent"

    pi_native_provider_launch(agent_dir, provider, global_agent_dir=tmp_path / "global")

    written = json.loads((agent_dir / "settings.json").read_text(encoding="utf-8"))
    assert written["skills"] == [str(root / "skills")]
