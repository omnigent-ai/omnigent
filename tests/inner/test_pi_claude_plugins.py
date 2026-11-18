"""Enabled Claude Code plugins reach managed Pi agent dirs through Pi settings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnigent.inner.pi_claude_plugins import (
    _MAX_WALK_DEPTH,
    _MAX_WALK_DIRS,
    MAX_PLUGIN_COMMANDS,
    PI_CLAUDE_PLUGIN_RESOURCES_DIR,
    _bounded_files,
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

    assert result["skills"] == [str(root / "skills" / "code-review")]
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


def test_installed_but_unenabled_and_project_only_enablement_are_not_loaded(
    claude_dir: Path, tmp_path: Path
) -> None:
    """Install records alone do not enable a plugin, and project settings are not read."""
    unenabled = _install(claude_dir, "unenabled@market", files=_skill("unenabled"))
    settings_path = claude_dir / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["enabledPlugins"].pop("unenabled@market")
    _write_json(settings_path, settings)
    project = tmp_path / "project"
    _write_json(project / ".claude" / "settings.json", {"enabledPlugins": {"proj@market": True}})
    _install(claude_dir, "proj@market", files=_skill("proj"))
    settings = json.loads(settings_path.read_text())
    settings["enabledPlugins"].pop("proj@market")
    _write_json(settings_path, settings)

    result = add_claude_plugin_resources({}, tmp_path / "managed")

    assert str(unenabled / "skills" / "unenabled") not in result.get("skills", [])
    assert not any("/proj/" in p for p in result.get("skills", []))


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

    # A package the user already lists is not listed twice.
    listed = add_claude_plugin_resources({"packages": [str(root)]}, tmp_path / "again")
    assert listed["packages"] == [str(root)]


def test_user_choices_are_preserved_without_duplicates(claude_dir: Path, tmp_path: Path) -> None:
    """An existing skill entry is not added again, and user entries keep their order."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    skill_dir = str(root / "skills" / "review")

    result = add_claude_plugin_resources({"skills": ["/user/skills", skill_dir]}, tmp_path / "m")

    assert result["skills"] == ["/user/skills", skill_dir]


def test_user_exclusions_block_plugin_entries(claude_dir: Path, tmp_path: Path) -> None:
    """A ``-path`` or ``!glob`` the user wrote keeps that plugin's skills out of the session."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    excluded = {"skills": [f"-{root / 'skills'}"]}
    assert add_claude_plugin_resources(excluded, tmp_path / "a")["skills"] == excluded["skills"]

    glob = {"skills": [f"!{root}/skills/*"]}
    assert add_claude_plugin_resources(glob, tmp_path / "b")["skills"] == glob["skills"]


def test_relative_user_entries_do_not_match_the_process_cwd(
    claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative entry is Pi's to resolve; it must not claim a same-named path under cwd."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    monkeypatch.chdir(root)

    result = add_claude_plugin_resources({"skills": ["skills/review"]}, tmp_path / "managed")

    assert result["skills"] == ["skills/review", str(root / "skills" / "review")]


def test_user_exclusions_block_individual_commands(claude_dir: Path, tmp_path: Path) -> None:
    """Exclusions match the original command files, not only the generated copies."""
    root = _install(
        claude_dir,
        "dp@market",
        files={
            "commands/go.md": "GO\n",
            "commands/keep.md": "KEEP\n",
            "commands/sub/x.md": "SUB\n",
        },
    )
    settings = {"prompts": [f"-{root / 'commands' / 'go.md'}", f"!{root}/commands/sub/*"]}
    managed = tmp_path / "managed"

    add_claude_plugin_resources(settings, managed)

    prompts = managed / PI_CLAUDE_PLUGIN_RESOURCES_DIR / "prompts"
    assert [p.name for p in prompts.iterdir()] == ["dp:keep.md"]


def test_unresolvable_user_paths_do_not_abort_preparation(
    claude_dir: Path, tmp_path: Path
) -> None:
    """Symlink loops and NUL bytes in user settings leave the other entries working."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    loop_a, loop_b = tmp_path / "loop-a", tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    settings = {
        "skills": [str(loop_a), "bad\0path"],
        "packages": [str(loop_a), "bad\0package"],
    }

    result = add_claude_plugin_resources(settings, tmp_path / "managed")

    assert result["skills"] == [str(loop_a), "bad\0path", str(root / "skills" / "review")]
    assert result["packages"] == [str(loop_a), "bad\0package"]


def test_unresolvable_user_paths_leave_settings_unchanged_without_plugins(
    claude_dir: Path, tmp_path: Path
) -> None:
    """With no plugins enabled, unusable user entries are returned exactly as written."""
    settings = {"skills": ["bad\0path"], "prompts": ["bad\0prompt"]}

    result = add_claude_plugin_resources(settings, tmp_path / "managed")

    assert result == settings


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


