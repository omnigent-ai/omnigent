from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from omnigent.spec.skill_sources import (
    SkillSourceContext,
    _harness_family,
    resolve_harness_skills,
)


def _write_skill(skills_dir: Path, name: str, *, user_invocable: bool | None = None) -> None:
    """
    Write a minimal ``<skills_dir>/<name>/SKILL.md`` with valid frontmatter.

    :param user_invocable: When not ``None``, emit a ``user-invocable:``
        frontmatter line with this value. Omitted (the default) leaves the
        field absent, which parses as user-invocable.
    """
    d = skills_dir / name
    d.mkdir(parents=True)
    ui = "" if user_invocable is None else f"user-invocable: {str(user_invocable).lower()}\n"
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} desc\n{ui}---\nbody\n")


def _ctx(
    root: Path,
    home: Path,
    skills_filter: str | list[str] = "all",
    claude_config_dir: Path | None = None,
    codex_home: Path | None = None,
) -> SkillSourceContext:
    """Build a context with a single discovery root and a pinned home."""
    return SkillSourceContext(
        roots=(root,),
        home=home,
        skills_filter=skills_filter,
        bundle_dir=None,
        claude_config_dir=claude_config_dir,
        codex_home=codex_home,
    )


@pytest.mark.parametrize(
    "harness,expected",
    [
        ("claude-sdk", "claude"),
        ("claude_sdk", "claude"),  # in-process SDK executor-type spelling (B1)
        ("claude-native", "claude"),
        ("native-claude", "claude"),
        ("agents_sdk", None),  # underscore non-claude executor type stays unmapped
        ("codex", "codex"),
        ("codex-native", "codex"),
        ("native-codex", "codex"),
        ("cursor", "cursor"),
        ("cursor-native", "cursor"),
        ("pi", "pi"),
        ("pi-native", "pi"),
        ("native-pi", "pi"),
        ("openai-agents", None),
        # Only the agy CLI reads ~/.gemini plugins; the in-process Gemini SDK
        # harness (bare "antigravity") does not, so it stays unmapped.
        ("antigravity", None),
        ("antigravity-native", "antigravity"),
        ("native-antigravity", "antigravity"),
        ("devin", "devin"),
        ("devin-native", "devin"),
        ("native-devin", "devin"),
        ("qwen", None),
        (None, None),
        ("", None),
    ],
)
def test_harness_family(harness: str | None, expected: str | None) -> None:
    assert _harness_family(harness) == expected


def test_devin_provider_lists_the_cli_reported_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Devin reads skills from several of its own dirs plus .claude/skills with
    # its own precedence, so the menu is sourced from ``devin skills list``
    # (authoritative) rather than a dir walk that would only approximate it.
    payload = json.dumps(
        [
            {
                "name": "code-review",
                "description": "review",
                "base_dir": "/w/.claude/skills/code-review",
                "triggers": ["user"],
            },
            {"name": "deploy", "description": "ship", "base_dir": "/w/.devin/skills/deploy"},
        ]
    )

    class _Result:
        returncode = 0
        stdout = payload
        stderr = ""

    calls: list[dict[str, object]] = []

    def _run(cmd: list[str], **kwargs: object) -> _Result:
        calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        return _Result()

    monkeypatch.setattr("omnigent.spec.skill_sources.subprocess.run", _run)
    out = resolve_harness_skills(_ctx(tmp_path, tmp_path), "devin-native")
    assert {s.name for s in out} == {"code-review", "deploy"}
    review = next(s for s in out if s.name == "code-review")
    assert review.description == "review"
    assert review.skill_dir == Path("/w/.claude/skills/code-review")
    # Sourced from the CLI, in the session's workspace.
    assert calls[0]["cmd"][:3] == ["devin", "skills", "list"]
    assert calls[0]["cwd"] == str(tmp_path)


def test_devin_provider_fails_soft_when_cli_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing/broken devin CLI must not break the menu — the bundled skills
    # (added by the caller) still show; the host list is just empty.
    def _run(cmd: list[str], **kwargs: object) -> object:
        raise FileNotFoundError("devin not on PATH")

    monkeypatch.setattr("omnigent.spec.skill_sources.subprocess.run", _run)
    assert resolve_harness_skills(_ctx(tmp_path, tmp_path), "devin-native") == []


def test_unknown_harness_falls_back_to_generic_host_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".claude" / "skills", "ws-skill")

    out = resolve_harness_skills(_ctx(workspace, home), "openai-agents")
    assert [s.name for s in out] == ["ws-skill"]


def test_none_harness_falls_back_to_generic_host_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".claude" / "skills", "ws-skill")

    out = resolve_harness_skills(_ctx(workspace, home), None)
    assert [s.name for s in out] == ["ws-skill"]


@pytest.mark.parametrize(
    "skills_filter,expected",
    [
        ("all", {"native", "shared", "ancestor", "home"}),
        ("none", set()),
        (["ancestor", "native", "hidden", "missing"], {"ancestor", "native"}),
    ],
)
def test_claude_provider_includes_bridged_agents_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_filter: str | list[str],
    expected: set[str],
) -> None:
    """Portable skills keep nearest-root precedence and yield to native skills."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    ancestor = tmp_path / "project"
    workspace = ancestor / "ws"
    for root, names in (
        (workspace, ("shared", "native")),
        (ancestor, ("shared", "ancestor")),
        (home, ("shared", "ancestor", "home")),
    ):
        for name in names:
            _write_skill(root / ".agents" / "skills", name)
    _write_skill(ancestor / ".claude" / "skills", "native")
    _write_skill(workspace / ".agents" / "skills", "hidden", user_invocable=False)
    selected = {
        "native": ancestor / ".claude" / "skills" / "native",
        "shared": workspace / ".agents" / "skills" / "shared",
        "ancestor": ancestor / ".agents" / "skills" / "ancestor",
        "home": home / ".agents" / "skills" / "home",
    }

    out = resolve_harness_skills(_ctx(workspace, home, skills_filter), "claude-native")
    assert {s.name: s.skill_dir for s in out} == {name: selected[name] for name in expected}


def test_claude_portable_skill_names_are_safe_command_basenames(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    # Unsafe frontmatter names stay labels; only a valid directory name becomes the command.
    for directory, name in (
        ("portable", "nested/name"),
        ("traversal", ".."),
        ("absolute", "/absolute"),
        ("display label", "portable"),
        ("dotted.name", "portable"),
    ):
        skill = workspace / ".agents" / "skills" / directory / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {json.dumps(name)}\ndescription: Test skill\n---\nBody.\n")

    out = resolve_harness_skills(_ctx(workspace, tmp_path / "home"), "claude-native")
    assert sorted(skill.name for skill in out) == ["absolute", "portable", "traversal"]


def test_claude_provider_invokes_skills_by_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code types a skill as ``/<dir>``; the frontmatter name is only its label.

    A spaced frontmatter name used as the command would fail the slash-command
    shape check and drop the skill from the menu entirely.
    """
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    skill = home / ".claude" / "skills" / "asd-ste100"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: Simplified Technical English (ASD-STE100)\ndescription: STE\n---\nbody\n"
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()

    out = resolve_harness_skills(_ctx(workspace, home), "claude-native")
    assert [(s.name, s.display_name) for s in out] == [
        ("asd-ste100", "Simplified Technical English (ASD-STE100)")
    ]


