"""Apply portable plugin intent in private session settings, preserving native policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent.config import load_global_config, load_local_config
from omnigent.preference_startup import inherited_launch
from omnigent.preferences import PluginIdentity, PreferenceKey, PreferenceKind

if TYPE_CHECKING:
    from omnigent.spec.skill_sources import SkillSourceContext
    from omnigent.spec.types import SkillSpec


@dataclass(frozen=True)
class PluginOverlay:
    enabled: dict[str, bool] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def requested_plugins(ecosystem: str, workspace: Path | None) -> dict[str, bool]:
    """Native names are plugin@marketplace; source paths and installations stay local."""
    launch = inherited_launch()
    if launch is None:
        return {}
    project = load_local_config(workspace / ".omnigent/config.yaml") if workspace else {}
    resolved = launch.resolve(load_global_config(), project)
    values = {}
    for key, preference in resolved.preferences.items():
        if key.kind != PreferenceKind.PLUGIN:
            continue
        identity = key.name
        assert isinstance(identity, PluginIdentity) and isinstance(preference.value, bool)
        if identity.ecosystem != ecosystem:
            continue
        if "@" in identity.name or "@" in identity.source:
            raise ValueError("Native plugin names and marketplace names cannot contain '@'")
        values[f"{identity.name}@{identity.source}"] = preference.value
    return values


def claude_plugin_overlay(
    ctx: SkillSourceContext, *, session_settings: Mapping[str, object] | None = None
) -> PluginOverlay:
    """Merge requested toggles with project rules; never enable an uninstalled plugin."""
    requested = requested_plugins("claude", ctx.roots[0] if ctx.roots else None)
    if not requested:
        return PluginOverlay()
    from omnigent.spec.skill_sources import (
        _enabled_plugin_settings_files,
        _managed_plugin_keys,
        _plugin_install_paths,
        _read_json,
    )

    values = dict(requested)
    warnings = []
    for path in _enabled_plugin_settings_files(ctx)[2:]:
        data = _read_json(path) or {}
        toggles = data.get("enabledPlugins")
        if isinstance(toggles, dict):
            for key in values.keys() & toggles.keys():
                if isinstance(toggles[key], bool):
                    values[key] = toggles[key]
    session_toggles = (session_settings or {}).get("enabledPlugins")
    if isinstance(session_toggles, dict):
        for key in values.keys() & session_toggles.keys():
            if isinstance(session_toggles[key], bool):
                values[key] = session_toggles[key]
    managed = _managed_plugin_keys(ctx)
    installed = _plugin_install_paths(ctx)
    for key, desired in requested.items():
        if key in managed:
            values[key] = True
        if values[key] != desired:
            warnings.append(
                f"Plugin {key}: native project/session/policy overrides the saved choice."
            )
        if values[key] and (key not in installed or not installed[key].is_dir()):
            # A native enabledPlugins=true entry can install from a marketplace.
            # Keep the requested value in the snapshot, but never trigger installation.
            values[key] = False
            warnings.append(
                f"Plugin {key}: selected but not installed; install it on this machine."
            )
    return PluginOverlay(values, tuple(warnings))


def apply_codex_plugin_overlay(
    codex_home: Path,
    source_home: Path,
    workspace: Path,
    *,
    codex_path: str,
) -> PluginOverlay:
    """Write only the private user tier; Codex applies project and managed tiers above it."""
    requested = requested_plugins("codex", workspace)
    if not requested:
        return PluginOverlay()
    import tomlkit

    from omnigent.harnesses.codex_native.bridge import _upsert_top_level_config_key
    from omnigent.spec.codex_plugin_skills import _inventory_plugins

    config = codex_home / "config.toml"
    if codex_home.resolve() == source_home.resolve() or config.is_symlink():
        raise ValueError("Plugin preferences require a private Codex config")
    inventory = _inventory_plugins(source_home, workspace, codex_path=codex_path)
    installed = {
        entry.get("pluginId")
        for entry in inventory or []
        if isinstance(entry, dict) and entry.get("installed") is True
    }
    values = dict(requested)
    warnings = []
    for key, enabled in requested.items():
        if enabled and key not in installed:
            values[key] = False
            detail = "inventory unavailable" if inventory is None else "not installed"
            warnings.append(f"Plugin {key}: selected but {detail}; no installation was attempted.")
    document = tomlkit.parse(config.read_text() if config.exists() else "")
    plugins = document.get("plugins", {})
    if not isinstance(plugins, Mapping):
        raise ValueError("Codex plugin configuration must be a table")
    plugins = dict(plugins)
    for key, enabled in values.items():
        entry = plugins.get(key, {})
        if not isinstance(entry, Mapping):
            raise ValueError(f"Invalid Codex plugin configuration for {key}")
        plugins[key] = {**entry, "enabled": enabled}
    if not _upsert_top_level_config_key(config, "plugins", plugins):
        raise ValueError("Could not write private Codex plugin preferences")
    return PluginOverlay(values, tuple(warnings))


def import_plugin_values(ecosystems: tuple[str, ...]) -> dict[PreferenceKey, bool]:
    """Read explicit user toggles only; project and enforced policy are not user defaults."""
    from omnigent.spec.skill_sources import (
        _enabled_plugin_settings_files,
        _read_json,
        skill_source_context_from_env,
    )

    values = {}
    for ecosystem in ecosystems:
        state = {}
        if ecosystem == "claude":
            from dataclasses import replace

            ctx = replace(
                skill_source_context_from_env(roots=(), harness="claude-native"), is_native=True
            )
            for path in _enabled_plugin_settings_files(ctx)[:2]:
                data = _read_json(path) or {}
                toggles = data.get("enabledPlugins")
                if isinstance(toggles, dict):
                    state.update(
                        {key: value for key, value in toggles.items() if type(value) is bool}
                    )
        elif ecosystem == "codex":
            import tomllib

            from omnigent.inner.codex_executor import _codex_home_config_source_from_env

            path = _codex_home_config_source_from_env() / "config.toml"
            document = tomllib.loads(path.read_text()) if path.exists() else {}
            plugins = document.get("plugins", {})
            if isinstance(plugins, dict):
                state.update(
                    {
                        key: entry["enabled"]
                        for key, entry in plugins.items()
                        if isinstance(entry, dict) and type(entry.get("enabled")) is bool
                    }
                )
        else:
            raise ValueError(f"Unsupported plugin ecosystem: {ecosystem}")
        for key, enabled in state.items():
            parts = key.split("@")
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"Plugin {key!r} needs a plugin@marketplace identity to import")
            values[
                PreferenceKey(PreferenceKind.PLUGIN, PluginIdentity(ecosystem, parts[1], parts[0]))
            ] = enabled
    return values


def codex_sdk_plugin_skills(
    source_home: Path, workspace: Path, skills_filter: str | list[str]
) -> list[SkillSpec] | None:
    """Discover through the same private user tier as SDK startup; keep stable asset paths."""
    if skills_filter != "all" or not requested_plugins("codex", workspace):
        return None
    import logging
    from dataclasses import replace
    from tempfile import TemporaryDirectory

    import tomlkit

    from omnigent.inner.codex_executor import _find_codex_cli, codex_minimal_config_requested
    from omnigent.spec.codex_plugin_skills import discover_codex_plugin_skills

    if codex_minimal_config_requested():
        return []
    codex = _find_codex_cli()
    if codex is None:
        return []
    with TemporaryDirectory(prefix="omnigent-preference-skills-") as directory:
        private = Path(directory)
        # Discovery needs marketplace registration, but no provider/auth config or hooks.
        source_config = source_home / "config.toml"
        document = tomlkit.parse(source_config.read_text() if source_config.exists() else "")
        config = {
            key: document[key]
            for key in ("plugins", "marketplaces", "projects")
            if key in document
        }
        (private / "config.toml").write_text(tomlkit.dumps(config))
        cache = source_home / "plugins/cache"
        if cache.is_dir():
            (private / "plugins").mkdir()
            (private / "plugins/cache").symlink_to(cache.resolve(), target_is_directory=True)
        overlay = apply_codex_plugin_overlay(private, source_home, workspace, codex_path=codex)
        for warning in overlay.warnings:
            logging.getLogger(__name__).warning("%s", warning)
        return [
            replace(skill, skill_dir=skill.skill_dir.resolve() if skill.skill_dir else None)
            for skill in discover_codex_plugin_skills(private, skills_filter, cwd=workspace)
        ]
