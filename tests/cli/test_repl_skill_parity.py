"""The REPL's slash commands are the ones the web composer offers.

Both surfaces answer "what can I type after ``/``" for the same session. The
runner builds the web menu with ``resolve_harness_skills``; the REPL used to
walk ``.claude/skills`` directly, which meant a Claude plugin's skills reached
the web menu and never reached the terminal. These tests pin the two together.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.chat import _merge_host_skills
from omnigent.spec.skill_sources import SkillSourceContext, resolve_harness_skills
from omnigent.spec.types import AgentSpec, ExecutorSpec, SkillSpec


def _write_skill(skills_dir: Path, name: str) -> None:
    """Write a minimal ``<skills_dir>/<name>/SKILL.md``."""
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} desc\n---\nbody\n")


def _claude_home_with_plugin(home: Path, *, plugin: str, skill: str) -> None:
    """Seed a fake ``~/.claude`` carrying one enabled, installed plugin."""
    install = home / ".claude" / "plugins" / "cache" / "mkt" / plugin / "1.0.0"
    _write_skill(install / "skills", skill)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {f"{plugin}@mkt": True}})
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    f"{plugin}@mkt": [
                        {"scope": "user", "installPath": str(install), "version": "1.0.0"}
                    ]
                },
            }
        )
    )


def _spec(harness: str, *, bundled: list[SkillSpec] | None = None) -> AgentSpec:
    """An agent spec that declares only a harness (and optional bundled skills)."""
    return AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type=harness),
        skills=bundled or [],
    )


def test_repl_offers_a_claude_plugin_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin's skill is typeable in the REPL, not just in the web menu.

    Claude Code registers an enabled plugin's skills as ``<plugin>:<skill>``
    slash commands, and the web composer lists them. The terminal walked only
    ``.claude/skills``, so the same session offered a shorter menu depending on
    which surface you typed into.
    """
    home = tmp_path / "home"
    _claude_home_with_plugin(home, plugin="superpowers", skill="using-superpowers")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(workspace)

    names = [s.name for s in _merge_host_skills(_spec("claude-native"), workspace)]

    assert "superpowers:using-superpowers" in names


def test_repl_matches_the_web_menu_for_the_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resolver, so the two composers cannot drift apart again.

    This is the invariant the bug broke: whatever the runner resolves for the
    web menu is what the REPL registers. Asserted as set equality rather than
    a membership check so an extra command on either side fails too.
    """
    home = tmp_path / "home"
    _claude_home_with_plugin(home, plugin="superpowers", skill="using-superpowers")
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".claude" / "skills", "code-review")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(workspace)

    repl = {s.name for s in _merge_host_skills(_spec("claude-native"), workspace)}
    web = {
        s.name
        for s in resolve_harness_skills(
            SkillSourceContext(
                roots=(workspace.resolve(),),
                home=home,
                skills_filter="all",
                bundle_dir=workspace,
            ),
            "claude-native",
        )
    }

    assert repl == web
    assert {"code-review", "superpowers:using-superpowers"} <= repl


def test_the_workspace_is_searched_not_only_the_agent_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``omnigent run --harness claude`` writes its spec to a temp directory.

    Walking up from that generated root reaches ``/`` without ever passing the
    directory the user is working in, so a project's own ``.claude/skills``
    would be invisible — the one place a project keeps its skills.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".claude" / "skills", "project-only")
    generated_spec_root = tmp_path / "generated"
    generated_spec_root.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(workspace)

    names = [s.name for s in _merge_host_skills(_spec("claude-sdk"), generated_spec_root)]

    assert "project-only" in names


def test_a_codex_session_lists_what_codex_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shrink for a replacing provider is deliberate, so pin it.

    ``select_codex_skill_dirs`` is the single source of truth for what gets
    symlinked into ``$CODEX_HOME/skills/``, so a ``~/.claude/skills`` entry is
    never registered by Codex. Offering it was a menu item that resolved to
    ``skill_not_found``. Without this test, unioning the generic walk back in
    would look like an improvement and pass every other test here.
    """
    home = tmp_path / "home"
    _write_skill(home / ".claude" / "skills", "claude-only")
    _write_skill(home / ".codex" / "skills", "codex-only")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(workspace)

    names = [s.name for s in _merge_host_skills(_spec("codex-native"), workspace)]

    assert "codex-only" in names
    assert "claude-only" not in names


def test_a_bundled_skill_is_not_offered_twice_under_its_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One skill, one command, even when its folder and its name differ.

    Codex names a skill by its *directory*, and its sources include the
    bundle's own ``skills/``. So a bundled skill in ``sra--triage/`` whose
    frontmatter says ``triage`` comes back from the provider under the other
    name, and deduplicating on the name alone lets it in a second time. The
    extra command resolves to nothing — the runner drops it on the directory
    match, so the REPL has to as well.
    """
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    bundle = tmp_path / "bundle"
    shared_dir = bundle / "skills" / "sra--triage"
    shared_dir.mkdir(parents=True)
    (shared_dir / "SKILL.md").write_text("---\nname: triage\ndescription: d\n---\nbody\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(tmp_path)
    bundled = SkillSpec(
        name="triage", description="the bundled one", content="body", skill_dir=shared_dir
    )

    names = [s.name for s in _merge_host_skills(_spec("codex-native", bundled=[bundled]), bundle)]

    assert names == ["triage"], names


def test_a_bundled_skill_still_wins_on_a_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's own skill is the one it ships with; the host copy loses."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".claude" / "skills", "code-review")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(workspace)
    bundled = SkillSpec(name="code-review", description="the bundled one", content="bundled body")

    merged = _merge_host_skills(_spec("claude-sdk", bundled=[bundled]), workspace)

    assert [s.description for s in merged if s.name == "code-review"] == ["the bundled one"]