@pytest.mark.parametrize("harness", ["claude-native", "claude-sdk"])
@pytest.mark.parametrize("configured", ["review", "code-review"])
def test_claude_provider_filter_accepts_directory_or_frontmatter_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, configured: str
) -> None:
    """A ``skills:`` list naming the frontmatter ``name`` still selects the skill."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    skill = home / ".claude" / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: code-review\ndescription: Review\n---\nbody\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    out = resolve_harness_skills(_ctx(workspace, home, skills_filter=[configured]), harness)
    assert [(s.name, s.display_name) for s in out] == [("review", "code-review")]


def test_claude_provider_sources_user_skills_from_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured Claude config dir replaces ``~/.claude`` as the user tier.

    Claude Code loads user skills from ``$CLAUDE_CONFIG_DIR/skills`` when
    set — and then no longer reads ``~/.claude/skills`` — so the menu must
    follow the same tier or the surfaces diverge in both directions.
    """
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    _write_skill(home / ".claude" / "skills", "default-home-skill")
    cfg = tmp_path / "claude-config"
    _write_skill(cfg / "skills", "config-dir-skill")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    out = resolve_harness_skills(_ctx(workspace, home, claude_config_dir=cfg), "claude-native")
    assert [s.name for s in out] == ["config-dir-skill"]


def test_claude_sdk_keeps_generic_walk_native_matches_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both modes include portable skills; only native honors the config-dir tier."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    (home / ".claude" / "skills").mkdir(parents=True)  # empty default user tier
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".claude" / "skills", "claude-dir-skill")
    _write_skill(workspace / ".agents" / "skills", "agents-only-skill")
    cfg = tmp_path / "claude-config"
    _write_skill(cfg / "skills", "user-cfg-skill")
    ctx = _ctx(workspace, home, claude_config_dir=cfg)

    sdk = {s.name for s in resolve_harness_skills(ctx, "claude-sdk")}
    native = {s.name for s in resolve_harness_skills(ctx, "claude-native")}

    # SDK (unchanged): generic walk lists the .agents entry, ignores config-dir.
    assert "agents-only-skill" in sdk
    assert "claude-dir-skill" in sdk
    assert "user-cfg-skill" not in sdk
    assert native == {"claude-dir-skill", "agents-only-skill", "user-cfg-skill"}


def test_codex_native_and_sdk_agree_without_a_configured_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a configured ``$CODEX_HOME`` both codex harnesses read ``~/.codex``.

    Both Codex harnesses include ``~/.agents/skills``. Absent a resolved
    ``$CODEX_HOME`` (``ctx.codex_home is None``), the native provider falls back
    to the same ``~/.codex/skills`` the SDK path uses — so the two agree until a
    custom codex home is in play (see the divergence test below).
    """
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    _write_skill(home / ".codex" / "skills", "codex-host-skill")
    _write_skill(home / ".agents" / "skills", "agents-only-skill")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = _ctx(workspace, home)

    native = {s.name for s in resolve_harness_skills(ctx, "codex-native")}
    sdk = {s.name for s in resolve_harness_skills(ctx, "codex")}
    assert native == sdk == {"codex-host-skill", "agents-only-skill"}


def test_codex_native_honors_codex_home_sdk_keeps_home_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native codex sources host skills from ``$CODEX_HOME``; SDK keeps ``~/.codex``.

    The codex analog of the ``$CLAUDE_CONFIG_DIR`` facet: codex-native honors
    ``$CODEX_HOME`` (its launch seeds the per-bridge home from that resolved
    home), so the menu must read it too. The in-process ``codex`` (SDK) harness
    has no such terminal, so it stays on ``~/.codex`` — the same seeded tree
    must diverge by harness.
    """
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    _write_skill(home / ".codex" / "skills", "default-codex-skill")
    _write_skill(home / ".agents" / "skills", "shared-skill")
    custom = tmp_path / "custom-codex-home"
    _write_skill(custom / "skills", "custom-codex-skill")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = _ctx(workspace, home, codex_home=custom)

    native = {s.name for s in resolve_harness_skills(ctx, "codex-native")}
    sdk = {s.name for s in resolve_harness_skills(ctx, "codex")}
    # Native reads $CODEX_HOME's skills; SDK ignores codex_home and reads ~/.codex.
    assert native == {"custom-codex-skill", "shared-skill"}
    assert sdk == {"default-codex-skill", "shared-skill"}


def test_claude_provider_defaults_user_tier_to_home_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a configured config dir the user tier stays ``~/.claude/skills``."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    _write_skill(home / ".claude" / "skills", "default-home-skill")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    out = resolve_harness_skills(_ctx(workspace, home), "claude-native")
    assert [s.name for s in out] == ["default-home-skill"]


def test_claude_provider_workspace_skill_wins_user_tier_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace ``.claude/skills`` name shadows the user tier's (project wins)."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace = tmp_path / "ws"
    ws_dir = workspace / ".claude" / "skills" / "shared-name"
    ws_dir.mkdir(parents=True)
    (ws_dir / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: workspace copy\n---\nbody\n"
    )
    home_dir = home / ".claude" / "skills" / "shared-name"
    home_dir.mkdir(parents=True)
    (home_dir / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: user copy\n---\nbody\n"
    )

    out = resolve_harness_skills(_ctx(workspace, home), "claude-native")
    assert [(s.name, s.description) for s in out] == [("shared-name", "workspace copy")]


