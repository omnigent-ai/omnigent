"""
Harness-aware host/plugin skill discovery for the web composer's
slash-command menu.

The runner's ``_resolve_session_skills`` unions a session's bundled
skills with the *extra* skills its harness surfaces in its own terminal.
"What a harness exposes" differs per vendor (Claude Code plugins, Codex
``~/.codex/skills``, Cursor ``~/.cursor``), so resolution dispatches to a
per-family provider. Unknown harnesses fall back to the generic host walk
(``discover_host_skills``) for backwards compatibility.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from omnigent.errors import OmnigentError
from omnigent.spec.parser import _discover_skills, _parse_skill, discover_host_skills
from omnigent.spec.types import SkillSpec

_log = logging.getLogger(__name__)

_SKILL_FAMILIES = frozenset({"claude", "codex", "cursor", "pi", "antigravity"})

# The bare ``antigravity`` harness is the in-process Gemini SDK executor, NOT the
# agy CLI. It never launches agy, so ~/.gemini plugin/builtin skills are not its
# skills — it keeps the generic walk, whose skills omnigent resolves+injects.
_ANTIGRAVITY_SDK_HARNESS = "antigravity"


def _harness_family(harness: str | None) -> str | None:
    """
    Map any harness spelling to its vendor family, or ``None``.

    Collapses variant suffixes/prefixes — ``claude-sdk``,
    ``claude-native`` and ``native-claude`` all map to ``"claude"`` —
    so the provider registry can be keyed by family rather than by every
    canonical variant.

    :param harness: A harness id (canonical or alias), e.g.
        ``"codex-native"``; ``None`` or ``""`` returns ``None``.
    :returns: One of ``"claude"``, ``"codex"``, ``"cursor"``, ``"pi"``,
        or ``None`` for unknown/other harnesses.
    """
    if not harness:
        return None
    # Normalize the underscore executor-type spelling (``claude_sdk``,
    # ``agents_sdk``) to the hyphen form before splitting — the in-process
    # SDK harness flows in as ``claude_sdk`` (canonicalize_harness leaves it
    # unchanged), and without this it would miss the ``claude`` family and
    # silently lose plugin slash-commands.
    normalized = harness.replace("_", "-")
    parts = normalized.split("-")
    base = parts[1] if parts[0] == "native" and len(parts) > 1 else parts[0]
    if base not in _SKILL_FAMILIES:
        return None
    # Unlike claude (where SDK and native share ~/.claude), antigravity's two
    # harnesses are different runtimes: only the agy CLI reads ~/.gemini.
    if base == _ANTIGRAVITY_SDK_HARNESS and normalized == _ANTIGRAVITY_SDK_HARNESS:
        return None
    return base


@dataclass(frozen=True)
class SkillSourceContext:
    """
    Inputs a per-harness skill provider needs.

    :param roots: Host-discovery roots in priority order — the session
        workspace, then the agent bundle workdir (the same roots
        ``_resolve_session_skills`` already computes).
    :param home: The user home directory (``Path.home()``); injected so
        tests can pin it.
    :param skills_filter: The spec's ``skills:`` filter
        (``"all"`` / ``"none"`` / list of names).
    :param bundle_dir: The materialized bundle root, or ``None``.
    """

    roots: tuple[Path, ...]
    home: Path
    skills_filter: str | list[str]
    bundle_dir: Path | None


SkillSource = Callable[[SkillSourceContext], list[SkillSpec]]


def _dedup(specs: list[SkillSpec]) -> list[SkillSpec]:
    """Return *specs* with later same-name entries dropped (first wins)."""
    seen: set[str] = set()
    out: list[SkillSpec] = []
    for s in specs:
        if s.name in seen:
            continue
        seen.add(s.name)
        out.append(s)
    return out


def _generic_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """Today's behavior: ``discover_host_skills`` over each root."""
    out: list[SkillSpec] = []
    for root in ctx.roots:
        out.extend(discover_host_skills(root, ctx.skills_filter))
    return _dedup(out)


def resolve_harness_skills(ctx: SkillSourceContext, harness: str | None) -> list[SkillSpec]:
    """
    Return the extra (non-bundled) skills the session's harness exposes.

    Dispatches by harness family. An unknown/other family falls back to
    the generic host walk so behavior is unchanged for harnesses without
    a dedicated provider.

    :param ctx: Session discovery context.
    :param harness: The session's harness id (canonical or alias).
    :returns: Deduplicated skill specs (first occurrence wins).
        Skills marked ``user-invocable: false`` are excluded — they are
        internal orchestration skills, not user-typeable slash commands
        (applied uniformly across every harness).
    """
    family = _harness_family(harness)
    provider = _SKILL_SOURCES.get(family, _generic_host_skills)
    return [s for s in _dedup(provider(ctx)) if s.user_invocable]


