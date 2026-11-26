"""Enabled Claude Code plugins reach managed Pi agent dirs through Pi settings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.harnesses.pi_native.credentials import PiProviderConfig, pi_native_provider_launch
from omnigent.inner import pi_claude_plugins as module
from omnigent.inner.pi_claude_plugins import (
    MAX_PLUGIN_FILES,
    PI_CLAUDE_PLUGIN_RESOURCES_DIR,
    add_claude_plugin_resources,
)
from omnigent.server.feature_flags import FEATURES_ENV_VAR

_GENERATED = PI_CLAUDE_PLUGIN_RESOURCES_DIR
_CACHE_LAYOUTS = ("directory", "symlinked_cache")


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def claude_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Path:
    """A Claude config dir with the pi_claude_plugins feature enabled."""
    config = tmp_path / "claude"
    if getattr(request, "param", "directory") == "symlinked_cache":
        shared = tmp_path / "shared-cache"
        shared.mkdir()
        (config / "plugins").mkdir(parents=True)
        (config / "plugins" / "cache").symlink_to(shared, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv(FEATURES_ENV_VAR, "pi_claude_plugins")
    return config


def _install(claude: Path, key: str, *, files: dict[str, str], enabled: bool = True) -> Path:
    """Install a plugin under the cache and register it as enabled or disabled."""
    name, _, marketplace = key.partition("@")
    root = claude / "plugins" / "cache" / marketplace / name / "1.0.0"
    for relative, content in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(content, encoding="utf-8")
    installed_path = claude / "plugins" / "installed_plugins.json"
    installed = json.loads(installed_path.read_text()) if installed_path.exists() else {}
    installed.setdefault("plugins", {})[key] = [{"installPath": str(root)}]
    _write_json(installed_path, installed)
    _set_enabled(claude, key, enabled)
    return root.resolve()


def _set_enabled(claude: Path, key: str, enabled: bool | None) -> None:
    """Set a plugin's user-level enablement; ``None`` removes the setting."""
    path = claude / "settings.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    enabled_plugins = settings.setdefault("enabledPlugins", {})
    if enabled is None:
        enabled_plugins.pop(key, None)
    else:
        enabled_plugins[key] = enabled
    _write_json(path, settings)


def _skill(name: str) -> dict[str, str]:
    return {f"skills/{name}/SKILL.md": f"---\nname: {name}\ndescription: d\n---\nBody\n"}


def _prompts(managed: Path) -> Path:
    return managed / _GENERATED / "prompts"


