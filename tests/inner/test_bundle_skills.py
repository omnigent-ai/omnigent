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
    bundle_plugin_name,
    bundle_skill_names,
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


def _write_skill(bundle: Path, dir_name: str, *, name: str, description: str = "desc") -> None:
    """Write ``<bundle>/skills/<dir_name>/SKILL.md`` with the given frontmatter."""
    skill_dir = bundle / "skills" / dir_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n"
    )


def test_bundle_skill_names_reads_frontmatter_name(tmp_path: Path) -> None:
    """
    The returned name comes from the frontmatter ``name`` field, not the
    directory name — they may differ, per ``SkillSpec``'s own docstring.
    """
    _write_skill(tmp_path, "on-disk-dir", name="feature-brainstorming")
    assert bundle_skill_names(tmp_path) == ["feature-brainstorming"]


def test_bundle_skill_names_multiple_skills_sorted_by_dir(tmp_path: Path) -> None:
    """Multiple skills are all returned, in directory-listing (sorted) order."""
    _write_skill(tmp_path, "b-dir", name="skill-b")
    _write_skill(tmp_path, "a-dir", name="skill-a")
    assert bundle_skill_names(tmp_path) == ["skill-a", "skill-b"]


def test_bundle_skill_names_no_skills_dir_returns_empty(tmp_path: Path) -> None:
    """A bundle with no ``skills/`` directory at all returns ``[]``, not an error."""
    assert bundle_skill_names(tmp_path) == []


def test_bundle_skill_names_empty_skills_dir_returns_empty(tmp_path: Path) -> None:
    """An empty ``skills/`` directory (no subdirectories) returns ``[]``."""
    (tmp_path / "skills").mkdir()
    assert bundle_skill_names(tmp_path) == []


def test_bundle_skill_names_skips_missing_frontmatter(tmp_path: Path) -> None:
    """
    A ``SKILL.md`` with no ``---`` frontmatter delimiters is skipped, not
    raised — this is a best-effort read, not spec validation (the parser
    already validated strictly before the bundle ever reached this code).
    """
    skill_dir = tmp_path / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Just plain text, no frontmatter.\n")
    assert bundle_skill_names(tmp_path) == []


def test_bundle_skill_names_skips_missing_name_field(tmp_path: Path) -> None:
    """Frontmatter present but missing the required ``name`` key is skipped."""
    skill_dir = tmp_path / "skills" / "no-name"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: no name here\n---\n\nBody.\n")
    assert bundle_skill_names(tmp_path) == []


def test_bundle_skill_names_skips_invalid_yaml(tmp_path: Path) -> None:
    """Malformed YAML frontmatter is skipped rather than raising."""
    skill_dir = tmp_path / "skills" / "bad-yaml"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: [unclosed\n---\n\nBody.\n")
    assert bundle_skill_names(tmp_path) == []


def test_bundle_skill_names_partial_failure_keeps_valid_ones(tmp_path: Path) -> None:
    """One broken SKILL.md doesn't take down the whole bundle's skill list."""
    _write_skill(tmp_path, "good", name="good-skill")
    broken_dir = tmp_path / "skills" / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "SKILL.md").write_text("no frontmatter here\n")
    assert bundle_skill_names(tmp_path) == ["good-skill"]


def test_bundle_plugin_name_prefers_manifest_name(tmp_path: Path) -> None:
    """
    A user-authored manifest whose ``name`` differs from the agent's
    display name wins: the CLI labels plugin skills by the manifest, so
    the ``<plugin>:<skill>`` allowlist entries must use that name or the
    qualified form never matches.
    """
    manifest_dir = tmp_path / ".claude-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text(json.dumps({"name": "custom-plugin"}))
    assert bundle_plugin_name(tmp_path, "display-name") == "custom-plugin"


def test_bundle_plugin_name_falls_back_to_agent_name(tmp_path: Path) -> None:
    """No manifest present: fall back to the agent display name."""
    assert bundle_plugin_name(tmp_path, "display-name") == "display-name"


def test_bundle_plugin_name_falls_back_to_basename_without_agent(tmp_path: Path) -> None:
    """No manifest and no agent name: fall back to the bundle basename."""
    assert bundle_plugin_name(tmp_path, None) == tmp_path.name


def test_bundle_plugin_name_ignores_malformed_manifest(tmp_path: Path) -> None:
    """A manifest that is not JSON (or has no usable name) is skipped."""
    manifest_dir = tmp_path / ".claude-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text("{not json")
    assert bundle_plugin_name(tmp_path, "display-name") == "display-name"
    (manifest_dir / "plugin.json").write_text(json.dumps({"name": ""}))
    assert bundle_plugin_name(tmp_path, "display-name") == "display-name"
    (manifest_dir / "plugin.json").write_text(json.dumps(["not", "a", "dict"]))
    assert bundle_plugin_name(tmp_path, None) == tmp_path.name


def test_frontmatter_regex_matches_spec_parser() -> None:
    """
    The tolerant reader's frontmatter regex is a deliberate copy of the
    spec parser's; if the parser's grammar ever changes, this copy must
    follow or the two would disagree on which SKILL.md files parse.
    """
    from omnigent.inner import bundle_skills
    from omnigent.spec import parser

    assert bundle_skills._FRONTMATTER_RE.pattern == parser._FRONTMATTER_RE.pattern
    assert bundle_skills._FRONTMATTER_RE.flags == parser._FRONTMATTER_RE.flags