def test_claude_plugins_read_from_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugin settings/manifests follow the configured Claude config dir.

    With ``$CLAUDE_CONFIG_DIR`` set, Claude Code keeps ``settings.json``
    and ``plugins/`` under that dir, so plugin slash-commands must be
    resolved from there rather than ``~/.claude``.
    """
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = tmp_path / "claude-config"
    install = cfg / "plugins" / "cache" / "mkt" / "toolkit" / "1.0.0"
    _write_skill(install / "skills", "review")
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps({"enabledPlugins": {"toolkit@mkt": True}}))
    (cfg / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {"toolkit@mkt": [{"scope": "user", "installPath": str(install)}]},
            }
        )
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()

    out = resolve_harness_skills(_ctx(workspace, home, claude_config_dir=cfg), "claude-native")
    assert [s.name for s in out] == ["toolkit:review"]


def _claude_home_with_plugin(
    home: Path, *, plugin: str, marketplace: str, skill: str, enabled: bool
) -> Path:
    """Seed a fake ~/.claude with one installed plugin and an enablement flag."""
    install = home / ".claude" / "plugins" / "cache" / marketplace / plugin / "1.0.0"
    _write_skill(install / "skills", skill)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {f"{plugin}@{marketplace}": enabled}})
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    f"{plugin}@{marketplace}": [
                        {"scope": "user", "installPath": str(install), "version": "1.0.0"}
                    ]
                },
            }
        )
    )
    return home


def test_claude_provider_surfaces_enabled_plugin_skill_namespaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _claude_home_with_plugin(
        tmp_path / "home",
        plugin="superpowers",
        marketplace="claude-plugins-official",
        skill="using-superpowers",
        enabled=True,
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
    assert "superpowers:using-superpowers" in [s.name for s in out]


def test_claude_sdk_underscore_harness_surfaces_plugin_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    B1 regression: the in-process Claude SDK harness flows in as the
    underscore executor-type spelling ``claude_sdk``; it must still
    resolve to the claude family and surface enabled-plugin skills.
    """
    home = _claude_home_with_plugin(
        tmp_path / "home",
        plugin="superpowers",
        marketplace="mkt",
        skill="using-superpowers",
        enabled=True,
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude_sdk")
    assert "superpowers:using-superpowers" in [s.name for s in out]


def test_claude_provider_excludes_disabled_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _claude_home_with_plugin(
        tmp_path / "home",
        plugin="superpowers",
        marketplace="claude-plugins-official",
        skill="using-superpowers",
        enabled=False,
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-sdk")
    assert "superpowers:using-superpowers" not in [s.name for s in out]


def test_claude_managed_plugin_surfaces_when_absent_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A plugin force-enabled only via ``managed_plugins.json`` — with no
    ``enabledPlugins`` entry in any settings file — is still surfaced.
    Claude Code's managed (policy) tier enables it regardless of settings.
    """
    home = tmp_path / "home"
    install = home / ".claude" / "plugins" / "cache" / "mkt" / "secrev" / "1.0.0"
    _write_skill(install / "skills", "do-review")
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {}}))
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "secrev@mkt": [
                        {"scope": "user", "installPath": str(install), "version": "1.0.0"}
                    ]
                },
            }
        )
    )
    (home / ".claude" / "plugins" / "managed_plugins.json").write_text(
        json.dumps({"managed_plugins": ["secrev@mkt"]})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-sdk")
    assert "secrev:do-review" in [s.name for s in out]


def test_claude_managed_plugin_overrides_settings_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``managed_plugins.json`` force-enables even when ``enabledPlugins``
    explicitly disables the same plugin: the managed tier is highest
    precedence in Claude Code and cannot be overridden by a settings toggle.
    """
    home = _claude_home_with_plugin(
        tmp_path / "home",
        plugin="secrev",
        marketplace="mkt",
        skill="do-review",
        enabled=False,
    )
    (home / ".claude" / "plugins" / "managed_plugins.json").write_text(
        json.dumps({"managed_plugins": ["secrev@mkt"]})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-sdk")
    assert "secrev:do-review" in [s.name for s in out]


def test_claude_provider_tolerates_missing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
    assert out == []  # no plugins, empty host walk → no crash


def test_claude_provider_tolerates_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{not json")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
    assert out == []


def test_claude_provider_none_filter_suppresses_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``skills_filter="none"`` is hermetic: no plugin skills leak in."""
    home = _claude_home_with_plugin(
        tmp_path / "home",
        plugin="superpowers",
        marketplace="claude-plugins-official",
        skill="using-superpowers",
        enabled=True,
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    ctx = SkillSourceContext(
        roots=(tmp_path / "ws",), home=home, skills_filter="none", bundle_dir=None
    )
    assert resolve_harness_skills(ctx, "claude-native") == []


def test_claude_provider_list_filter_selects_by_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list filter selects plugin skills by their bare (un-namespaced) name."""
    home = tmp_path / "home"
    install = home / ".claude" / "plugins" / "cache" / "mkt" / "superpowers" / "1.0.0"
    _write_skill(install / "skills", "using-superpowers")
    _write_skill(install / "skills", "writing-plans")
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"superpowers@mkt": True}})
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"superpowers@mkt": [{"installPath": str(install)}]}})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    ctx = SkillSourceContext(
        roots=(tmp_path / "ws",),
        home=home,
        skills_filter=["writing-plans"],
        bundle_dir=None,
    )
    names = [s.name for s in resolve_harness_skills(ctx, "claude-native")]
    assert names == ["superpowers:writing-plans"]


def test_codex_provider_surfaces_home_codex_skills(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_skill(home / ".codex" / "skills", "using-superpowers")
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "codex-native")
    assert "using-superpowers" in [s.name for s in out]


def test_codex_provider_respects_none_filter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_skill(home / ".codex" / "skills", "using-superpowers")
    ctx = SkillSourceContext(
        roots=(tmp_path / "ws",), home=home, skills_filter="none", bundle_dir=None
    )
    assert resolve_harness_skills(ctx, "codex") == []


def test_cursor_provider_surfaces_skills_by_dir_name(tmp_path: Path) -> None:
    """
    Cursor names a skill by its ``plugin--skill`` directory (collision-safe
    across the many ``fe-*`` plugins), not the bare frontmatter ``name``.
    """
    home = tmp_path / "home"
    skill_dir = home / ".cursor" / "skills" / "fe-epl-tools--metric-view-adoption"
    skill_dir.mkdir(parents=True)
    # Frontmatter name is the BARE name; the dir carries the namespace.
    (skill_dir / "SKILL.md").write_text(
        "---\nname: metric-view-adoption\ndescription: MV adoption workflow.\n---\nbody\n"
    )
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "cursor-native")
    assert "fe-epl-tools--metric-view-adoption" in [s.name for s in out]


def test_cursor_provider_respects_none_filter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skill_dir = home / ".cursor" / "skills" / "fe-epl-tools--metric-view-adoption"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: metric-view-adoption\ndescription: d\n---\nbody\n"
    )
    ctx = SkillSourceContext(
        roots=(tmp_path / "ws",), home=home, skills_filter="none", bundle_dir=None
    )
    assert resolve_harness_skills(ctx, "cursor") == []


def test_pi_provider_is_bundle_only_noop(tmp_path: Path) -> None:
    """
    Pi loads skills from the bundle (already carried by ``spec.skills``,
    the base layer) and auto-discovers host skills internally — but
    omnigent can't enumerate Pi's host-skill layout to name/resolve them,
    so the provider surfaces nothing extra (under-report rather than list
    a command that won't resolve).
    """
    home = tmp_path / "home"
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "pi-native")
    assert out == []


def test_pi_session_does_not_inherit_generic_claude_host_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A Pi session must not surface ``~/.claude/skills`` host skills (those
    belong to Claude). Proves Pi has an explicit provider rather than
    falling through to the generic host walk.
    """
    home = tmp_path / "home"
    _write_skill(home / ".claude" / "skills", "claude-only-skill")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "pi-native")
    assert "claude-only-skill" not in [s.name for s in out]


def test_codex_provider_filters_user_invocable_false(tmp_path: Path) -> None:
    """Codex (and all harnesses) must not surface user-invocable:false skills."""
    home = tmp_path / "home"
    _write_skill(home / ".codex" / "skills", "triage", user_invocable=False)
    _write_skill(home / ".codex" / "skills", "account-review-deck")  # absent -> shown
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "codex-native")]
    assert "triage" not in names
    assert "account-review-deck" in names


def test_cursor_provider_filters_user_invocable_false(tmp_path: Path) -> None:
    """A user-invocable:false cursor skill is dropped (consistent across harnesses)."""
    home = tmp_path / "home"
    _write_skill(home / ".cursor" / "skills", "sra--triage", user_invocable=False)
    _write_skill(home / ".cursor" / "skills", "fe--report", user_invocable=True)
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "cursor-native")]
    assert "sra--triage" not in names
    assert "fe--report" in names


def test_claude_plugin_provider_filters_user_invocable_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled plugin's user-invocable:false skill is not surfaced."""
    home = tmp_path / "home"
    install = home / ".claude" / "plugins" / "cache" / "mkt" / "sra" / "1.0.0"
    _write_skill(install / "skills", "triage", user_invocable=False)
    _write_skill(install / "skills", "aisec-review", user_invocable=True)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sra@mkt": True}})
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"sra@mkt": [{"installPath": str(install)}]}})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "sra:triage" not in names
    assert "sra:aisec-review" in names