def _read_json(path: Path) -> dict[str, object] | None:
    """Best-effort JSON read; ``None`` unless it is a string-keyed object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        return None
    return cast(dict[str, object], data)


def _enabled_plugin_settings_files(ctx: SkillSourceContext) -> list[Path]:
    """
    Settings files carrying ``enabledPlugins``, weakest→strongest.

    Claude Code merges ``enabledPlugins`` across its settings tiers, and
    the gitignored ``settings.local.json`` (the "local" tier) wins over
    the shared ``settings.json`` within a scope. Reading only
    ``settings.json`` would miss a plugin toggled in the local file. We
    approximate Claude's precedence for the menu (last-wins):

        user (home) .json → .local  <  bundle .json → .local
            <  workspace .json → .local

    — ``.local`` over ``.json`` within each scope, project (roots) over
    user (home), and the primary workspace (``roots[0]``) over the shipped
    bundle (``roots[1]``), matching the workspace-first precedence the host
    skill walk already uses.

    :param ctx: Session discovery context.
    :returns: Candidate settings paths in increasing precedence order.
    """
    files: list[Path] = []
    # ``reversed`` puts the primary workspace (roots[0]) last → strongest.
    for scope in (ctx.home, *reversed(ctx.roots)):
        files.append(scope / ".claude" / "settings.json")
        files.append(scope / ".claude" / "settings.local.json")
    return files


def _managed_plugin_keys(ctx: SkillSourceContext) -> set[str]:
    """
    Plugin keys force-enabled by Claude Code's managed (policy) tier.

    ``~/.claude/plugins/managed_plugins.json`` carries a ``managed_plugins``
    list of ``<plugin>@<marketplace>`` keys that an organization's policy
    force-installs. Claude Code treats the managed tier as the highest
    precedence — it cannot be overridden by a user/project ``enabledPlugins``
    toggle — so a managed plugin is enabled even when it carries no
    ``enabledPlugins`` entry (or an explicit ``false``) in any settings file.

    :param ctx: Session discovery context.
    :returns: The set of managed ``<plugin>@<marketplace>`` keys, empty when
        the file is absent, unreadable, or malformed.
    """
    data = _read_json(ctx.home / ".claude" / "plugins" / "managed_plugins.json")
    if data is None:
        return set()
    managed = data.get("managed_plugins")
    if not isinstance(managed, list):
        return set()
    return {key for key in managed if isinstance(key, str) and key}


def _enabled_plugin_keys(ctx: SkillSourceContext) -> set[str]:
    """
    Resolve which plugins are enabled, honoring scope + local precedence
    and the managed (policy) tier.

    Merges ``enabledPlugins`` across the settings files from
    :func:`_enabled_plugin_settings_files` in increasing-precedence order
    (last-wins per key), so a plugin enabled globally but explicitly
    disabled in a project's ``settings.local.json`` is correctly excluded
    (and vice-versa). The managed tier (:func:`_managed_plugin_keys`) is then
    unioned on top: it force-enables at the highest precedence and so cannot
    be overridden by a settings disable.

    :param ctx: Session discovery context.
    :returns: The set of ``<plugin>@<marketplace>`` keys whose effective
        enablement is truthy, unioned with the managed (policy) keys.
    """
    state: dict[str, bool] = {}
    for path in _enabled_plugin_settings_files(ctx):
        data = _read_json(path)
        if data is None:
            continue
        ep = data.get("enabledPlugins")
        if isinstance(ep, dict):
            for key, value in ep.items():
                # Accept only a real JSON boolean — a string ``"false"`` is
                # truthy under ``bool()`` and would wrongly enable a plugin.
                # Non-bool values are ignored (they don't override a prior
                # real bool from a weaker-precedence tier).
                if isinstance(value, bool):
                    state[key] = value
    enabled = {key for key, on in state.items() if on}
    # Managed (policy) plugins are force-enabled at the highest precedence and
    # cannot be overridden by a user/project enabledPlugins toggle, so union
    # them on top of the settings-derived set (force-enable, never disable).
    return enabled | _managed_plugin_keys(ctx)


def _plugin_install_paths(ctx: SkillSourceContext, enabled: set[str]) -> dict[str, Path]:
    """Map ``<plugin>@<marketplace>`` → installPath for enabled+installed plugins."""
    data = _read_json(ctx.home / ".claude" / "plugins" / "installed_plugins.json")
    if data is None:
        return {}
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    # installPath values from installed_plugins.json are trusted only within
    # the Claude plugins cache root; anything pointing elsewhere is logged and
    # skipped so a tampered/odd manifest can't turn an arbitrary directory into
    # a discovery root.
    plugins_root = (ctx.home / ".claude" / "plugins").resolve()
    out: dict[str, Path] = {}
    for key, entries in plugins.items():
        if key not in enabled or not isinstance(entries, list):
            continue
        # A plugin may have multiple scope entries (user/project); take the
        # first one carrying a usable installPath rather than assuming it's
        # entries[0], whose scope may not have an installed path.
        for entry in entries:
            path = entry.get("installPath") if isinstance(entry, dict) else None
            if not (isinstance(path, str) and path):
                continue
            # A relative installPath is resolved against the plugins root
            # (its only sensible base), never the runner's cwd.
            path_obj = Path(path)
            resolved = (path_obj if path_obj.is_absolute() else plugins_root / path_obj).resolve()
            if not resolved.is_relative_to(plugins_root):
                _log.warning(
                    "Skipping plugin %r: installPath %r is outside %s",
                    key,
                    path,
                    plugins_root,
                )
                break
            out[key] = resolved
            break
    return out


def _claude_plugin_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """
    Enabled Claude Code plugin skills, namespaced ``<plugin>:<skill>``.

    Plugin skills are host skills, so they obey the spec's
    ``skills_filter`` exactly as :func:`discover_host_skills` does:
    ``"none"`` suppresses them entirely (hermetic), ``"all"`` surfaces
    every skill from every enabled plugin, and a list selects by the
    skill's own (bare) name — matching how the filter names skills,
    independent of the display namespace.
    """
    if ctx.skills_filter == "none":
        return []
    filter_names: set[str] | None = (
        set(ctx.skills_filter) if isinstance(ctx.skills_filter, list) else None
    )
    enabled = _enabled_plugin_keys(ctx)
    if not enabled:
        return []
    out: list[SkillSpec] = []
    for key, install_path in _plugin_install_paths(ctx, enabled).items():
        plugin = key.split("@", 1)[0]
        skipped: list[str] = []
        for spec in _discover_skills(install_path / "skills", skipped=skipped):
            if filter_names is not None and spec.name not in filter_names:
                continue
            out.append(replace(spec, name=f"{plugin}:{spec.name}"))
        # Surface dropped skills with the plugin key so a missing command is
        # diagnosable rather than silently absent.
        for detail in skipped:
            _log.warning("Plugin %r: skipped skill: %s", key, detail)
    return out


def claude_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """Generic host walk (``~/.claude/skills`` etc.) plus enabled plugins."""
    return _generic_host_skills(ctx) + _claude_plugin_skills(ctx)


def codex_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """
    Codex skills: ``<bundle>/skills`` + ``~/.codex/skills`` under the filter.

    Reuses the Codex executor's own helpers — ``codex_skill_sources`` (the
    shared source-list builder) and ``select_codex_skill_dirs`` (the shared
    selector) — so the menu draws from the same roots and selection the
    executor symlinks into ``$CODEX_HOME/skills/``. The menu is the subset of
    that selection whose ``SKILL.md`` parses: the executor links by existence,
    so a present-but-unparseable skill is linked but not shown (correct — Codex
    won't register a malformed skill as a command either).

    Names are surfaced by **directory name** (the selector's key), not the
    frontmatter ``name``. Codex registers a skill's slash command under its
    directory, and the executor symlinks under that dir name — so when the
    two differ, the menu label must match the directory (mirrors the Cursor
    provider). The lazy import keeps the Codex-specific dependency out of
    ``omnigent.spec``'s module-load path.
    """
    from omnigent.inner.codex_executor import codex_skill_sources, select_codex_skill_dirs

    sources = codex_skill_sources(ctx.bundle_dir, ctx.home)
    out: list[SkillSpec] = []
    for name, skill_dir in select_codex_skill_dirs(ctx.skills_filter, sources).items():
        try:
            spec = _parse_skill(skill_dir / "SKILL.md")
        except (OmnigentError, OSError):  # best-effort discovery
            continue
        out.append(replace(spec, name=name))
    return out


def cursor_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """
    Cursor skills under ``~/.cursor/skills`` (the ambient real home a
    cursor-native session runs against).

    Cursor names a skill by its ``plugin--skill`` *directory* (e.g.
    ``fe-epl-tools--metric-view-adoption``), not the bare frontmatter
    ``name`` — the dir carries the namespace and is collision-safe across
    the many installed plugins. So this surfaces the dir name, overriding
    the parsed bare name. Honors ``skills_filter`` (``"none"`` hermetic,
    ``"all"`` everything, a list selecting by the surfaced dir name).
    Only ``~/.cursor/skills`` is scanned; ``skills-cursor`` (Cursor's own
    managed built-ins) is left out until confirmed runnable as a command.
    """
    if ctx.skills_filter == "none":
        return []
    filter_names: set[str] | None = (
        set(ctx.skills_filter) if isinstance(ctx.skills_filter, list) else None
    )
    skills_dir = ctx.home / ".cursor" / "skills"
    if not skills_dir.is_dir():
        return []
    try:
        children = sorted(skills_dir.iterdir())
    except OSError as exc:
        # An unreadable skills dir must not 500 the /skills endpoint (matches
        # the lenient codex/_discover_skills behavior) — log and yield nothing.
        _log.warning("Skipping unreadable cursor skills dir %s: %s", skills_dir, exc)
        return []
    out: list[SkillSpec] = []
    for child in children:
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        if filter_names is not None and child.name not in filter_names:
            continue
        try:
            spec = _parse_skill(child / "SKILL.md")
        except (OmnigentError, OSError):  # best-effort discovery
            continue
        out.append(replace(spec, name=child.name))
    return out


def _agy_skill_dirs(ctx: SkillSourceContext) -> list[tuple[Path, str | None]]:
    """
    agy's skill directories with their namespace, in discovery order.

    The five sources agy's own ``/skills`` panel lists (live-verified, agy
    1.1.9 — the panel prints these paths verbatim)::

        <workspace>/.agents/skills/<skill>/SKILL.md          "Workspace"
        ~/.gemini/antigravity-cli/skills/<skill>/SKILL.md    "Global"
        ~/.gemini/skills/<skill>/SKILL.md                    "Shared"
        ~/.gemini/config/plugins/<plugin>/skills/<skill>/…   imported plugins
        ~/.gemini/antigravity-cli/builtin/skills/<skill>/…   shipped builtins

    Only plugin skills are namespaced — agy registers them as
    ``<plugin>:<skill>`` and its TUI accepts only that spelling; every other
    source is bare. The workspace source is ``.agents/skills`` (vendor-neutral),
    NOT ``.claude/skills``: that is the one directory the old generic fallback
    got right for agy.

    A plugin is ENABLED iff it still carries ``plugin.json``: ``agy plugin
    disable`` renames that file to ``plugin.json.disabled`` and changes nothing
    else — ``config/import_manifest.json`` keeps listing the plugin (so does
    ``agy plugin list``) and the ``skills/`` tree stays on disk. The manifest is
    therefore NOT a usable enabled-signal; the manifest file's name is.

    :param ctx: Session discovery context. ``home`` is the real user home, which
        is truthful for every source above even though agy runs under a
        bridge-owned ``--gemini_dir``: the plugin, Global and Shared trees are
        linked back to the real home by ``_seed_isolated_agy_plugins`` /
        ``_seed_isolated_agy_skills``; the workspace tree is not under the Gemini
        dir at all; and agy recreates its builtins in whatever Gemini dir it is
        launched with. Anything listed here therefore resolves in-session — a
        menu entry agy could not expand would just fail, since a native session
        sends ``/name`` to the CLI as plaintext.
    :returns: ``(directory, namespace)`` pairs; ``namespace`` is ``None`` for
        every source except plugins.
    """
    gemini = ctx.home / ".gemini"
    dirs: list[tuple[Path, str | None]] = []
    for root in ctx.roots:
        workspace_skills = root / ".agents" / "skills"
        if workspace_skills.is_dir():
            dirs.append((workspace_skills, None))
    for bare in (
        gemini / "antigravity-cli" / "skills",
        gemini / "skills",
    ):
        if bare.is_dir():
            dirs.append((bare, None))
    plugins_root = gemini / "config" / "plugins"
    try:
        plugins = sorted(plugins_root.iterdir()) if plugins_root.is_dir() else []
    except OSError as exc:
        # An unreadable plugins dir must not 500 the /skills endpoint.
        _log.warning("Skipping unreadable agy plugins dir %s: %s", plugins_root, exc)
        plugins = []
    for plugin in plugins:
        if not (plugin / "plugin.json").is_file():
            continue  # absent or renamed to plugin.json.disabled
        skills_dir = plugin / "skills"
        if skills_dir.is_dir():
            dirs.append((skills_dir, plugin.name))
    builtin = gemini / "antigravity-cli" / "builtin" / "skills"
    if builtin.is_dir():
        dirs.append((builtin, None))
    return dirs


def antigravity_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """
    agy's own skills: enabled ``~/.gemini`` plugins plus shipped builtins.

    agy owns a host-skill mechanism (a ``/skills`` TUI listing backed by
    :func:`_agy_skill_dirs`), and unlike Pi's that mechanism is enumerable — so
    agy gets a real provider rather than a no-op. It deliberately does NOT
    compose with :func:`_generic_host_skills` (the way ``claude`` does): the
    generic walk sources ``~/.claude/skills``, which are Claude's, not agy's.
    The menu lists what this agent actually has, matching codex/cursor.

    Honors ``skills_filter`` (``"none"`` hermetic, ``"all"`` everything, a list
    selecting by the surfaced name). Names match what agy's TUI accepts —
    ``<plugin>:<skill>`` for plugin skills, bare elsewhere — because a native
    session's ``/name`` is sent to the vendor CLI as plaintext for it to expand
    (there is no server-side resolve+inject on this path), so a label agy does
    not recognise would simply fail.

    :param ctx: Session discovery context.
    :returns: Parsed skills; unparseable ones are skipped (best-effort).
    """
    if ctx.skills_filter == "none":
        return []
    filter_names: set[str] | None = (
        set(ctx.skills_filter) if isinstance(ctx.skills_filter, list) else None
    )
    out: list[SkillSpec] = []
    for skills_dir, namespace in _agy_skill_dirs(ctx):
        try:
            children = sorted(skills_dir.iterdir())
        except OSError as exc:
            _log.warning("Skipping unreadable agy skills dir %s: %s", skills_dir, exc)
            continue
        for child in children:
            if not child.is_dir() or not (child / "SKILL.md").is_file():
                continue
            try:
                spec = _parse_skill(child / "SKILL.md")
            except (OmnigentError, OSError):  # best-effort discovery
                continue
            name = spec.name if namespace is None else f"{namespace}:{spec.name}"
            if filter_names is not None and name not in filter_names:
                continue
            out.append(spec if namespace is None else replace(spec, name=name))
    return out


def pi_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """
    Pi exposes no *extra* discoverable skills to the menu.

    Pi has its **own** host-skill mechanism: it loads bundle skills via
    ``--skill`` (already carried by ``spec.skills``, which the runner
    unions in separately) and runs its own auto-discovery at runtime,
    sourcing from Pi's internal extension layout. omnigent can't enumerate
    that layout to name/resolve those skills for the menu, so listing any
    would risk surfacing a command Pi can't invoke. Hence the explicit
    no-op (under-report when unsure).

    This is why Pi gets a dedicated provider rather than the generic
    ``~/.claude/skills`` fallback: the fallback's skills are resolvable by
    omnigent on harnesses that have *no* host-skill mechanism of their own
    (the unregistered SDK/CLI harnesses — antigravity, qwen — which route a
    typed skill through omnigent's own resolve+inject), but Pi's competing,
    unenumerable mechanism makes that fallback unsafe. The distinction is
    "does the harness own an unenumerable host-skill mechanism", not the
    vendor.

    :param ctx: Session discovery context (unused).
    :returns: Always an empty list.
    """
    del ctx
    return []


# Keyed by harness family (see _harness_family). A harness with no entry
# (qwen, openai-agents, the in-process antigravity SDK, …) falls through to
# _generic_host_skills in resolve_harness_skills — the ~/.claude/skills walk,
# whose skills omnigent resolves+injects regardless of vendor.
#
# The dividing line is whether the harness owns a host-skill mechanism, and if
# so whether omnigent can enumerate it:
#
#   no mechanism            -> generic walk (omnigent injects the skill text)
#   mechanism, enumerable   -> dedicated provider listing what the agent has
#                              (claude, codex, cursor, antigravity/agy)
#   mechanism, unenumerable -> explicit no-op (pi) — listing anything would
#                              risk surfacing a command the harness can't run
#
# agy moved from the first row to the second when it gained plugin + builtin
# skills; it is enumerable (see antigravity_host_skills), so it lists its own.
_SKILL_SOURCES: dict[str | None, SkillSource] = {
    "claude": claude_host_skills,
    "codex": codex_host_skills,
    "cursor": cursor_host_skills,
    "pi": pi_host_skills,
    "antigravity": antigravity_host_skills,
}