def test_flag_off_leaves_settings_untouched(
    claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the feature, no plugin entries and no generated prompts are written."""
    monkeypatch.delenv(FEATURES_ENV_VAR)
    _install(claude_dir, "dp@market", files=_skill("review"))
    managed = tmp_path / "managed"

    assert add_claude_plugin_resources({"skills": ["/user"]}, managed) == {"skills": ["/user"]}
    assert not (managed / _GENERATED).exists()


@pytest.mark.parametrize("claude_dir", _CACHE_LAYOUTS, indirect=True)
@pytest.mark.parametrize("install_path", ["resolved", "relative"])
def test_enabled_plugin_skills_and_namespaced_commands(
    claude_dir: Path, tmp_path: Path, install_path: str
) -> None:
    """Skills are linked one directory each; commands get ``<plugin>:<command>`` names."""
    root = _install(
        claude_dir,
        "dp@market",
        files={
            **_skill("code-review"),
            "commands/review.md": "---\nallowed-tools: Bash(git:*)\n---\n"
            "Focus $ARGUMENTS from ${CLAUDE_PLUGIN_ROOT}\n",
            "commands/sub/deep.md": "Deep $ARGUMENTS\n",
        },
    )
    _write_json(
        claude_dir / "plugins" / "installed_plugins.json",
        {
            "plugins": {
                "dp@market": [
                    {
                        "installPath": str(root)
                        if install_path == "resolved"
                        else "cache/market/dp/1.0.0"
                    }
                ]
            }
        },
    )
    managed = tmp_path / "managed"

    result = add_claude_plugin_resources({}, managed)

    assert result["skills"] == [str(root / "skills" / "code-review")]
    assert sorted(p.name for p in _prompts(managed).iterdir()) == [
        "dp:review.md",
        "dp:sub:deep.md",
    ]
    review = (_prompts(managed) / "dp:review.md").read_text(encoding="utf-8")
    assert f"from {root}" in review
    assert "${CLAUDE_PLUGIN_ROOT}" not in review
    assert "allowed-tools" in review


@pytest.mark.parametrize("claude_dir", _CACHE_LAYOUTS, indirect=True)
def test_only_enabled_plugins_load(
    claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled plugins stay out; managed policy enables one with no user setting; install
    records alone, and project-level ``enabledPlugins``, do not enable a plugin."""
    _install(claude_dir, "off@market", files=_skill("off"), enabled=False)
    _install(claude_dir, "on@market", files=_skill("on"))
    _install(claude_dir, "managed@market", files=_skill("managed"))
    _write_json(
        claude_dir / "plugins" / "managed_plugins.json", {"managed_plugins": ["managed@market"]}
    )
    _set_enabled(claude_dir, "managed@market", None)
    _install(claude_dir, "unenabled@market", files=_skill("unenabled"))
    _set_enabled(claude_dir, "unenabled@market", None)
    project = tmp_path / "project"
    _write_json(project / ".claude" / "settings.json", {"enabledPlugins": {"p@market": True}})
    _install(claude_dir, "p@market", files=_skill("project"))
    _set_enabled(claude_dir, "p@market", None)
    monkeypatch.chdir(project)

    skills = add_claude_plugin_resources({}, tmp_path / "m")["skills"]
    names = {Path(p).parents[2].name for p in skills}

    assert names == {"on", "managed"}


@pytest.mark.parametrize("claude_dir", _CACHE_LAYOUTS, indirect=True)
def test_pi_manifest_plugin_loads_as_package_and_keeps_commands(
    claude_dir: Path, tmp_path: Path
) -> None:
    """A plugin declaring a ``pi`` manifest loads as a Pi package, not a skills bridge."""
    root = _install(
        claude_dir,
        "ext@market",
        files={
            "package.json": json.dumps({"name": "ext", "pi": {"extensions": ["./x.js"]}}),
            **_skill("unused"),
            "commands/go.md": "Go $ARGUMENTS\n",
        },
    )
    managed = tmp_path / "managed"

    result = add_claude_plugin_resources({}, managed)

    assert result["packages"] == [str(root)]
    assert "skills" not in result
    assert (_prompts(managed) / "ext:go.md").exists()


@pytest.mark.parametrize(
    ("listed", "claude_dir"),
    [
        pytest.param(lambda root: {"source": str(root), "skills": []}, "directory", id="object"),
        pytest.param(str, "directory", id="string"),
        pytest.param(str, "symlinked_cache", id="cache-alias"),
    ],
    indirect=["claude_dir"],
)
def test_user_package_entry_claims_the_plugin_skills(
    claude_dir: Path, tmp_path: Path, listed
) -> None:
    """A package the user lists for a plugin brings its skills, so they are not added again."""
    _install(claude_dir, "dp@market", files=_skill("review"))
    root = claude_dir / "plugins" / "cache" / "market" / "dp" / "1.0.0"

    result = add_claude_plugin_resources({"packages": [listed(root)]}, tmp_path / "managed")

    assert "skills" not in result
    assert result["packages"] == [listed(root)]


@pytest.mark.parametrize("claude_dir", _CACHE_LAYOUTS, indirect=True)
def test_user_entries_claim_by_exact_resolved_path(claude_dir: Path, tmp_path: Path) -> None:
    """A skill directory, a command file, or a commands directory the user lists is not added."""
    _install(
        claude_dir,
        "dp@market",
        files={**_skill("review"), "commands/go.md": "GO\n", "commands/keep.md": "KEEP\n"},
    )
    root = claude_dir / "plugins" / "cache" / "market" / "dp" / "1.0.0"
    managed = tmp_path / "managed"
    settings = {
        "skills": [str(root / "skills" / "review")],
        "prompts": [str(root / "commands" / "go.md")],
    }

    result = add_claude_plugin_resources(settings, managed)

    assert result["skills"] == settings["skills"]
    assert [p.name for p in _prompts(managed).iterdir()] == ["dp:keep.md"]


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
    managed = tmp_path / "managed"

    add_claude_plugin_resources({}, managed)

    assert (_prompts(managed) / "dp:go.md").read_text(encoding="utf-8") == "FIRST\n"


def test_relaunch_drops_commands_of_removed_plugins(claude_dir: Path, tmp_path: Path) -> None:
    """Each launch regenerates the command dir, so stale prompts do not survive."""
    _install(claude_dir, "dp@market", files={"commands/go.md": "Go\n"})
    managed = tmp_path / "managed"
    add_claude_plugin_resources({}, managed)
    _install(claude_dir, "dp@market", files={}, enabled=False)

    result = add_claude_plugin_resources({}, managed)

    assert "prompts" not in result
    assert not (managed / _GENERATED).exists()


@pytest.mark.parametrize("claude_dir", _CACHE_LAYOUTS, indirect=True)
def test_symlinks_are_never_bridged(claude_dir: Path, tmp_path: Path) -> None:
    """Symlinked command files, commands/ roots and skill dirs pointing elsewhere are skipped."""
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET_OUTSIDE_PLUGIN", encoding="utf-8")
    root = _install(claude_dir, "dp@market", files={"commands/ok.md": "OK\n", **_skill("real")})
    (root / "commands" / "leak.md").symlink_to(secret)
    (root / "skills" / "linked").symlink_to(root / "skills" / "real")
    managed = tmp_path / "managed"

    result = add_claude_plugin_resources({}, managed)

    assert [p.name for p in _prompts(managed).iterdir()] == ["dp:ok.md"]
    assert result["skills"] == [str(root / "skills" / "real")]

    outside = tmp_path / "outside-commands"
    outside.mkdir()
    (outside / "x.md").write_text("SECRET_OUTSIDE_PLUGIN", encoding="utf-8")
    other = _install(claude_dir, "linked@market", files={"placeholder": ""})
    (other / "commands").symlink_to(outside, target_is_directory=True)

    add_claude_plugin_resources({}, managed)

    assert not (_prompts(managed) / "linked:x.md").exists()


@pytest.mark.parametrize("name", ["bad name", "../escape"])
def test_unsafe_plugin_names_are_not_bridged(claude_dir: Path, tmp_path: Path, name: str) -> None:
    """A plugin name becomes a filename, so names with separators or spaces are skipped."""
    _install(claude_dir, f"{name}@market", files={"commands/go.md": "X\n", **_skill("x")})

    assert add_claude_plugin_resources({}, tmp_path / "managed") == {}


def test_overlong_generated_command_name_is_skipped(claude_dir: Path, tmp_path: Path) -> None:
    """An over-long generated command name is skipped without failing preparation."""
    long_part = "a" * 128
    _install(
        claude_dir,
        "demo@market",
        files={f"commands/{long_part}/{long_part}.md": "LONG\n", "commands/ok.md": "OK\n"},
    )
    managed = tmp_path / "managed"

    add_claude_plugin_resources({}, managed)

    assert [p.name for p in _prompts(managed).iterdir()] == ["demo:ok.md"]


def test_unresolvable_user_entries_do_not_abort_preparation(
    claude_dir: Path, tmp_path: Path
) -> None:
    """Symlink loops, NUL bytes and unknown ``~user`` entries leave the other entries working."""
    root = _install(claude_dir, "dp@market", files=_skill("review"))
    loop_a, loop_b = tmp_path / "loop-a", tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    settings = {
        "skills": [str(loop_a), "bad\0path", "~nonexistent_user_zz/skills"],
        "packages": [str(loop_a), "bad\0package"],
    }

    result = add_claude_plugin_resources(settings, tmp_path / "managed")

    assert result["skills"] == [*settings["skills"], str(root / "skills" / "review")]
    assert result["packages"] == settings["packages"]


def test_no_plugins_leaves_settings_unchanged(claude_dir: Path, tmp_path: Path) -> None:
    """With no plugins enabled, unusable user entries come back exactly as written."""
    settings = {
        "skills": ["~nonexistent_user_zz/skills"],
        "prompts": ["bad\0prompt"],
        "packages": ["/does/not/matter", "~nonexistent_user_zz/pkg"],
    }
    assert add_claude_plugin_resources(settings, tmp_path / "managed") == settings


_KINDS = ("absolute", "tilde", "relative", "prefixed", "tilde_unknown_user", "symlink_loop", "nul")
_LISTED = {"absolute", "tilde"}
_UNRESOLVABLE = {"tilde_unknown_user", "symlink_loop", "nul"}


def _loop_fails_to_resolve(tmp_path: Path) -> bool:
    """Whether this Python raises when resolving the loop; 3.13+ may resolve it instead."""
    try:
        Path(_loop(tmp_path)).resolve()
    except (OSError, RuntimeError):
        return True
    return False


_CONSUMERS = {"skills": "skills", "packages": "packages", "commands": "prompts"}


@pytest.mark.parametrize(("kind", "consumer"), [(k, c) for k in _KINDS for c in _CONSUMERS])
def test_user_entry_table(
    kind: str,
    consumer: str,
    claude_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A user entry claims a plugin resource only when it lists that resource by path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    files = {
        "skills": _skill("review"),
        "packages": {"package.json": json.dumps({"pi": {}}), **_skill("review")},
        "commands": {"commands/go.md": "GO\n"},
    }[consumer]
    root = _install(claude_dir, "dp@market", files=files)
    target = {
        "skills": root / "skills" / "review",
        "packages": root,
        "commands": root / "commands" / "go.md",
    }[consumer]
    entry = {
        "absolute": str(target),
        "tilde": f"~/{target.relative_to(tmp_path).as_posix()}",
        "relative": target.relative_to(root).as_posix(),
        # Prefixed entries are not paths and claim nothing; excluding one resource is deferred.
        "prefixed": f"-{target}",
        "tilde_unknown_user": "~nonexistent_user_zz/skills",
        "nul": "bad\0entry",
        "symlink_loop": _loop(tmp_path),
    }[kind]
    key = _CONSUMERS[consumer]
    managed = tmp_path / "managed"

    result = add_claude_plugin_resources({key: [entry]}, managed)

    assert result[key][0] == entry
    added = result[key][1:]
    if consumer == "commands":
        bridged = (_prompts(managed) / "dp:go.md").exists()
    elif consumer == "skills":
        bridged = str(target) in added
    else:
        bridged = str(root) in added
    assert bridged == (kind not in _LISTED), f"{kind} for {consumer}"
    if kind in _UNRESOLVABLE and (kind != "symlink_loop" or _loop_fails_to_resolve(tmp_path)):
        assert "Ignoring Pi resource path" in caplog.text


def _loop(tmp_path: Path) -> str:
    loop_a, loop_b = tmp_path / "loop-a", tmp_path / "loop-b"
    for link in (loop_a, loop_b):
        link.unlink(missing_ok=True)
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    return str(loop_a)


@pytest.mark.parametrize("claude_dir", _CACHE_LAYOUTS, indirect=True)
def test_native_provider_launch_writes_plugin_entries(claude_dir: Path, tmp_path: Path) -> None:
    """The ``omnigent pi`` terminal launch seeds the same managed settings as the gateway."""
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


def test_entries_read_per_directory_are_bounded_before_sorting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory listing is streamed: no more entries are read than the per-directory cap."""
    crowded = tmp_path / "crowded"
    crowded.mkdir()
    for index in range(module._MAX_ENTRIES_PER_DIR + 200):
        (crowded / f"f{index:05d}.txt").write_text("x", encoding="utf-8")
    read = [0]
    real_scandir = os.scandir

    class Counted:
        def __init__(self, path: Path) -> None:
            self.scan = real_scandir(path)

        def __enter__(self) -> Counted:
            return self

        def __exit__(self, *exc: object) -> None:
            self.scan.close()

        def __iter__(self):  # type: ignore[no-untyped-def]
            for entry in self.scan:
                read[0] += 1
                yield entry

    monkeypatch.setattr(os, "scandir", Counted)

    assert module._walk(crowded, lambda n: n.endswith(".md"), MAX_PLUGIN_FILES) == []
    assert read[0] <= module._MAX_ENTRIES_PER_DIR


def test_file_budget_caps_the_matches(tmp_path: Path) -> None:
    """A walk returns at most the file limit."""
    many = tmp_path / "many"
    many.mkdir()
    for index in range(MAX_PLUGIN_FILES + 40):
        (many / f"c{index:04d}.md").write_text("x", encoding="utf-8")

    found = module._walk(many, lambda n: n.endswith(".md"), MAX_PLUGIN_FILES)

    assert len(found) == MAX_PLUGIN_FILES


def test_directory_budget_stops_descending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a directory budget of three, only the root and two child directories are read."""
    monkeypatch.setattr(module, "_MAX_WALK_DIRS", 3)
    tree = tmp_path / "tree"
    for index in range(5):
        (tree / f"d{index}").mkdir(parents=True)
        (tree / f"d{index}" / "leaf.md").write_text("x", encoding="utf-8")

    found = module._walk(tree, lambda n: n.endswith(".md"), MAX_PLUGIN_FILES)

    assert len(found) == 2


def test_depth_budget_stops_descending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a depth limit of two, a file two levels down is read; one three levels down is not."""
    monkeypatch.setattr(module, "_MAX_WALK_DEPTH", 2)
    tree = tmp_path / "tree"
    (tree / "a" / "b" / "c").mkdir(parents=True)
    (tree / "a" / "b" / "inside.md").write_text("x", encoding="utf-8")
    (tree / "a" / "b" / "c" / "outside.md").write_text("x", encoding="utf-8")

    names = {
        file.name for _, file in module._walk(tree, lambda n: n.endswith(".md"), MAX_PLUGIN_FILES)
    }

    assert names == {"inside.md"}


def test_frontmatter_name_renames_a_plugin_command(claude_dir: Path, tmp_path: Path) -> None:
    """A command's frontmatter ``name`` replaces its filename, as plugin command discovery does."""
    _install(
        claude_dir,
        "dp@market",
        files={"commands/old.md": "---\nname: new\n---\nBody $ARGUMENTS\n"},
    )
    managed = tmp_path / "managed"

    add_claude_plugin_resources({}, managed)

    assert [p.name for p in _prompts(managed).iterdir()] == ["dp:new.md"]


def _make_unusable(path: Path, kind: str, limit: int, outside: Path) -> None:
    """Replace *path* with a special, symlinked, oversized, or directory entry; never read it."""
    if path.is_dir() and not path.is_symlink():
        path.rmdir() if not any(path.iterdir()) else None
    path.unlink(missing_ok=True)
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(outside)
    elif kind == "oversize":
        path.write_bytes(b" " * (limit + 1))
    else:
        path.mkdir()


_UNUSABLE_KINDS = ("fifo", "symlink", "oversize", "directory")


@pytest.mark.timeout(20)
@pytest.mark.parametrize("kind", _UNUSABLE_KINDS)
@pytest.mark.parametrize("consumer", ["package", "installed", "command", "skill"])
def test_unusable_metadata_is_skipped_without_blocking(
    consumer: str,
    kind: str,
    claude_dir: Path,
    tmp_path: Path,
) -> None:
    """A FIFO, symlink, oversized file, or directory where metadata belongs never blocks or loads.

    The hard timeout turns a regression that blocks on a FIFO into a failure. The tests do
    not read the special file; only the bridge's bounded reader is allowed to open it.
    """
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs need POSIX")
    outside = tmp_path / "outside.txt"
    if consumer == "package":
        root = _install(claude_dir, "dp@market", files={**_skill("review"), "package.json": "{}"})
        bad = root / "package.json"
        limit = module.MAX_MANIFEST_BYTES
        outside.write_text('{"pi": {}}', encoding="utf-8")
        _make_unusable(bad, kind, limit, outside)
        result = add_claude_plugin_resources({}, tmp_path / "managed")
        # Unusable metadata means a plain plugin: its skill loads, and no package does.
        assert str(root / "skills" / "review") in result["skills"]
        assert "packages" not in result
        return
    if consumer == "installed":
        root = _install(claude_dir, "dp@market", files=_skill("review"))
        bad = claude_dir / "plugins" / "installed_plugins.json"
        valid = bad.read_bytes()
        limit = module.MAX_MANIFEST_BYTES
        outside.write_bytes(valid)
        _make_unusable(bad, kind, limit, outside)
        result = add_claude_plugin_resources({}, tmp_path / "managed")
        # A symlink to the user's own valid config is followed; the others load nothing.
        loaded = str(root / "skills" / "review") in result.get("skills", [])
        assert loaded is (kind == "symlink")
        return
    if consumer == "command":
        root = _install(
            claude_dir, "dp@market", files={"commands/ok.md": "OK\n", "commands/x.md": "X\n"}
        )
        bad = root / "commands" / "x.md"
        outside.write_text("SECRET", encoding="utf-8")
        _make_unusable(bad, kind, module.MAX_COMMAND_BYTES, outside)
        managed = tmp_path / "managed"
        add_claude_plugin_resources({}, managed)
        names = {p.name for p in _prompts(managed).iterdir()}
        assert "dp:ok.md" in names
        assert "dp:x.md" not in names
        return
    root = _install(claude_dir, "dp@market", files={**_skill("complete"), **_skill("bad")})
    bad = root / "skills" / "bad" / "SKILL.md"
    outside.write_text("---\nname: bad\ndescription: d\n---\n", encoding="utf-8")
    _make_unusable(bad, kind, module.MAX_SKILL_BYTES, outside)
    result = add_claude_plugin_resources({}, tmp_path / "managed")
    assert str(root / "skills" / "complete") in result["skills"]
    assert str(root / "skills" / "bad") not in result["skills"]


@pytest.mark.timeout(20)
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs need POSIX")
def test_fifo_settings_file_skips_the_whole_bridge(claude_dir: Path, tmp_path: Path) -> None:
    """A FIFO in the user's own settings would block the shared readers, so nothing is bridged."""
    _install(claude_dir, "dp@market", files=_skill("review"))
    settings_path = claude_dir / "settings.json"
    settings_path.unlink()
    os.mkfifo(settings_path)

    assert add_claude_plugin_resources({}, tmp_path / "managed") == {}


@pytest.mark.parametrize("claude_dir", _CACHE_LAYOUTS, indirect=True)
def test_project_scoped_installs_are_not_loaded(claude_dir: Path, tmp_path: Path) -> None:
    """Only a trusted user install loads; unusable entries do not hide a later valid one."""
    user = _install(claude_dir, "dp@market", files=_skill("user"))
    outside = _install(claude_dir, "outside@market", files=_skill("outside"))
    outside = outside.rename(tmp_path / "outside")
    linked = user.parent / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    other = claude_dir / "plugins" / "cache" / "market" / "dp" / "0.9.0"
    for relative, content in _skill("other").items():
        (other / relative).parent.mkdir(parents=True, exist_ok=True)
        (other / relative).write_text(content, encoding="utf-8")
    entries = [
        {"scope": "project", "projectPath": "/elsewhere", "installPath": str(other)},
        {"installPath": str(outside)},
        {"installPath": str(linked)},
    ]
    installed = claude_dir / "plugins" / "installed_plugins.json"
    _write_json(installed, {"plugins": {"dp@market": entries}})
    assert add_claude_plugin_resources({}, tmp_path / "managed") == {}
    entries.append({"scope": "user", "installPath": str(user)})
    _write_json(installed, {"plugins": {"dp@market": entries}})

    result = add_claude_plugin_resources({}, tmp_path / "managed")

    assert result["skills"] == [str(user / "skills" / "user")]


@pytest.mark.parametrize("claude_dir", ["symlinked_cache"], indirect=True)
@pytest.mark.parametrize("unusable", ["missing", "file", "stat-error"])
def test_unusable_shared_install_does_not_hide_user_fallback(
    claude_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unusable: str,
) -> None:
    """An unavailable shared installation cannot hide a healthy non-cache user install."""
    stale = _install(claude_dir, "dp@market", files=_skill("review"))
    fallback = claude_dir / "plugins" / "local" / "dp"
    fallback.parent.mkdir()
    stale.rename(fallback)
    if unusable == "file":
        stale.touch()
    elif unusable == "stat-error":
        original_is_dir = Path.is_dir

        def is_dir(path: Path) -> bool:
            if path == stale:
                raise PermissionError("installation is unavailable")
            return original_is_dir(path)

        monkeypatch.setattr(Path, "is_dir", is_dir)
    _write_json(
        claude_dir / "plugins" / "installed_plugins.json",
        {"plugins": {"dp@market": [{"installPath": str(stale)}, {"installPath": str(fallback)}]}},
    )

    result = add_claude_plugin_resources({}, tmp_path / "managed")

    assert result["skills"] == [str(fallback / "skills" / "review")]
