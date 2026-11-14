"""Resolve managed providers without importing Pi's login or configuration.

OMP reads ``models.yml`` and ``config.yml`` and owns its SQLite authentication
store. Managed sessions reuse our wire-provider records and bounded token command
transport; unmanaged sessions keep OMP's own root and use its public model CLI.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from omnigent.harnesses.pi_native.auth_command import AUTH_DIR_NAME, wrap_auth_command
from omnigent.harnesses.pi_native.bridge import _atomic_text
from omnigent.harnesses.pi_native.credentials import (
    PI_CODING_AGENT_DIR_ENV_VAR,
    PiNativeLaunch,
    PiProviderConfig,
    _enabled_model_refs,
    _native_model_selection,
    _native_provider_model_options,
    _native_thinking_args,
    _PiModelsConfig,
    pi_own_login_model_arg,
    resolve_pi_native_provider,
)
from omnigent.harnesses.pi_native.model_scope import ScopableModel
from omnigent.inner.pi_settings import _relocate_resource_settings
from omnigent.onboarding.provider_config import load_config

if TYPE_CHECKING:
    import httpx

    from omnigent.spec.types import ExecutorAuth

_LOGGER = logging.getLogger(__name__)
AUTH_DIR_ENV = "OMNIGENT_OMP_NATIVE_AUTH_DIR"
# OMP discovers these resources in its own agent root. Authentication databases
# and provider configuration are deliberately not linked into managed sessions.
_OMP_RESOURCES = (
    "extensions",
    "skills",
    "managed-skills",
    "prompts",
    "themes",
    "hooks",
    "commands",
    "tools",
    "modules",
    "plugins",
    "npm",
    "git",
    "mcp.json",
    ".mcp.json",
    "mcp-auth.json",
    "keybindings.json",
    "AGENTS.override.md",
    "AGENTS.md",
    "AGENTS.MD",
    "CLAUDE.md",
    "CLAUDE.MD",
)


def global_omp_agent_dir(
    *, env: Mapping[str, str] | None = None, workspace: Path | None = None
) -> Path:
    """Resolve OMP's native config root, including profiles and directory overrides."""
    source_env = os.environ if env is None else env
    config_root = Path.home() / (source_env.get("PI_CONFIG_DIR") or ".omp")
    profile = source_env.get("OMP_PROFILE", source_env.get("PI_PROFILE", "")).strip()
    if profile and profile != "default":
        root = config_root / "profiles" / profile / "agent"
    else:
        override = source_env.get(PI_CODING_AGENT_DIR_ENV_VAR, "").strip()
        root = Path(override).expanduser() if override else config_root / "agent"
    return workspace / root if workspace is not None and not root.is_absolute() else root


