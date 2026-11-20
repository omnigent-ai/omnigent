"""
Expose bundle and portable skills to Claude harnesses.

Both SDK and native Claude load bundled skills as plugins. Native Claude
also loads portable ``.agents`` skills through a session-owned additional
directory, preserving their bare command names and supporting files.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
from dataclasses import replace
from pathlib import Path

from omnigent.spec.skill_sources import (
    _claude_code_skills,
    select_claude_portable_skills,
    skill_source_context_from_env,
)

_log = logging.getLogger(__name__)


def _copy_contained_skill(
    skills_root: Path,
    root_fd: int,
    relative: Path,
    destination: Path,
    ancestors: frozenset[tuple[int, int]] = frozenset(),
) -> None:
    """Materialize a skill entry using only descriptors beneath the pinned root."""
    try:
        resolved = (skills_root / relative).resolve(strict=True).relative_to(skills_root)
    except (ValueError, RuntimeError) as exc:
        raise ValueError("Skill symlinks must stay inside the skills tree without cycles") from exc

    source_fd = os.dup(root_fd)
    try:
        for index, component in enumerate(resolved.parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(resolved.parts) - 1:
                flags |= os.O_DIRECTORY
            info = os.stat(component, dir_fd=source_fd, follow_symlinks=False)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("Skills may contain only regular files and directories")
            next_fd = os.open(component, flags, dir_fd=source_fd)
            os.close(source_fd)
            source_fd = next_fd
        info = os.fstat(source_fd)
        if stat.S_ISDIR(info.st_mode):
            identity = (info.st_dev, info.st_ino)
            if identity in ancestors:
                raise ValueError("Skill symlinks must not create directory cycles")
            destination.mkdir(mode=0o700)
            for name in os.listdir(source_fd):
                _copy_contained_skill(
                    skills_root,
                    root_fd,
                    resolved / name,
                    destination / name,
                    ancestors | {identity},
                )
        elif stat.S_ISREG(info.st_mode):
            # Any extra link could expose bundle configuration outside skills/.
            if info.st_nlink != 1:
                raise ValueError("Skill source files must not be hard-linked")
            with os.fdopen(os.dup(source_fd), "rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
            destination.chmod(info.st_mode & 0o777)
        else:
            raise ValueError("Skills may contain only regular files and directories")
    finally:
        os.close(source_fd)


def stage_contained_bundle_skills(
    bundle_dir: Path, destination: Path, agent_name: str | None
) -> Path:
    """Copy only bundled skills and a generated manifest into a fresh private plugin.

    The destination's parent must be owned by the trusted runtime. Internal
    symlinks are materialized; escaping links, root skills links, special files,
    and multiply linked files are rejected. Script execute permissions survive.
    The caller must separately prevent sandbox access to original bundle config.
    """
    bundle_root = bundle_dir.resolve(strict=True)
    if destination.resolve().is_relative_to(bundle_root):
        raise ValueError("Contained skill destination must stay outside the original bundle")
    skills_root = bundle_root / "skills"
    if skills_root.is_symlink():
        raise ValueError("The bundle skills root must not be a symlink")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("Contained bundle skills require directory-relative no-follow file access")

    destination.mkdir(mode=0o700, parents=True)
    try:
        try:
            root_fd = os.open(skills_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            root_fd = None
        if root_fd is not None:
            try:
                _copy_contained_skill(skills_root, root_fd, Path(), destination / "skills")
            finally:
                os.close(root_fd)
        ensure_bundle_plugin_manifest(destination, agent_name or bundle_dir.name)
    except BaseException:
        _remove_skill_overlay(destination)
        raise
    return destination


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


def _remove_skill_overlay(path: Path) -> None:
    """Remove owned copies, including read-only files, without following skill links."""
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    path.chmod(path.stat().st_mode | 0o700)
    for root, dirs, files in os.walk(path, followlinks=False):
        for names, owner_permissions in ((dirs, 0o700), (files, 0o600)):
            for name in names:
                entry = Path(root) / name
                if not entry.is_symlink():
                    entry.chmod(entry.stat().st_mode | owner_permissions)
    shutil.rmtree(path)


def claude_agents_skill_overlay(bridge_dir: Path) -> Path:
    """Return the session-owned directory :func:`claude_agents_skill_args` adds.

    :param bridge_dir: Session-owned bridge directory.
    :returns: The overlay path, e.g. ``bridge_dir / "agent-skills"``.
    """
    return bridge_dir / "agent-skills"


def claude_agents_skill_args(
    bridge_dir: Path,
    roots: tuple[Path, ...],
    skills_filter: str | list[str],
) -> list[str]:
    """Expose portable skills through Claude's additional-directory discovery.

    :param bridge_dir: Session-owned directory for the skill links.
    :param roots: Workspace and optional bundle discovery roots, in priority order.
    :param skills_filter: Host skill selection from the agent spec.
    :returns: Claude CLI arguments loading the selected portable skills.
    """
    overlay = claude_agents_skill_overlay(bridge_dir)
    _remove_skill_overlay(overlay)
    ctx = skill_source_context_from_env(
        roots=roots,
        harness="claude-native",
        skills_filter=skills_filter,
        bundle_dir=roots[1] if len(roots) > 1 else None,
    )
    skills = _claude_code_skills(ctx, ".agents")
    if not skills:
        return []
    skills = select_claude_portable_skills(
        skills, _claude_code_skills(replace(ctx, is_native=True, skills_filter="all"))
    )
    if not skills:
        return []
    target = overlay / ".claude" / "skills"
    target.mkdir(parents=True)
    staged = False
    for skill in skills:
        if skill.skill_dir is None:
            continue
        # Claude uses the link's directory basename as the command name.
        destination = target / skill.name
        try:
            destination.symlink_to(skill.skill_dir.resolve(), target_is_directory=True)
        except FileExistsError:
            _log.warning("Skipping portable skill %s: destination already exists", skill.skill_dir)
            continue
        except OSError:
            # Windows may require privileges to create directory symlinks.
            try:
                shutil.copytree(skill.skill_dir, destination, symlinks=True)
            except OSError as exc:
                if not isinstance(exc, FileExistsError):
                    _remove_skill_overlay(destination)
                _log.warning("Skipping portable skill %s: %s", skill.skill_dir, exc)
                continue
        staged = True
    return ["--add-dir", str(overlay)] if staged else []