def test_codex_provider_surfaces_skills_by_dir_name(tmp_path: Path) -> None:
    """
    When a Codex skill's directory name differs from its frontmatter name,
    the menu surfaces the DIRECTORY name — the command Codex registers and
    the name the executor symlinks under (so menu label == runnable name).
    """
    home = tmp_path / "home"
    skill_dir = home / ".codex" / "skills" / "sra--triage"
    skill_dir.mkdir(parents=True)
    # Frontmatter name is bare; directory carries the namespace.
    (skill_dir / "SKILL.md").write_text("---\nname: triage\ndescription: d\n---\nbody\n")
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "codex-native")]
    assert "sra--triage" in names
    assert "triage" not in names


def test_codex_menu_set_matches_executor_linked_set(tmp_path: Path) -> None:
    """
    Invariant: the menu provider and the executor's symlink path select the
    SAME skill set from the SAME sources (both via codex_skill_sources +
    select_codex_skill_dirs) — so a / menu entry is always actually linked.
    """
    from omnigent.inner.codex_executor import (
        codex_skill_sources,
        select_codex_skill_dirs,
    )

    home = tmp_path / "home"
    host = home / ".codex" / "skills"
    for n in ("alpha", "beta--gamma"):
        d = host / n
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {n.split('--')[-1]}\ndescription: d\n---\nx\n")

    # What the executor would symlink (keys = dir names linked into CODEX_HOME).
    linked = set(select_codex_skill_dirs("all", codex_skill_sources(None, home)))
    # What the menu surfaces.
    menu = {s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "codex-native")}
    assert menu == linked == {"alpha", "beta--gamma"}


def test_claude_provider_project_disable_overrides_global_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin enabled globally but disabled in-project is excluded (scope precedence)."""
    home = tmp_path / "home"
    install = home / ".claude" / "plugins" / "cache" / "mkt" / "sp" / "1.0.0"
    _write_skill(install / "skills", "using-superpowers")
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": True}})  # global: enabled
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"sp@mkt": [{"installPath": str(install)}]}})
    )
    # Project root disables it.
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": False}})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    ctx = SkillSourceContext(roots=(ws,), home=home, skills_filter="all", bundle_dir=None)
    names = [s.name for s in resolve_harness_skills(ctx, "claude-native")]
    assert "sp:using-superpowers" not in names


def test_claude_provider_install_path_from_later_scope_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """installPath is taken from the first entry that has one, not blindly entries[0]."""
    home = tmp_path / "home"
    install = home / ".claude" / "plugins" / "cache" / "mkt" / "sp" / "1.0.0"
    _write_skill(install / "skills", "using-superpowers")
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": True}})
    )
    # First entry lacks installPath; the second carries it.
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "sp@mkt": [
                        {"scope": "project"},
                        {"scope": "user", "installPath": str(install)},
                    ]
                },
            }
        )
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "sp:using-superpowers" in names


def _claude_home_plugin_installed(home: Path, key: str, skill: str) -> None:
    """Install (not toggle) one plugin + skill under a fake ~/.claude."""
    install = home / ".claude" / "plugins" / "cache" / "mkt" / key.split("@")[0] / "1.0.0"
    _write_skill(install / "skills", skill)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {key: [{"installPath": str(install)}]}})
    )


def test_claude_local_settings_disable_overrides_settings_json_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings.local.json (local override) wins over settings.json within a scope."""
    home = tmp_path / "home"
    _claude_home_plugin_installed(home, "sp@mkt", "using-superpowers")
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": True}})  # enabled in shared
    )
    (home / ".claude" / "settings.local.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": False}})  # disabled locally
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "sp:using-superpowers" not in names


def test_claude_local_settings_enable_overrides_settings_json_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin enabled only in settings.local.json is surfaced."""
    home = tmp_path / "home"
    _claude_home_plugin_installed(home, "sp@mkt", "using-superpowers")
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": False}})
    )
    (home / ".claude" / "settings.local.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": True}})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "sp:using-superpowers" in names


def test_claude_workspace_settings_win_over_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Among roots, the workspace (primary) overrides the shipped bundle."""
    home = tmp_path / "home"
    _claude_home_plugin_installed(home, "sp@mkt", "using-superpowers")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace = tmp_path / "ws"
    bundle = tmp_path / "bundle"
    (workspace / ".claude").mkdir(parents=True)
    (bundle / ".claude").mkdir(parents=True)
    # Bundle enables; workspace disables — workspace must win.
    (bundle / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": True}})
    )
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": False}})
    )
    ctx = SkillSourceContext(
        roots=(workspace, bundle), home=home, skills_filter="all", bundle_dir=None
    )
    names = [s.name for s in resolve_harness_skills(ctx, "claude-native")]
    assert "sp:using-superpowers" not in names


def test_non_invocable_host_skill_shadows_same_named_invocable_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Documented behavior: a workspace skill marked user-invocable:false
    shadows a same-named invocable home skill, so it is filtered out
    entirely rather than falling back to the invocable copy — the
    project's authoritative copy wins (it shadows in execution too).
    """
    home = tmp_path / "home"
    _write_skill(home / ".claude" / "skills", "x")  # home: invocable
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".claude" / "skills", "x", user_invocable=False)  # project: internal

    out = resolve_harness_skills(_ctx(workspace, home), "claude-native")
    assert "x" not in [s.name for s in out]


def test_claude_provider_string_false_enablement_does_not_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON string ``"false"`` must not enable a plugin (only real bools count)."""
    home = tmp_path / "home"
    install = home / ".claude" / "plugins" / "cache" / "mkt" / "sp" / "1.0.0"
    _write_skill(install / "skills", "using-superpowers")
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": "false"}})  # truthy string, not a bool
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"sp@mkt": [{"installPath": str(install)}]}})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "sp:using-superpowers" not in names


def test_claude_provider_skips_install_path_outside_plugins_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installPath escaping ~/.claude/plugins/ is skipped, not scanned."""
    home = tmp_path / "home"
    # Skill lives OUTSIDE the plugins cache root (a tampered/odd manifest).
    outside = tmp_path / "evil"
    _write_skill(outside / "skills", "using-superpowers")
    (home / ".claude" / "plugins").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"sp@mkt": True}})
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"sp@mkt": [{"installPath": str(outside)}]}})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "sp:using-superpowers" not in names


def test_cursor_provider_tolerates_unreadable_skills_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable ~/.cursor/skills must yield [] (lenient), not 500 the menu."""
    home = tmp_path / "home"
    _write_skill(home / ".cursor" / "skills", "fe--report")

    real_iterdir = Path.iterdir

    def _boom(self: Path):
        if self.name == "skills" and self.parent.name == ".cursor":
            raise PermissionError("permission denied")
        return real_iterdir(self)

    monkeypatch.setattr("pathlib.Path.iterdir", _boom)
    # Must not raise.
    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "cursor-native")
    assert out == []