@pytest.mark.parametrize(
    "listed",
    [lambda root: {"source": str(root), "skills": []}, lambda root: {"source": str(root)}],
    ids=["object-with-empty-skills", "object-plain"],
)
def test_user_package_entry_decides_plugin_skills(
    claude_dir: Path, tmp_path: Path, listed
) -> None:
    """A user package entry for the plugin decides its fallback skills, including none."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    settings = {"packages": [listed(root)]}

    result = add_claude_plugin_resources(settings, tmp_path / "managed")

    assert "skills" not in result
    assert result["packages"] == [listed(root)]


def test_overlong_generated_command_name_is_skipped(claude_dir: Path, tmp_path: Path) -> None:
    """An over-long generated command name is skipped without failing preparation."""
    long_part = "a" * 128
    _install(
        claude_dir,
        "demo@market",
        files={
            f"commands/{long_part}/{long_part}.md": "LONG\n",
            "commands/ok.md": "OK\n",
        },
    )
    managed = tmp_path / "managed"

    result = add_claude_plugin_resources({}, managed)

    prompts = managed / PI_CLAUDE_PLUGIN_RESOURCES_DIR / "prompts"
    assert result["prompts"] == [str(prompts)]
    assert [p.name for p in prompts.iterdir()] == ["demo:ok.md"]


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
    assert written["skills"] == [str(root / "skills" / "review")]


@dataclass(frozen=True)
class _Targets:
    """Paths one consumer decides on, all resolved."""

    cache: Path
    root: Path
    skill_dir: Path
    skill_file: Path
    command: Path
    sibling: Path


def _install_for(consumer: str, claude: Path, tmp_path: Path) -> _Targets:
    """Install a plugin shaped for *consumer* and return the paths to decide on."""
    if consumer == "commands":
        root = _install(
            claude,
            "dp@market",
            files={"commands/go.md": "GO\n", "commands/other.md": "OTHER\n"},
        )
    elif consumer == "packages":
        root = _install(
            claude,
            "dp@market",
            files={
                "package.json": json.dumps({"name": "dp", "pi": {"extensions": ["./x.js"]}}),
                **_skill("review"),
            },
        )
    else:
        root = _install(claude, "dp@market", files=_skill("review"))
    return _Targets(
        cache=claude / "plugins" / "cache",
        root=root,
        skill_dir=root / "skills" / "review",
        skill_file=root / "skills" / "review" / "SKILL.md",
        command=root / "commands" / "go.md",
        sibling=root / "commands" / "other.md",
    )


_CONSUMER_KEY = {"skills": "skills", "packages": "packages", "commands": "prompts"}
_ENTRY_KINDS = (
    "absolute",
    "include",
    "exclude",
    "relative",
    "tilde",
    "tilde_unknown_user",
    "symlink_loop",
    "nul",
    "ancestor_exclusion",
    "descendant_exclusion",
    "glob_ancestor",
    "glob_target",
    "glob_descendant",
    "glob_unrelated",
)
# Kinds the user has not decided on: the plugin entry is still bridged.
_NOT_DECIDING = {"relative", "tilde_unknown_user", "symlink_loop", "nul", "glob_unrelated"}
# Truncation applies to consumers that walk a tree; packages are never walked.
_TRUNCATIONS = ("none", "files", "dirs", "depth")


def _truncate(consumer: str, targets: _Targets, mode: str) -> None:
    """Make the consumer's tree too large or too deep to inspect completely."""
    if mode == "none":
        return
    tree = {"skills": targets.skill_dir, "commands": targets.command.parent}[consumer]
    suffix = ".txt" if consumer == "skills" else ".md"
    if mode == "files":
        for index in range(MAX_PLUGIN_COMMANDS + 8):
            (tree / f"{index:04d}{suffix}").write_text("x", encoding="utf-8")
    elif mode == "dirs":
        for index in range(_MAX_WALK_DIRS + 40):
            (tree / f"d{index:03d}").mkdir(parents=True)
    elif mode == "depth":
        deep = tree.joinpath(*[f"l{level}" for level in range(_MAX_WALK_DEPTH + 2)])
        deep.mkdir(parents=True)
        (deep / f"x{suffix}").write_text("x", encoding="utf-8")


def _entry(kind: str, consumer: str, t: _Targets, tmp_path: Path) -> str:
    target = {"skills": t.skill_dir, "packages": t.root, "commands": t.command}[consumer]
    below = {"skills": t.skill_file, "packages": t.root / "package.json", "commands": t.sibling}
    tree = {"skills": t.skill_dir, "packages": t.root, "commands": t.command.parent}[consumer]
    loop = tmp_path / "loop-a"
    if kind == "absolute":
        return str(target)
    if kind == "include":
        return f"+{target}"
    if kind == "exclude":
        return f"-{target}"
    if kind == "relative":
        return target.relative_to(t.root).as_posix()
    if kind == "tilde":
        return f"~/{target.relative_to(tmp_path).as_posix()}"
    if kind == "tilde_unknown_user":
        return "~nonexistent_user_zz/skills"
    if kind == "symlink_loop":
        loop.unlink(missing_ok=True)
        (tmp_path / "loop-b").unlink(missing_ok=True)
        loop.symlink_to(tmp_path / "loop-b")
        (tmp_path / "loop-b").symlink_to(loop)
        return str(loop)
    if kind == "nul":
        return "bad\0entry"
    if kind == "ancestor_exclusion":
        return f"-{t.cache}"
    if kind == "descendant_exclusion":
        return f"-{below[consumer]}"
    if kind == "glob_ancestor":
        return f"!{t.cache}/*"
    if kind == "glob_target":
        return f"!{target}"
    if kind == "glob_descendant":
        # Matches a file inside the consumer's tree; for commands it matches no file.
        patterns = {"skills": "*.md", "packages": "*.json", "commands": "z*"}
        return f"!{tree}/{patterns[consumer]}"
    if kind == "glob_unrelated":
        return f"!{tmp_path}/nowhere-at-all/*"
    raise AssertionError(kind)