def _read_omp_settings(root: Path) -> dict[str, object]:
    """Read canonical OMP settings, using the same YAML fallback order as OMP."""
    for name in ("config.yml", "config.yaml"):
        try:
            raw = yaml.safe_load((root / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, yaml.YAMLError):
            _LOGGER.warning("Unable to read OMP settings at %s", root / name)
            return {}
        return raw if isinstance(raw, dict) else {}
    return {}


def _omp_own_login_models(
    agent_dir: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> list[ScopableModel]:
    """Ask OMP for its available chat catalog, never inspect credential storage.

    ``omp models --json`` uses OMP's AuthStorage (agent.db), environment keys,
    custom YAML providers and extension models through the supported registry.
    """
    child_env = dict(os.environ if env is None else env)
    executable = child_env.get("OMNIGENT_OMP_PATH") or shutil.which(
        "omp", path=child_env.get("PATH")
    )
    if not executable:
        return []
    if agent_dir is not None:
        child_env[PI_CODING_AGENT_DIR_ENV_VAR] = str(agent_dir)
        child_env["OMP_PROFILE"] = ""
        child_env["PI_PROFILE"] = ""
    try:
        result = subprocess.run(
            [executable, "models", "--json"],
            env=child_env,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            return []
        payload = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _LOGGER.debug("OMP model catalog command unavailable", exc_info=True)
        return []
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    entries: dict[str, ScopableModel] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider, model_id, name = row.get("provider"), row.get("id"), row.get("name")
        if (
            not isinstance(provider, str)
            or not provider
            or not isinstance(model_id, str)
            or not model_id
        ):
            continue
        entry = ScopableModel(
            provider=provider,
            model_id=model_id,
            name=name if isinstance(name, str) and name else None,
        )
        # Retired managed selections cannot address an OMP-owned provider.
        if pi_own_login_model_arg(entry.reference) != entry.reference:
            continue
        entries[entry.reference] = entry
    return list(entries.values())


def _omp_native_metadata(
    payload: Mapping[str, object],
    *,
    executable: str | None = None,
    env: Mapping[str, str] | None = None,
) -> object:
    """Resolve metadata inside OMP's loader, independent of executable layout."""
    child_env = dict(os.environ if env is None else env)
    executable = executable or child_env.get("OMNIGENT_OMP_PATH") or shutil.which(
        "omp", path=child_env.get("PATH")
    )
    if not executable:
        return None
    helper = files("omnigent.resources.omp_native").joinpath("resolve_model_scope.mjs")
    try:
        result = subprocess.run(
            [
                executable,
                "models",
                "--json",
                "--no-extensions",
                "--extension",
                str(helper),
            ],
            input=json.dumps(payload),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        prefix = "OMNIGENT_OMP_NATIVE_RESOLVER:"
        for line in result.stdout.splitlines():
            if line.startswith(prefix):
                return json.loads(line[len(prefix) :])
    except (OSError, ValueError, subprocess.SubprocessError):
        _LOGGER.debug("OMP native metadata command unavailable", exc_info=True)
    return None


def resolve_omp_package_root(
    executable: str | None = None, *, env: Mapping[str, str] | None = None
) -> Path | None:
    """Ask OMP's supported getPackageDir API for its host package location.

    This honors OMP's PI_PACKAGE_DIR override and works when the selected
    executable is a wrapper outside the package tree.
    """
    root = _omp_native_metadata({"operation": "package-root"}, executable=executable, env=env)
    return Path(root) if isinstance(root, str) and root else None


def _scope_omp_models(
    patterns: list[str],
    models: list[ScopableModel],
    settings: Mapping[str, object],
) -> list[ScopableModel]:
    """Apply OMP's native matcher through its loader, including wrapper launches."""
    if not models:
        return []
    indices = _omp_native_metadata(
        {
            "operation": "scope",
            "patterns": patterns,
            "modelRoles": settings.get("modelRoles", {}),
            "models": [
                {
                    "provider": model.provider,
                    "id": model.model_id,
                    "name": model.name or model.model_id,
                }
                for model in models
            ],
        }
    )
    if not isinstance(indices, list) or any(
        type(index) is not int or not 0 <= index < len(models) for index in indices
    ):
        _LOGGER.warning("OMP model curation unavailable; hiding the curated catalog")
        return []
    return [models[index] for index in dict.fromkeys(indices)]


def omp_own_login_model_options(agent_dir: Path | None = None) -> list[dict[str, object]]:
    """List OMP's own authenticated models with its global picker curation."""
    entries = _omp_own_login_models(agent_dir)
    root = agent_dir if agent_dir is not None else global_omp_agent_dir()
    settings = _read_omp_settings(root)
    raw = settings.get("enabledModels")
    patterns = (
        [item for item in raw if isinstance(item, str) and item.strip()]
        if isinstance(raw, list)
        else []
    )
    if patterns:
        entries = _scope_omp_models(patterns, entries, settings)
    return [
        {
            "id": entry.reference,
            "model": entry.reference,
            "displayName": entry.name or entry.model_id,
        }
        for entry in sorted(entries, key=lambda entry: entry.reference)
    ]


def omp_own_login_serves_reference(reference: str | None, agent_dir: Path | None = None) -> bool:
    """Check the full OMP catalog independently of picker curation."""
    return bool(reference) and any(
        entry.reference == reference for entry in _omp_own_login_models(agent_dir)
    )


def omp_own_login_model_arg(selection: str) -> str | None:
    """Preserve OMP-owned selectors, stripping unusable managed-provider prefixes."""
    return pi_own_login_model_arg(selection)


def resolve_omp_native_provider(
    *,
    model: str | None = None,
    config_loader: Callable[[], dict[str, object]] = load_config,
    auth: ExecutorAuth | None = None,
) -> PiProviderConfig | None:
    """Resolve OMP's independent inference binding without reading Pi auth.json."""
    return resolve_pi_native_provider(
        model=model,
        config_loader=config_loader,
        auth=auth,
        harness="omp-native",
        own_login_serves_reference=omp_own_login_serves_reference,
    )


def omp_native_model_options(
    *,
    config_loader: Callable[[], dict[str, object]] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, object]]:
    """Return managed provider choices, or the real OMP own-login catalog."""
    provider = (
        resolve_omp_native_provider()
        if config_loader is None
        else resolve_omp_native_provider(config_loader=config_loader)
    )
    if provider is None:
        return omp_own_login_model_options()
    return _native_provider_model_options(provider, transport=transport)


def _omp_models_config(rendered: _PiModelsConfig) -> dict[str, Any]:
    """Remove Pi-only hints not supported by OMP's canonical YAML schema.

    OMP derives model thinking controls from its own catalog policy, rather
    than Pi's legacy thinkingLevelMap / forceAdaptiveThinking properties.
    """
    result = cast("dict[str, Any]", deepcopy(rendered))
    for payload in result["providers"].values():
        for entry in [payload, *payload["models"]]:
            entry.pop("thinkingLevelMap", None)
            compat = entry.get("compat")
            if isinstance(compat, dict):
                compat.pop("forceAdaptiveThinking", None)
                if not compat:
                    entry.pop("compat", None)
        if not payload.get("apiKey"):
            payload.pop("apiKey", None)
            payload["auth"] = "none"
    return result


def write_omp_models_config(
    agent_dir: Path,
    provider: PiProviderConfig,
    rendered: _PiModelsConfig | None = None,
) -> Path:
    """Write/refresh the live models.yml, including private renewable commands."""
    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(agent_dir, 0o700)
    for config_path in (agent_dir / AUTH_DIR_NAME).glob("auth-*.json"):
        if not config_path.name.endswith(".result.json"):
            config_path.unlink(missing_ok=True)
    config = _omp_models_config(
        rendered if rendered is not None else provider.to_models_config(coding_agent="omp")
    )
    providers = config["providers"]
    for provider_id, payload in providers.items():
        api_key = payload.get("apiKey")
        if os.name == "posix" and isinstance(api_key, str) and api_key.startswith("!"):
            payload["apiKey"] = wrap_auth_command(
                agent_dir,
                provider_id,
                api_key[1:],
                refresh_interval_ms=provider.auth_refresh_interval_ms,
                refresh_timeout_ms=provider.auth_timeout_ms,
            )
    path = agent_dir / "models.yml"
    _atomic_text(path, yaml.safe_dump(config, sort_keys=False))
    # A managed session never relies on OMP's one-time legacy JSON migration.
    (agent_dir / "models.json").unlink(missing_ok=True)
    return path


def _prepare_managed_omp_agent_dir(
    agent_dir: Path, root: Path, *, enabled_models: list[str] | None
) -> None:
    """Preserve OMP user resources while keeping managed provider state isolated."""
    root = root.expanduser().absolute()
    if agent_dir.resolve() == root.resolve():
        raise ValueError("OMP managed configuration must not overwrite its own login directory")
    settings = _relocate_resource_settings(_read_omp_settings(root), root, agent_dir.absolute())
    settings.pop("defaultThinkingLevel", None)
    if enabled_models is not None:
        settings["enabledModels"] = enabled_models
    handlers = settings.get("extensionHandlers")
    settings["extensionHandlers"] = {
        **(handlers if isinstance(handlers, dict) else {}),
        "toolCallTimeoutMs": 86_410_000,
    }
    _atomic_text(agent_dir / "config.yml", yaml.safe_dump(settings, sort_keys=False))
    for name in _OMP_RESOURCES:
        source, target = root / name, agent_dir / name
        if target.is_symlink():
            if source.exists() and target.exists() and target.samefile(source):
                continue
            target.unlink()
        elif target.exists():
            continue
        if source.exists():
            target.symlink_to(source, target_is_directory=source.is_dir())


def omp_native_provider_launch(
    agent_dir: Path,
    provider: PiProviderConfig,
    reasoning_effort: str | None = None,
    *,
    selection: str | None = None,
    global_agent_dir: Path | None = None,
    client_surface: str | None = None,
    omp_env: Mapping[str, str] | None = None,
) -> PiNativeLaunch:
    """Write canonical OMP configuration and return native env/args/effort notice.

    Call this only for a managed provider. With no managed provider the caller
    leaves OMP's root, SQLite login, profiles and resources entirely unchanged.
    """
    root = global_agent_dir if global_agent_dir is not None else global_omp_agent_dir(env=omp_env)
    if agent_dir.resolve() == root.expanduser().resolve():
        raise ValueError("OMP managed configuration must not overwrite its own login directory")
    rendered = provider.to_models_config(client_surface=client_surface, coding_agent="omp")
    provider_id, model_id = _native_model_selection(provider, rendered, selection)
    write_omp_models_config(agent_dir, provider, rendered)
    _prepare_managed_omp_agent_dir(
        agent_dir,
        root,
        enabled_models=_enabled_model_refs(rendered) if provider.curated_models else None,
    )
    env = {
        PI_CODING_AGENT_DIR_ENV_VAR: str(agent_dir),
        "OMP_PROFILE": "",
        "PI_PROFILE": "",
    }
    if os.name == "posix" and any(
        payload.get("apiKey", "").startswith("!") for payload in rendered["providers"].values()
    ):
        env[AUTH_DIR_ENV] = str(agent_dir / AUTH_DIR_NAME)
    if provider.inference_bound:
        env["OMNIGENT_OMP_INFERENCE_BOUND"] = "1"
    model_arg = f"{provider_id}/{model_id}" if "/" in model_id else model_id
    args = ["--provider", provider_id, "--model", model_arg]
    thinking_args, effort_warning = _native_thinking_args(reasoning_effort, harness="omp")
    args.extend(thinking_args)
    from omnigent.inner.pi_gateway import pi_gateway_extension_args

    gateway_args = pi_gateway_extension_args(agent_dir, rendered)
    if gateway_args:
        gateway_entrypoint = agent_dir / "omnigent_omp_gateway.mjs"
        _atomic_text(
            gateway_entrypoint,
            files("omnigent.resources.omp_native")
            .joinpath(gateway_entrypoint.name)
            .read_text(encoding="utf-8"),
        )
        args.extend(["--extension", str(gateway_entrypoint)])
    return PiNativeLaunch(env=env, args=args, effort_warning=effort_warning)