# ---------------------------------------------------------------------------
# antigravity (agy CLI) provider
# ---------------------------------------------------------------------------
#
# Layout live-verified against agy 1.1.9:
#   ~/.gemini/config/plugins/<name>/skills/<skill>/SKILL.md   (imported plugins)
#   ~/.gemini/antigravity-cli/builtin/skills/<skill>/SKILL.md (shipped builtins)
# A plugin is DISABLED by renaming its manifest to ``plugin.json.disabled``
# (``agy plugin disable`` performs exactly that rename; the import manifest and
# config.json are left untouched, and ``agy plugin list`` still lists it).


def _write_agy_plugin(home: Path, plugin: str, *skills: str, enabled: bool = True) -> None:
    """Write an agy plugin with *skills*, enabled or disabled via its manifest name."""
    root = home / ".gemini" / "config" / "plugins" / plugin
    root.mkdir(parents=True, exist_ok=True)
    (root / ("plugin.json" if enabled else "plugin.json.disabled")).write_text('{"name":"x"}')
    for skill in skills:
        _write_skill(root / "skills", skill)


def test_antigravity_provider_surfaces_plugin_and_builtin_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agy's own skills — imported plugins plus shipped builtins — reach the menu."""
    home = tmp_path / "home"
    _write_agy_plugin(home, "superpowers", "brainstorming", "writing-plans")
    _write_skill(home / ".gemini" / "antigravity-cli" / "builtin" / "skills", "antigravity-guide")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [
        s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "antigravity-native")
    ]
    # Plugin skills are namespaced <plugin>:<skill> (agy's own /skills panel
    # lists them that way and the TUI only accepts that spelling); builtins are
    # bare. Live-verified against agy 1.1.9.
    assert sorted(names) == [
        "antigravity-guide",
        "superpowers:brainstorming",
        "superpowers:writing-plans",
    ]


def test_antigravity_provider_skips_disabled_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin disabled via the plugin.json -> plugin.json.disabled rename is hidden.

    The skills stay on disk and the import manifest still lists the plugin, so the
    manifest is NOT a usable enabled-signal — the manifest name is.
    """
    home = tmp_path / "home"
    _write_agy_plugin(home, "superpowers", "brainstorming", enabled=False)
    _write_agy_plugin(home, "othertools", "still-on", enabled=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [
        s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "antigravity-native")
    ]
    assert "superpowers:brainstorming" not in names
    assert "othertools:still-on" in names


def test_antigravity_session_does_not_inherit_generic_claude_host_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agy session must not surface ~/.claude/skills (those belong to Claude).

    agy owns an enumerable host-skill mechanism, so it lists exactly what agy has
    rather than falling through to the generic walk.
    """
    home = tmp_path / "home"
    _write_skill(home / ".claude" / "skills", "claude-only-skill")
    _write_agy_plugin(home, "superpowers", "brainstorming")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [
        s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "antigravity-native")
    ]
    assert "claude-only-skill" not in names
    assert "superpowers:brainstorming" in names


def test_antigravity_sdk_harness_keeps_the_generic_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process Gemini SDK harness is NOT agy and keeps the generic fallback.

    It never launches the agy CLI, so ~/.gemini plugins are not its skills; its
    skills are the omnigent-injected generic ones.
    """
    home = tmp_path / "home"
    _write_skill(home / ".claude" / "skills", "claude-only-skill")
    _write_agy_plugin(home, "superpowers", "brainstorming")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "antigravity")]
    assert "claude-only-skill" in names
    assert "superpowers:brainstorming" not in names


def test_antigravity_provider_filters_user_invocable_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-invocable:false agy skill is dropped (consistent across harnesses)."""
    home = tmp_path / "home"
    _write_agy_plugin(home, "superpowers")
    _write_skill(home / ".gemini" / "config" / "plugins" / "superpowers" / "skills", "shown")
    _write_skill(
        home / ".gemini" / "config" / "plugins" / "superpowers" / "skills",
        "internal",
        user_invocable=False,
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [
        s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "antigravity-native")
    ]
    assert names == ["superpowers:shown"]


def test_antigravity_provider_respects_none_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``skills: none`` in the agent spec suppresses agy's host skills."""
    home = tmp_path / "home"
    _write_agy_plugin(home, "superpowers", "brainstorming")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    out = resolve_harness_skills(_ctx(tmp_path / "ws", home, "none"), "antigravity-native")
    assert out == []


def test_antigravity_provider_tolerates_missing_and_unreadable_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ~/.gemini at all (or an unreadable tree) yields [] rather than raising."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    assert resolve_harness_skills(_ctx(tmp_path / "ws", home), "antigravity-native") == []

    _write_agy_plugin(home, "superpowers", "brainstorming")
    real_iterdir = Path.iterdir

    def _boom(self: Path):
        if self.name == "plugins":
            raise PermissionError("permission denied")
        return real_iterdir(self)

    monkeypatch.setattr("pathlib.Path.iterdir", _boom)
    # Must not raise.
    assert resolve_harness_skills(_ctx(tmp_path / "ws", home), "antigravity-native") == []


def test_antigravity_provider_surfaces_all_five_agy_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every source agy's own /skills panel lists is surfaced.

    Live-verified against agy 1.1.9, whose panel names them: Workspace
    (``<ws>/.agents/skills``), Global (``antigravity-cli/skills``), Shared
    (``.gemini/skills``), plus imported plugins and shipped builtins.
    """
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    _write_skill(ws / ".agents" / "skills", "ws-skill")
    _write_skill(home / ".gemini" / "antigravity-cli" / "skills", "global-skill")
    _write_skill(home / ".gemini" / "skills", "shared-skill")
    _write_agy_plugin(home, "superpowers", "brainstorming")
    _write_skill(home / ".gemini" / "antigravity-cli" / "builtin" / "skills", "antigravity-guide")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = sorted(s.name for s in resolve_harness_skills(_ctx(ws, home), "antigravity-native"))
    assert names == [
        "antigravity-guide",
        "global-skill",
        "shared-skill",
        "superpowers:brainstorming",
        "ws-skill",
    ]


def test_antigravity_provider_reads_agents_skills_not_claude_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.agents/skills`` in the workspace is agy's; ``.claude/skills`` is not.

    The generic walk scans both, which is why agy previously saw Claude's. agy
    genuinely reads the vendor-neutral ``.agents/skills``, so that one stays.
    """
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    _write_skill(ws / ".agents" / "skills", "neutral-skill")
    _write_skill(ws / ".claude" / "skills", "claude-ws-skill")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(ws, home), "antigravity-native")]
    assert names == ["neutral-skill"]


def _write_plugin_command(commands_dir: Path, name: str, text: str) -> None:
    """Write a ``<commands_dir>/<name>.md`` command file (plugin or ``.claude/commands``)."""
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / f"{name}.md").write_text(text)


