"""
Tests for ``omnigent.inner.bundle_skills`` — the shared helpers that
expose an agent bundle's skills to a Claude harness (the SDK executor and
the ``claude-native`` CLI launch path both use these so they stay in
lockstep).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.inner.bundle_skills import (
    claude_agents_skill_args,
    claude_native_skill_args,
    ensure_bundle_plugin_manifest,
)


def test_ensure_bundle_plugin_manifest_writes_when_missing(tmp_path: Path) -> None:
    """
    With no ``.claude-plugin/plugin.json`` present, the helper writes one
    with ``name = agent_name``.

    A regression that wrote the wrong name (or the bundle's basename)
    would mis-namespace every bundled skill in the model's listing
    (e.g. ``omnigent-ap-chat-x9p606iz/bundle:researcher``).
    """
    ensure_bundle_plugin_manifest(tmp_path, "coding-supervisor")
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    assert manifest.is_file()
    assert json.loads(manifest.read_text())["name"] == "coding-supervisor"


def test_ensure_bundle_plugin_manifest_is_idempotent(tmp_path: Path) -> None:
    """
    An existing manifest is preserved verbatim — the helper bails on the
    first ``.exists()`` check, protecting a user-authored richer manifest
    (e.g. with ``version`` / ``author``) from being overwritten.
    """
    (tmp_path / ".claude-plugin").mkdir()
    existing = tmp_path / ".claude-plugin" / "plugin.json"
    user_authored = '{"name":"user-name","author":"someone"}'
    existing.write_text(user_authored)
    ensure_bundle_plugin_manifest(tmp_path, "different-name")
    # Unchanged bytes — a regression that overwrote unconditionally would
    # silently drop the user's metadata.
    assert existing.read_text() == user_authored


def test_ensure_bundle_plugin_manifest_falls_back_to_basename(tmp_path: Path) -> None:
    """
    When ``agent_name`` is ``None`` the manifest name falls back to the
    bundle directory's basename — still deterministic, just less readable.
    """
    bundle = tmp_path / "my-bundle"
    bundle.mkdir()
    ensure_bundle_plugin_manifest(bundle, None)
    data = json.loads((bundle / ".claude-plugin" / "plugin.json").read_text())
    assert data["name"] == "my-bundle"


def _make_bundle_with_skill(root: Path) -> Path:
    """
    Create a minimal bundle dir containing one skill.

    :param root: Parent dir to create the bundle under.
    :returns: The bundle root path (contains ``skills/only/SKILL.md``).
    """
    bundle = root / "bundle"
    (bundle / "skills" / "only").mkdir(parents=True)
    (bundle / "skills" / "only" / "SKILL.md").write_text("# only\n")
    return bundle


@pytest.mark.parametrize(
    "skills_filter, expect_setting_sources",
    [
        # "all" → host skills via the CLI default; no explicit override.
        pytest.param("all", False, id="all"),
        # "none" → suppress host skills with empty setting-sources.
        pytest.param("none", True, id="none"),
        # list → like "all" for host sources (no per-name CLI allowlist);
        # bundle skills still load via --plugin-dir.
        pytest.param(["only"], False, id="list"),
    ],
)
def test_claude_native_skill_args_with_bundle(
    tmp_path: Path,
    skills_filter: str | list[str],
    expect_setting_sources: bool,
) -> None:
    """
    A bundle with ``skills/`` yields ``--plugin-dir <bundle>`` (the CLI
    plugin convention loads ``<bundle>/skills/<dir>/SKILL.md``) and a
    written manifest. ``--setting-sources ""`` appears only for ``"none"``
    — the SDK-parity gate on host skills.

    :param tmp_path: Pytest temp dir.
    :param skills_filter: The spec's ``skills_filter`` under test.
    :param expect_setting_sources: Whether ``--setting-sources`` should be
        emitted (only the ``"none"`` filter suppresses host skills).
    """
    bundle = _make_bundle_with_skill(tmp_path)
    args = claude_native_skill_args(bundle, agent_name="researcher", skills_filter=skills_filter)

    assert "--plugin-dir" in args
    assert args[args.index("--plugin-dir") + 1] == str(bundle)
    assert (tmp_path / "bundle" / ".claude-plugin" / "plugin.json").is_file()
    if expect_setting_sources:
        assert args[args.index("--setting-sources") + 1] == ""
    else:
        assert "--setting-sources" not in args


def test_claude_native_skill_args_no_bundle_is_empty() -> None:
    """
    With no bundle (the ``omnigent claude`` CLI path), no plugin args are
    produced under the default ``"all"`` filter — Claude launches with its
    own host config untouched.
    """
    assert claude_native_skill_args(None) == []


def test_claude_native_skill_args_bundle_without_skills_dir(tmp_path: Path) -> None:
    """
    A bundle that ships no ``skills/`` directory adds no ``--plugin-dir`` —
    a spurious empty plugin would make Claude Code warn/reject.
    """
    (tmp_path / "no_skills").mkdir()
    assert "--plugin-dir" not in claude_native_skill_args(tmp_path / "no_skills")


def test_claude_agents_skill_args_without_portable_skills_skips_native_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    native = tmp_path / ".claude" / "skills" / "native" / "SKILL.md"
    native.parent.mkdir(parents=True)
    native.write_text("---\nname: native\ndescription: Native skill\n---\nBody.\n")

    def unexpected_read(*args: object, **kwargs: object) -> str:
        raise AssertionError("A launch without portable skills must not reread native skills")

    monkeypatch.setattr(Path, "read_text", unexpected_read)

    assert claude_agents_skill_args(tmp_path / "bridge", (tmp_path,), "all") == []


@pytest.mark.parametrize(
    "skills_filter,expected",
    [("all", {"portable", "hidden"}), ("none", set()), (["portable"], {"portable"})],
)
def test_claude_agents_skill_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_filter: str | list[str],
    expected: set[str],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    workspace = tmp_path / "workspace"
    for name in ("portable", "hidden", "duplicate"):
        source = workspace / ".agents" / "skills" / f"source-{name}"
        source.mkdir(parents=True)
        hidden = "user-invocable: false\n" if name == "hidden" else ""
        (source / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n{hidden}---\nRead reference.txt.\n"
        )
        (source / "reference.txt").write_text(name)
    config = tmp_path / "claude-config"
    native = config / "skills" / "duplicate"
    native.mkdir(parents=True)
    (native / "SKILL.md").write_text(
        "---\nname: duplicate\ndescription: native\n---\nNative skill."
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    bridge = tmp_path / "bridge"
    # A relaunch must discard skills removed by a changed filter.
    claude_agents_skill_args(bridge, (workspace,), "all")
    args = claude_agents_skill_args(bridge, (workspace,), skills_filter)
    if not expected:
        assert args == []
        assert not (bridge / "agent-skills").exists()
        return
    assert args[0] == "--add-dir"
    linked = list((Path(args[1]) / ".claude" / "skills").iterdir())
    assert {path.name for path in linked} == expected
    assert {path.joinpath("reference.txt").read_text() for path in linked} == expected
    assert all((path / "SKILL.md").is_file() for path in linked)
    assert not (workspace / ".claude").exists()


def test_claude_agents_skill_args_ignores_unloaded_bundle_claude_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workspace, bundle = tmp_path / "workspace", tmp_path / "bundle"
    for source, name, content in (
        (workspace / ".agents", "shared", "Workspace skill"),
        (bundle / ".agents", "shared", "Shadowed bundle skill"),
        (bundle / ".agents", "fallback", "Bundle fallback"),
        (bundle / ".claude", "shared", "Not loaded by Claude"),
    ):
        skill = source / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: {name}\n---\n{content}\n")

    args = claude_agents_skill_args(tmp_path / "bridge", (workspace, bundle), "all")

    exposed = (Path(args[args.index("--add-dir") + 1]) / ".claude" / "skills").glob("*/SKILL.md")
    assert {path.read_text().splitlines()[-1] for path in exposed} == {
        "Workspace skill",
        "Bundle fallback",
    }


@pytest.mark.parametrize("workspace_is_bundle", [False, True])
def test_claude_agents_skill_args_keeps_bundle_discovery_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_is_bundle: bool
) -> None:
    from omnigent.spec.skill_sources import resolve_harness_skills, skill_source_context_from_env

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    bundle_parent = tmp_path / "bundles"
    bundle = bundle_parent / "agent"
    workspace = bundle if workspace_is_bundle else tmp_path / "workspace"
    for root, name in (
        (workspace, "workspace"),
        (bundle, "bundled"),
        (bundle_parent, "unrelated"),
    ):
        skill = root / ".agents" / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: {name}\n---\nLocal test skill.\n")

    roots = (workspace, bundle)
    args = claude_agents_skill_args(tmp_path / "bridge", roots, "all")
    exposed = Path(args[1]) / ".claude" / "skills"
    assert {path.name for path in exposed.iterdir()} == {"workspace", "bundled"}
    ctx = skill_source_context_from_env(roots=roots, harness="claude-native", bundle_dir=bundle)
    assert {skill.name for skill in resolve_harness_skills(ctx, "claude-native")} == {
        "workspace",
        "bundled",
    }