def _expected_bridged(kind: str, consumer: str, truncation: str) -> bool:
    """Whether the consumer's plugin entry should be bridged for this combination."""
    if kind not in _NOT_DECIDING:
        if kind == "descendant_exclusion" and consumer == "commands":
            pass  # a command has no children, so a sibling exclusion leaves it bridged
        elif kind == "glob_descendant" and consumer == "commands":
            pass  # the glob matches a different file than the command
        else:
            return False
    if truncation == "none" or consumer == "packages":
        return True
    # A skill directory is never bridged from an incomplete inspection; a command
    # beyond the file budget is never generated, while the others still are.
    return consumer == "commands" and truncation != "files"


_TABLE_CASES = [
    (kind, consumer, truncation)
    for kind in _ENTRY_KINDS
    for consumer in ("skills", "packages", "commands")
    for truncation in (_TRUNCATIONS if consumer != "packages" else ("none",))
]


@pytest.mark.parametrize(("kind", "consumer", "truncation"), _TABLE_CASES)
def test_user_entry_table(
    kind: str,
    consumer: str,
    truncation: str,
    claude_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each entry kind decides, or does not decide, each consumer the same way everywhere.

    Truncation makes the consumer's tree too large or too deep to inspect completely.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    targets = _install_for(consumer, claude_dir, tmp_path)
    _truncate(consumer, targets, truncation)
    key = _CONSUMER_KEY[consumer]
    entry = _entry(kind, consumer, targets, tmp_path)
    managed = tmp_path / "managed"

    result = add_claude_plugin_resources({key: [entry]}, managed)

    assert result[key][0] == entry, "the user's own entry is kept first"
    added = result[key][1:]
    if consumer == "commands":
        bridged = (managed / PI_CLAUDE_PLUGIN_RESOURCES_DIR / "prompts" / "dp:go.md").exists()
    elif consumer == "skills":
        bridged = str(targets.skill_dir) in added
    else:
        bridged = str(targets.root) in added
    expected = _expected_bridged(kind, consumer, truncation)
    assert bridged == expected, f"{kind} for {consumer} with {truncation}: bridged={bridged}"


@pytest.mark.parametrize("truncation", ["files", "dirs", "depth"])
def test_truncated_skill_inspection_never_bridges_the_directory(
    truncation: str, claude_dir: Path, tmp_path: Path
) -> None:
    """A skill whose tree cannot be fully inspected is skipped; a complete sibling still loads.

    This covers Polly's case: an exclusion for ``SKILL.md`` past the file budget.
    """
    root = _install(claude_dir, "dp@market", files={**_skill("complete"), **_skill("partial")})
    partial = root / "skills" / "partial"
    if truncation == "files":
        for index in range(MAX_PLUGIN_COMMANDS + 8):
            (partial / f"{index:04d}.txt").write_text("x", encoding="utf-8")
    elif truncation == "dirs":
        for index in range(_MAX_WALK_DIRS + 40):
            (partial / f"d{index:03d}").mkdir()
    else:
        deep = partial.joinpath(*[f"l{level}" for level in range(_MAX_WALK_DEPTH + 2)])
        deep.mkdir(parents=True)
        (deep / "x.txt").write_text("x", encoding="utf-8")
    excluded = {"skills": [f"!{partial}/SKILL.md"]}

    result = add_claude_plugin_resources(excluded, tmp_path / "managed")

    added = result["skills"][1:]
    assert str(partial) not in added
    assert str(root / "skills" / "complete") in added


def test_single_directory_stops_at_the_file_budget(tmp_path: Path) -> None:
    """One directory with more files than the budget is cut off inside its own loop."""
    commands = tmp_path / "commands"
    commands.mkdir()
    for index in range(MAX_PLUGIN_COMMANDS + 40):
        (commands / f"c{index:04d}.md").write_text("x", encoding="utf-8")

    found, complete = _bounded_files(commands, lambda name: name.endswith(".md"))

    assert len(found) == MAX_PLUGIN_COMMANDS
    assert complete is False


def test_unknown_home_prefix_never_aborts_preparation(
    claude_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``~user`` entry is skipped with a warning, with or without enabled plugins."""
    settings = {
        "skills": ["~nonexistent_user_zz/skills"],
        "packages": ["~nonexistent_user_zz/pkg"],
        "prompts": ["~nonexistent_user_zz/p"],
    }

    result = add_claude_plugin_resources(settings, tmp_path / "managed")

    assert result == settings
    assert "Ignoring Pi resource path" in caplog.text
