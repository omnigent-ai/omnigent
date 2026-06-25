from __future__ import annotations

import json
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


def _ctx(root: Path, home: Path, skills_filter: str | list[str] = "all") -> SkillSourceContext:
    """Build a context with a single discovery root and a pinned home."""
    return SkillSourceContext(
        roots=(root,), home=home, skills_filter=skills_filter, bundle_dir=None
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
        ("antigravity", None),
        ("qwen", None),
        (None, None),
        ("", None),
    ],
)
def test_harness_family(harness: str | None, expected: str | None) -> None:
    assert _harness_family(harness) == expected


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