def _claude_home_with_plugin_command(
    home: Path, *, plugin: str = "knowledge-base", marketplace: str = "mkt"
) -> tuple[Path, Path]:
    """Seed a fake ~/.claude with one enabled plugin carrying a skill and a command.

    :returns: ``(home, install)`` where *install* is the plugin's install path.
    """
    _claude_home_with_plugin(
        home, plugin=plugin, marketplace=marketplace, skill="kb-search", enabled=True
    )
    install = home / ".claude" / "plugins" / "cache" / marketplace / plugin / "1.0.0"
    _write_plugin_command(
        install / "commands",
        "kb-review",
        "---\n"
        "description: Review a knowledge-base PR\n"
        "argument-hint: --pr <number>\n"
        "---\n"
        "Review the KB pull request given as $ARGUMENTS.\n",
    )
    return home, install


def _write_plugin_manifest(install: Path, **fields: object) -> None:
    """Write the plugin's ``.claude-plugin/plugin.json`` with *fields*."""
    (install / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (install / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": install.parent.name, "version": "1.0.0", **fields})
    )


def test_claude_provider_surfaces_plugin_commands_namespaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin's ``commands/<name>.md`` shows up next to its skills."""
    home, _ = _claude_home_with_plugin_command(tmp_path / "home")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
    by_name = {s.name: s for s in out}
    assert "knowledge-base:kb-search" in by_name
    command = by_name.get("knowledge-base:kb-review")
    assert command is not None
    assert command.description == "Review a knowledge-base PR"
    # Sibling files under commands/ are other commands, not skill resources.
    assert command.skill_dir is None


def test_plugin_command_without_frontmatter_uses_first_body_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frontmatter is optional on plugin commands; the body's first line labels it."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home", plugin="tools")
    _write_plugin_command(install / "commands", "deploy", "Deploy the current branch.\n")
    _write_plugin_command(install / "commands", "headed", "# Ship it\n\nShip the branch.\n")
    _write_plugin_command(install / "commands", "wordy", "Long first line " + "x" * 120 + "\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
    by_name = {s.name: s for s in out}
    command = by_name.get("tools:deploy")
    assert command is not None
    assert command.description == "Deploy the current branch."
    assert command.content == "Deploy the current branch."
    # A heading loses its marker and a long line is cut at 100 characters, as in Claude Code.
    assert by_name["tools:headed"].description == "Ship it"
    assert by_name["tools:wordy"].description == ("Long first line " + "x" * 120)[:97] + "..."


def test_plugin_commands_respect_list_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``skills_filter`` list selects plugin commands by bare name, as it does skills."""
    home, _ = _claude_home_with_plugin_command(tmp_path / "home")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    selected = _ctx(tmp_path / "ws", home, skills_filter=["kb-review"])
    names = [s.name for s in resolve_harness_skills(selected, "claude-native")]
    assert names == ["knowledge-base:kb-review"]


def test_plugin_command_description_with_colon_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prose description containing ``: `` still labels the command."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    _write_plugin_command(
        install / "commands",
        "kb-triage",
        "---\ndescription: Triage a ticket: label, assign, and reply\n---\nTriage $ARGUMENTS.\n",
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
    command = {s.name: s for s in out}["knowledge-base:kb-triage"]
    assert command.description == "Triage a ticket: label, assign, and reply"


def test_plugin_command_marked_not_user_invocable_is_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``user-invocable: false`` hides a command from the menu, as it does a skill."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    _write_plugin_command(
        install / "commands",
        "kb-internal",
        "---\ndescription: model-only step\nuser-invocable: false\n---\nbody\n",
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "knowledge-base:kb-review" in names
    assert "knowledge-base:kb-internal" not in names


def test_plugin_command_frontmatter_name_replaces_its_file_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frontmatter ``name`` renames the command in place; parent directories still prefix it."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    _write_plugin_command(
        install / "commands", "kb-old", "---\nname: kb-new\ndescription: renamed\n---\nbody\n"
    )
    _write_plugin_command(
        install / "commands" / "reports",
        "draft",
        "---\nname: Weekly Report\ndescription: spaced\n---\nbody\n",
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "knowledge-base:kb-new" in names
    assert "knowledge-base:reports:Weekly Report" in names
    for absent in ("knowledge-base:kb-old", "knowledge-base:reports:draft"):
        assert absent not in names


def test_plugin_commands_namespace_nested_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested files are ``<plugin>:<dir>:<name>``; hidden paths and any-case ``.md`` count."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    (install / "commands" / "notes.txt").write_text("not a command")
    _write_plugin_command(
        install / "commands" / "group", "inner", "---\ndescription: nested\n---\nbody\n"
    )
    _write_plugin_command(install / "commands" / "group" / "deep", "leaf", "Deep command.\n")
    _write_plugin_command(install / "commands" / ".drafts", "wip", "Hidden paths count.\n")
    (install / "commands" / "SHOUT.MD").write_text("Only a lowercase .md is stripped.\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "knowledge-base:kb-review" in names
    assert "knowledge-base:group:inner" in names
    assert "knowledge-base:group:deep:leaf" in names
    assert "knowledge-base:.drafts:wip" in names
    assert "knowledge-base:SHOUT.MD" in names
    for absent in ("knowledge-base:notes", "knowledge-base:inner", "knowledge-base:group"):
        assert absent not in names


def test_plugin_command_directory_with_skill_md_is_one_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory holding ``skill.md`` is one command named by that file; siblings are ignored."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    (install / "commands" / "legacy").mkdir()
    (install / "commands" / "legacy" / "skill.md").write_text("Directory-named command.\n")
    _write_plugin_command(install / "commands" / "legacy", "sibling", "Ignored next to skill.md\n")
    (install / "commands" / "deep" / "er").mkdir(parents=True)
    (install / "commands" / "deep" / "er" / "SKILL.md").write_text(
        "---\nname: deepname\ndescription: Renamed by its frontmatter.\n---\nbody\n"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
    by_name = {s.name: s for s in out}
    assert by_name["knowledge-base:kb-review"].description == "Review a knowledge-base PR"
    assert by_name["knowledge-base:legacy"].description == "Directory-named command."
    assert by_name["knowledge-base:deep:deepname"].description == "Renamed by its frontmatter."
    for absent in (
        "knowledge-base:legacy:skill",
        "knowledge-base:legacy:sibling",
        "knowledge-base:deep:er",
    ):
        assert absent not in by_name


def test_plugin_commands_root_skill_md_collapses_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``skill.md`` directly under ``commands/`` makes the whole directory one command."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    (install / "commands" / "SKILL.md").write_text(
        "---\nname: rootskill\ndescription: Root SKILL.md under commands.\n---\nbody\n"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "knowledge-base:kb-search" in names
    assert "knowledge-base:rootskill" in names
    for absent in ("knowledge-base:kb-review", "knowledge-base:commands"):
        assert absent not in names


def test_plugin_commands_stop_at_claude_code_depth_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commands 31 directories deep are offered; the 32nd level is beyond Claude Code's bound."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    deepest = install / "commands" / Path(*(f"d{index}" for index in range(31)))
    _write_plugin_command(deepest, "ok", "Thirty-one directories deep.\n")
    _write_plugin_command(deepest / "d31", "too-deep", "Beyond Claude Code's depth bound.\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    deep_prefix = "knowledge-base:" + ":".join(f"d{index}" for index in range(31))
    assert f"{deep_prefix}:ok" in names
    assert f"{deep_prefix}:d31:too-deep" not in names


def test_plugin_commands_do_not_follow_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlinked entries are skipped, so a directory cycle cannot mint duplicate commands."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    commands = install / "commands"
    (commands / "loop").mkdir()
    (commands / "loop" / "back").symlink_to(commands, target_is_directory=True)
    (commands / "alias.md").symlink_to(commands / "kb-review.md")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert names.count("knowledge-base:kb-review") == 1
    assert not any(":loop" in name or name == "knowledge-base:alias" for name in names)


def test_plugin_commands_root_linked_outside_plugin_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``commands/`` symlink that leaves the plugin is not scanned."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    outside = tmp_path / "outside"
    _write_plugin_command(outside, "leak", "---\ndescription: outside\n---\nbody\n")
    (install / "commands" / "kb-review.md").unlink()
    (install / "commands").rmdir()
    (install / "commands").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "knowledge-base:kb-search" in names
    assert "knowledge-base:leak" not in names


@pytest.mark.parametrize("layout", ["missing", "self-link"])
def test_plugin_without_usable_commands_root_still_lists_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, layout: str
) -> None:
    """A missing or self-linked ``commands/`` yields no commands, quietly, and keeps the skills."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    (install / "commands" / "kb-review.md").unlink()
    (install / "commands").rmdir()
    if layout == "self-link":
        (install / "commands").symlink_to("commands")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    with caplog.at_level(logging.WARNING, logger="omnigent.spec.skill_sources"):
        names = [
            s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
        ]
    assert names == ["knowledge-base:kb-search"]
    assert "commands" not in caplog.text


def test_plugin_command_over_size_limit_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code ignores command files over 1 MiB; a file at the limit is still offered."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    header = "---\ndescription: big\n---\n"
    _write_plugin_command(install / "commands", "kb-huge", header + "x" * (1024 * 1024))
    _write_plugin_command(
        install / "commands", "kb-limit", header + "x" * (1024 * 1024 - len(header))
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "knowledge-base:kb-review" in names
    assert "knowledge-base:kb-limit" in names
    assert "knowledge-base:kb-huge" not in names


def test_plugin_skill_wins_name_collision_with_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a plugin ships skills/x and commands/x.md, the skill entry wins."""
    home = _claude_home_with_plugin(
        tmp_path / "home", plugin="dup", marketplace="mkt", skill="review", enabled=True
    )
    install = home / ".claude" / "plugins" / "cache" / "mkt" / "dup" / "1.0.0"
    _write_plugin_command(
        install / "commands", "review", "---\ndescription: command flavor\n---\nbody\n"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    out = resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
    matches = [s for s in out if s.name == "dup:review"]
    assert len(matches) == 1
    assert matches[0].skill_dir is not None  # the skill entry won, not the command


@pytest.mark.parametrize(
    "declared",
    [["./extra/epsilon.md", "./more-cmds/", "./skilldir/"], "./more-cmds/"],
    ids=["list", "string"],
)
def test_plugin_manifest_commands_replace_the_default_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared: list[str] | str
) -> None:
    """Manifest ``commands`` paths load instead of ``commands/``, as files or directories."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    _write_plugin_manifest(install, commands=declared)
    _write_plugin_command(install / "extra", "epsilon", "---\ndescription: file\n---\nbody\n")
    _write_plugin_command(install / "more-cmds", "zeta", "---\ndescription: dir\n---\nbody\n")
    _write_plugin_command(install / "more-cmds" / "sub", "x", "Nested under a listed dir.\n")
    (install / "skilldir").mkdir()
    (install / "skilldir" / "SKILL.md").write_text(
        "---\nname: sd\ndescription: skill dir\n---\nbody\n"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "knowledge-base:kb-search" in names
    assert "knowledge-base:kb-review" not in names  # commands/ is no longer auto-loaded
    assert "knowledge-base:zeta" in names
    assert "knowledge-base:sub:x" in names
    listed_file_and_skill_dir = isinstance(declared, list)
    assert ("knowledge-base:epsilon" in names) is listed_file_and_skill_dir
    assert ("knowledge-base:sd" in names) is listed_file_and_skill_dir


def test_plugin_manifest_command_path_outside_plugin_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A manifest path that escapes the plugin root, or points nowhere, is logged and skipped."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    _write_plugin_command(install.parent, "escaped", "---\ndescription: outside\n---\nbody\n")
    _write_plugin_command(tmp_path, "absolute", "---\ndescription: outside\n---\nbody\n")
    (install / "loop.md").symlink_to("loop.md")
    _write_plugin_manifest(
        install,
        commands=[
            "./commands/kb-review.md",
            "../escaped.md",
            str(tmp_path / "absolute.md"),
            "./typo/missing.md",
            "./bad\x00.md",
            "./loop.md",
        ],
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    with caplog.at_level(logging.WARNING, logger="omnigent.spec.skill_sources"):
        names = [
            s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
        ]
    assert "knowledge-base:kb-review" in names
    for absent in ("knowledge-base:escaped", "knowledge-base:absolute"):
        assert absent not in names
    assert "'../escaped.md' is outside the plugin" in caplog.text
    assert "typo/missing.md is missing or not .md" in caplog.text
    assert "'./bad\\x00.md' is unusable" in caplog.text
    assert "'./loop.md' is unusable" in caplog.text


def test_plugin_manifest_links_keep_their_declared_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared entry that is a symlink is followed and named as declared."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    _write_plugin_command(install / "real-dir", "x", "---\ndescription: linked dir\n---\nbody\n")
    (install / "alias.md").symlink_to(install / "commands" / "kb-review.md")
    (install / "dir-link").symlink_to(install / "real-dir", target_is_directory=True)
    _write_plugin_manifest(install, commands=["./alias.md", "./dir-link/"])
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    names = [s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")]
    assert "knowledge-base:alias" in names
    assert "knowledge-base:x" in names
    for absent in ("knowledge-base:kb-review", "knowledge-base:real", "knowledge-base:dir-link"):
        assert absent not in names


def test_plugin_manifest_malformed_commands_field_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``commands`` value that is neither a path nor a list yields no commands and a warning."""
    home, install = _claude_home_with_plugin_command(tmp_path / "home")
    _write_plugin_manifest(install, commands={"path": "./commands/"})
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    with caplog.at_level(logging.WARNING, logger="omnigent.spec.skill_sources"):
        names = [
            s.name for s in resolve_harness_skills(_ctx(tmp_path / "ws", home), "claude-native")
        ]
    assert names == ["knowledge-base:kb-search"]
    assert "manifest 'commands' must be a path or a list of paths" in caplog.text


def _claude_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A fake home holding a git checkout, the layout a native Claude session runs in.

    :returns: ``(home, workspace)``.
    """
    home = tmp_path / "home"
    workspace = home / "repo"
    (workspace / ".git").mkdir(parents=True)
    return home, workspace


def test_claude_native_lists_personal_and_project_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code loads ``.claude/commands`` files as slash commands, so the native menu does too.

    A file's path names it (``ops/deploy.md`` is ``ops:deploy``), its frontmatter
    ``name`` is ignored, and the description falls back to the first body line.
    """
    home, workspace = _claude_workspace(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    _write_plugin_command(
        home / ".claude" / "commands",
        "team",
        "---\ndescription: Orchestrate a team\nargument-hint: <task>\n---\nTask: $ARGUMENTS\n",
    )
    project = workspace / ".claude" / "commands"
    _write_plugin_command(project / "ops", "deploy", "# Deploy the branch\n\nSteps.\n")
    _write_plugin_command(project, "old", "---\nname: renamed\ndescription: kept\n---\nbody\n")
    # Claude Code only lists lowercase ``.md`` files here.
    (project / "notes.MD").write_text("Not a command.\n")
    ctx = _ctx(workspace, home)

    native = {s.name: s for s in resolve_harness_skills(ctx, "claude-native")}
    assert set(native) == {"team", "ops:deploy", "old"}
    assert native["team"].description == "Orchestrate a team"
    assert native["ops:deploy"].description == "Deploy the branch"
    assert native["old"].description == "kept"
    assert all(spec.skill_dir is None for spec in native.values())
    # The in-process SDK harness keeps its generic walk, which has no commands tier.
    assert resolve_harness_skills(ctx, "claude-sdk") == []


def test_claude_command_directory_with_skill_md_is_one_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``commands/<dir>/SKILL.md`` is the command ``<dir>``; siblings are not, subdirs are.

    A ``SKILL.md`` at the root itself names the root, ``/commands``, as in Claude Code.
    """
    home, workspace = _claude_workspace(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    commands = workspace / ".claude" / "commands"
    _write_plugin_command(commands, "SKILL", "---\ndescription: Root skill\n---\nbody\n")
    _write_plugin_command(commands, "standup", "A root-level sibling.\n")
    review = commands / "review"
    _write_plugin_command(review, "SKILL", "---\ndescription: Review a PR\n---\nbody\n")
    _write_plugin_command(review, "checklist", "Reference the skill reads.\n")
    _write_plugin_command(review / "deep", "security", "Security pass.\n")

    out = resolve_harness_skills(_ctx(workspace, home), "claude-native")
    assert sorted((s.name, s.description) for s in out) == [
        ("commands", "Root skill"),
        ("review", "Review a PR"),
        ("review:deep:security", "Security pass."),
    ]


def test_claude_skill_and_personal_command_win_name_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As in Claude Code, skills beat same-named commands and personal ones beat project ones.

    A command named like a plugin entry (``<plugin>/<name>.md``) beats that entry too.
    """
    home, workspace = _claude_workspace(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    _claude_home_with_plugin_command(home)
    _write_skill(home / ".claude" / "skills", "deploy")
    personal = home / ".claude" / "commands"
    project = workspace / ".claude" / "commands"
    _write_plugin_command(project, "deploy", "---\ndescription: project deploy\n---\nbody\n")
    _write_plugin_command(personal, "ship", "---\ndescription: personal ship\n---\nbody\n")
    _write_plugin_command(project, "ship", "---\ndescription: project ship\n---\nbody\n")
    _write_plugin_command(
        project / "knowledge-base", "kb-review", "---\ndescription: project review\n---\nbody\n"
    )

    out = resolve_harness_skills(_ctx(workspace, home), "claude-native")
    assert sorted((s.name, s.description) for s in out) == [
        ("deploy", "deploy desc"),
        ("knowledge-base:kb-review", "project review"),
        ("knowledge-base:kb-search", "kb-search desc"),
        ("ship", "personal ship"),
    ]


@pytest.mark.parametrize("git_entry", ["directory", "file"])
def test_claude_project_commands_stop_at_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_entry: str
) -> None:
    """Project commands come from the workspace up to its repository root, not above it.

    A linked worktree marks its root with a ``.git`` file rather than a directory.
    """
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    repo = home / "code" / "repo"
    repo.mkdir(parents=True)
    if git_entry == "directory":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/repo\n")
    workspace = repo / "service"
    _write_plugin_command(workspace / ".claude" / "commands", "service-cmd", "Workspace.\n")
    _write_plugin_command(repo / ".claude" / "commands", "repo-cmd", "Repository root.\n")
    _write_plugin_command(home / "code" / ".claude" / "commands", "above-cmd", "Above it.\n")

    names = {s.name for s in resolve_harness_skills(_ctx(workspace, home), "claude-native")}
    assert names == {"service-cmd", "repo-cmd"}


def test_claude_project_commands_outside_home_and_any_repository_read_every_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With neither a repository root nor home above it, the walk reads each ancestor."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    scratch = tmp_path / "scratch"
    workspace = scratch / "project"
    _write_plugin_command(workspace / ".claude" / "commands", "here-cmd", "Workspace.\n")
    _write_plugin_command(scratch / ".claude" / "commands", "above-cmd", "An ancestor.\n")

    names = {s.name for s in resolve_harness_skills(_ctx(workspace, home), "claude-native")}
    # Directories above tmp_path are the test machine's own, so check only these two.
    assert {"here-cmd", "above-cmd"} <= names


def test_claude_personal_commands_follow_config_dir_and_skip_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$CLAUDE_CONFIG_DIR`` moves the personal tier, and the project walk stops below home."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace = home / "scratch"  # not a repository, so the project walk climbs to home
    _write_plugin_command(workspace / ".claude" / "commands", "scratch-cmd", "Workspace.\n")
    _write_plugin_command(home / ".claude" / "commands", "home-cmd", "Default user tier.\n")
    config = tmp_path / "claude-config"
    _write_plugin_command(config / "commands", "config-cmd", "Configured user tier.\n")

    def names(ctx: SkillSourceContext) -> set[str]:
        return {s.name for s in resolve_harness_skills(ctx, "claude-native")}

    assert names(_ctx(workspace, home)) == {"home-cmd", "scratch-cmd"}
    assert names(_ctx(workspace, home, claude_config_dir=config)) == {"config-cmd", "scratch-cmd"}


def test_claude_commands_follow_symlinks_and_load_each_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linked command dirs load, as Claude Code follows links; loops and aliases add no names."""
    home, workspace = _claude_workspace(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    personal = home / ".claude" / "commands"
    _write_plugin_command(personal, "team", "Team.\n")
    (personal / "team-alias.md").symlink_to(personal / "team.md")
    shared = tmp_path / "shared-commands"
    _write_plugin_command(shared, "lint", "Lint the tree.\n")
    (personal / "shared").symlink_to(shared, target_is_directory=True)
    (personal / "loop").symlink_to(personal, target_is_directory=True)
    # A project tier linked to the personal one (a dotfiles setup) repeats nothing.
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "commands").symlink_to(personal, target_is_directory=True)

    names = [s.name for s in resolve_harness_skills(_ctx(workspace, home), "claude-native")]
    assert len(names) == 2
    assert "shared:lint" in names
    assert len({"team", "team-alias"} & set(names)) == 1


def test_claude_commands_respect_skills_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``skills`` list selects commands by name; ``none`` launches Claude without them."""
    home, workspace = _claude_workspace(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    _write_plugin_command(home / ".claude" / "commands", "team", "Team.\n")
    _write_plugin_command(home / ".claude" / "commands", "merge", "Merge.\n")

    def names(skills_filter: str | list[str]) -> list[str]:
        ctx = _ctx(workspace, home, skills_filter=skills_filter)
        return [s.name for s in resolve_harness_skills(ctx, "claude-native")]

    assert names(["team"]) == ["team"]
    assert names("none") == []
