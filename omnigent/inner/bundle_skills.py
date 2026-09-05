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
from pathlib import Path


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
      unaffected and remain visible.
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
