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
    "skills_filter, include_workspace_settings, expect_setting_sources",
    [
        # "none" → suppress host skills with empty setting-sources, whatever
        # the workspace-settings gate says (the "none" branch wins).
        pytest.param("none", False, "", id="none-default"),
        pytest.param("none", True, "", id="none-workspace"),
        # "all" with the workspace-settings gate open → the CLI's default
        # sources; no explicit --setting-sources override.
        pytest.param("all", True, None, id="all-workspace"),
        # "all" with the gate closed (the default) → workspace-scoped sources
        # are dropped, keeping only the user scope.
        pytest.param("all", False, "user", id="all-default"),
        # list → like "all" for host sources (no per-name CLI allowlist);
        # bundle skills still load via --plugin-dir.
        pytest.param(["only"], True, None, id="list-workspace"),
        pytest.param(["only"], False, "user", id="list-default"),
    ],
)
def test_claude_native_skill_args_with_bundle(
    tmp_path: Path,
    skills_filter: str | list[str],
    include_workspace_settings: bool,
    expect_setting_sources: str | None,
) -> None:
    """
    A bundle with ``skills/`` yields ``--plugin-dir <bundle>`` (the CLI
    plugin convention loads ``<bundle>/skills/<dir>/SKILL.md``) and a
    written manifest. ``--setting-sources`` follows the SDK-parity host-skill
    gate *and* the workspace-settings gate: ``""`` suppresses host skills for
    ``"none"``; otherwise the default (``include_workspace_settings=False``)
    restricts sources to ``user`` and only ``include_workspace_settings=True``
    keeps the CLI's default sources.

    :param tmp_path: Pytest temp dir.
    :param skills_filter: The spec's ``skills_filter`` under test.
    :param include_workspace_settings: Whether workspace-scoped sources load.
    :param expect_setting_sources: Expected ``--setting-sources`` value, or
        ``None`` when the flag should be absent (the CLI's default sources).
    """
    bundle = _make_bundle_with_skill(tmp_path)
    args = claude_native_skill_args(
        bundle,
        agent_name="researcher",
        skills_filter=skills_filter,
        include_workspace_settings=include_workspace_settings,
    )

    assert "--plugin-dir" in args
    assert args[args.index("--plugin-dir") + 1] == str(bundle)
    assert (tmp_path / "bundle" / ".claude-plugin" / "plugin.json").is_file()
    if expect_setting_sources is None:
        assert "--setting-sources" not in args
    else:
        assert args[args.index("--setting-sources") + 1] == expect_setting_sources


def test_claude_native_skill_args_default_drops_workspace_settings(tmp_path: Path) -> None:
    """
    Security regression: with its default arguments (``skills_filter="all"``,
    ``include_workspace_settings=False``) the native launch restricts Claude's
    setting sources to ``user`` — never the workspace scope.

    A launch that pre-accepts Claude's workspace-trust dialog
    (``ensure_claude_workspace_trusted``) must not also let an unreviewed
    workspace's ``.claude/settings.json`` (e.g. a ``SessionStart`` hook) run
    at startup. Dropping the workspace-scoped sources is what prevents that,
    so this must hold for the *default* argument shape, not only when a caller
    opts in.
    """
    bundle = _make_bundle_with_skill(tmp_path)

    args = claude_native_skill_args(bundle, agent_name="researcher")

    assert "--setting-sources" in args
    assert args[args.index("--setting-sources") + 1] == "user"
    # No-bundle default path is likewise scoped to user only.
    assert claude_native_skill_args(None) == ["--setting-sources", "user"]


def test_claude_native_skill_args_no_bundle_is_empty() -> None:
    """
    With no bundle (the ``omnigent claude`` CLI path) and the workspace-trust
    gate intact (``include_workspace_settings=True``), no plugin args are
    produced under the default ``"all"`` filter — Claude launches with its
    own host config untouched, its own trust dialog still gating workspace
    settings. This is the local wrapper's contract.
    """
    assert claude_native_skill_args(None, include_workspace_settings=True) == []


def test_claude_native_skill_args_bundle_without_skills_dir(tmp_path: Path) -> None:
    """
    A bundle that ships no ``skills/`` directory adds no ``--plugin-dir`` —
    a spurious empty plugin would make Claude Code warn/reject.
    """
    (tmp_path / "no_skills").mkdir()
    assert "--plugin-dir" not in claude_native_skill_args(tmp_path / "no_skills")
