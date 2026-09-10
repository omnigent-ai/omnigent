"""
Shared helpers for exposing an agent bundle's skills to a Claude harness.

Both the Claude Agent SDK executor (in-process, ``claude_sdk_executor``)
and the ``claude-native`` CLI launch path expose a bundle's
``skills/<dir>/SKILL.md`` files to Claude Code through its plugin
convention (``--plugin-dir <bundle>``). This module centralizes the two
pieces that wiring needs so the SDK and native paths stay in lockstep:
writing the bundle's ``.claude-plugin/plugin.json`` manifest, and
translating the spec's ``skills_filter`` into the Claude Code CLI args
(``--plugin-dir`` + ``--setting-sources``) that the native path passes
to the real ``claude`` binary.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

# Matches ``_FRONTMATTER_RE`` in ``omnigent.spec.parser`` — kept as a
# separate copy here (rather than importing the parser's private regex)
# since this module intentionally stays a lightweight, best-effort reader:
# by the time a bundle reaches a harness it already passed the parser's
# strict validation, so a skill directory that fails this tolerant read
# is skipped rather than treated as an error.
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


def ensure_bundle_plugin_manifest(
    bundle_dir: Path,
    agent_name: str | None,
) -> None:
    """
    Write a minimal ``<bundle>/.claude-plugin/plugin.json`` manifest
    when one isn't already present.

    Idempotent — if the file already exists (including with a
    user-supplied richer manifest), it's left untouched. The
    manifest gives the bundle a stable plugin name so Claude's
    skill listing labels bundled skills as
    ``<agent-name>:<skill-name>`` instead of falling back to the
    bundle's auto-generated tmp-dir basename
    (e.g. ``omnigent-ap-chat-x9p606iz/bundle:researcher``).

    :param bundle_dir: Materialized bundle root; the manifest is
        written at ``<bundle_dir>/.claude-plugin/plugin.json``.
    :param agent_name: Display name for the plugin. ``None`` falls
        back to the bundle directory's basename — still
        deterministic, just less readable.
    :returns: None.
    """
    manifest_dir = bundle_dir / ".claude-plugin"
    manifest_path = manifest_dir / "plugin.json"
    if manifest_path.exists():
        return
    manifest_dir.mkdir(parents=True, exist_ok=True)
    name = agent_name or bundle_dir.name
    manifest_path.write_text(
        json.dumps(
            {
                "name": name,
                "description": f"Bundled skills for omnigent agent {name!r}",
            },
            indent=2,
        )
        + "\n",
    )


def bundle_skill_names(bundle_dir: Path) -> list[str]:
    """
    Read the ``name`` frontmatter field out of each of the bundle's
    ``skills/<dir>/SKILL.md`` files.

    Used to seed the SDK's ``skills`` allowlist with a bundle's own
    skills when ``skills_filter: none`` would otherwise leave them
    listed (via ``--plugin-dir``) but unable to actually invoke the
    native ``Skill`` tool — an empty allowlist blocks every skill,
    bundled or host.

    Deliberately tolerant: a bundle only reaches a harness after the
    spec parser already validated every ``SKILL.md`` strictly, so a
    directory that fails this lightweight re-read (missing file, bad
    YAML, no ``name`` key) is skipped rather than raised — this is a
    best-effort enrichment at turn time, not spec validation.

    :param bundle_dir: Materialized agent-bundle root.
    :returns: Skill names in directory-listing order, e.g.
        ``["code-review", "feature-brainstorming"]``. Empty when the
        bundle has no ``skills/`` directory or no readable skills.
    """
    skills_dir = bundle_dir / "skills"
    if not skills_dir.is_dir():
        return []
    names: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        match = _FRONTMATTER_RE.match(text)
        if not match:
            continue
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(frontmatter, dict):
            continue
        name = frontmatter.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def bundle_plugin_name(bundle_dir: Path, agent_name: str | None) -> str:
    """
    Resolve the plugin name Claude Code will label this bundle's skills
    with (the ``<plugin>`` half of ``<plugin>:<skill>``).

    Prefers the ``name`` recorded in the bundle's own
    ``.claude-plugin/plugin.json`` — a user-supplied manifest may carry a
    name that differs from the agent's display name, and the CLI labels
    skills by the manifest, not the agent. Falls back to the same
    ``agent_name or bundle_dir.name`` default that
    :func:`ensure_bundle_plugin_manifest` writes, so both agree when the
    manifest was auto-generated (or is unreadable).

    :param bundle_dir: Materialized agent-bundle root.
    :param agent_name: Agent display name, or ``None``.
    :returns: The plugin name to build ``<plugin>:<skill>`` entries with.
    """
    manifest_path = bundle_dir / ".claude-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        manifest = None
    if isinstance(manifest, dict):
        name = manifest.get("name")
        if isinstance(name, str) and name:
            return name
    return agent_name or bundle_dir.name


def claude_native_skill_args(
    bundle_dir: Path | None,
    *,
    agent_name: str | None = None,
    skills_filter: str | list[str] = "all",
) -> list[str]:
    """
    Build the ``claude`` CLI args that expose bundle + host skills.

    This is the native-CLI mirror of the SDK's
    ``_resolve_skills_option`` + plugin wiring in
    ``claude_sdk_executor``. The real ``claude`` binary discovers a
    bundle's ``skills/<dir>/SKILL.md`` files as plugin skills when the
    bundle is passed via ``--plugin-dir``, and gates host skills
    (``~/.claude/skills/``, project ``.claude/skills/``) via
    ``--setting-sources``. ``skills_filter`` maps the same way the SDK
    maps it onto ``setting_sources`` (matching the wrapped variants):

    - ``"all"`` → host skills included (the CLI's default setting
      sources), so no ``--setting-sources`` is emitted.
    - ``"none"`` → ``--setting-sources ""`` suppresses host-skill
      discovery; bundle skills loaded via ``--plugin-dir`` are
      unaffected and remain visible. Unlike the SDK path, no allowlist
      seeding is needed here: the CLI has no per-name allowlist flag,
      so nothing blocks the plugin skills (the SDK's empty ``skills``
      list is what the executor's seeding works around).
    - ``list[str]`` → treated like ``"all"`` for host sources (the SDK
      uses ``setting_sources=None`` for the list case). The CLI has no
      per-name skill allowlist flag, so the named subset is not
      enforced on native — bundle skills load via ``--plugin-dir`` and
      host skills follow the default sources.

    ``--plugin-dir`` is emitted only when ``bundle_dir`` actually
    contains a ``skills/`` directory, so agents that ship no bundled
    skills add no plugin args (and ``omnigent claude``'s minimal
    spec, which has no bundle, passes ``bundle_dir=None``).

    :param bundle_dir: Materialized agent-bundle root, or ``None`` when
        the launch has no bundle (e.g. the ``omnigent claude`` CLI
        running against the user's own ``~/.claude`` config).
    :param agent_name: Agent display name for the plugin manifest, e.g.
        ``"researcher"``. ``None`` falls back to the bundle basename.
    :param skills_filter: The spec's ``skills_filter``: ``"all"`` /
        ``"none"`` / a list of skill names. Defaults to ``"all"``.
    :returns: CLI args to append after ``claude`` (possibly empty),
        e.g. ``["--plugin-dir", "/tmp/bundle", "--setting-sources", ""]``.
    """
    args: list[str] = []
    if bundle_dir is not None and (bundle_dir / "skills").is_dir():
        ensure_bundle_plugin_manifest(bundle_dir, agent_name)
        args.extend(["--plugin-dir", str(bundle_dir)])
    if skills_filter == "none":
        # Empty setting sources suppress host-skill discovery. Bundle
        # skills ride --plugin-dir and are unaffected.
        args.extend(["--setting-sources", ""])
    return args
